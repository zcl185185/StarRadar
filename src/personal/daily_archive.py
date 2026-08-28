"""个人雷达的每日发现归档。

``scores.json`` 始终是当前最新结果；每次个人管道完成时，另外存一份不可覆盖的
快照到 ``data/personal/daily_history``。这样手动刷新也不会丢掉刷新前看到的项目。
"""
from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path

from config import PERSONAL_DIR


ARCHIVE_DIR = PERSONAL_DIR / "daily_history"
RETENTION_DAYS = 90


def _read_json(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return value if isinstance(value, dict) else None


def _archive_paths() -> list[Path]:
    if not ARCHIVE_DIR.is_dir():
        return []
    return sorted(ARCHIVE_DIR.glob("*.json"), key=lambda p: p.name)


def _archive_items(payload: dict) -> dict[str, tuple[int, dict]]:
    result: dict[str, tuple[int, dict]] = {}
    for index, item in enumerate(payload.get("items") or [], start=1):
        repo = item.get("repo") if isinstance(item, dict) else None
        name = str((repo or {}).get("full_name") or "")
        if name:
            result[name] = (index, item)
    return result


def _existing_occurrences() -> dict[str, dict[str, object]]:
    """读取已有归档，得到项目的首次发现与出现次数。"""
    seen: dict[str, dict[str, object]] = {}
    for path in _archive_paths():
        payload = _read_json(path) or {}
        date = str(payload.get("date") or path.name[:10])
        for name in _archive_items(payload):
            entry = seen.setdefault(name, {"first_seen": date, "appearances": 0})
            entry["appearances"] = int(entry["appearances"]) + 1
    return seen


def _latest_archive() -> dict | None:
    for path in reversed(_archive_paths()):
        payload = _read_json(path)
        if payload:
            return payload
    return None


def _cleanup_old_archives(today: datetime) -> None:
    cutoff = today.date() - timedelta(days=RETENTION_DAYS)
    for path in _archive_paths():
        try:
            day = datetime.strptime(path.name[:10], "%Y-%m-%d").date()
        except ValueError:
            continue
        if day < cutoff:
            try:
                path.unlink()
            except OSError:
                pass


def save_daily_archive(payload: dict) -> dict:
    """保存一次雷达运行，并附上相对上一次运行的变化信息。

    同一天多次点击刷新会生成多个运行记录，而不是相互覆盖；界面可按日期与运行时刻
    回看。完整快照默认保留 90 天。
    """
    now_local = datetime.now().astimezone()
    archive_date = now_local.date().isoformat()
    stamp = now_local.strftime("%H%M%S")
    previous = _latest_archive()
    previous_items = _archive_items(previous or {})
    previous_seen = _existing_occurrences()

    archived = deepcopy(payload)
    archived["version"] = 1
    archived["date"] = archive_date
    archived["source"] = "personal_radar"

    items = archived.get("items") or []
    added = returned = moved_up = moved_down = unchanged = 0
    for rank, item in enumerate(items, start=1):
        repo = item.get("repo") if isinstance(item, dict) else None
        name = str((repo or {}).get("full_name") or "")
        old = previous_items.get(name)
        old_seen = previous_seen.get(name)
        if old is None:
            status = "returned" if old_seen else "new"
            returned += int(status == "returned")
            added += int(status == "new")
            previous_rank = None
        else:
            previous_rank = old[0]
            if rank < previous_rank:
                status = "up"
                moved_up += 1
            elif rank > previous_rank:
                status = "down"
                moved_down += 1
            else:
                status = "steady"
                unchanged += 1
        item["daily_history"] = {
            "status": status,
            "rank": rank,
            "previous_rank": previous_rank,
            "first_seen": (old_seen or {}).get("first_seen", archive_date),
            "appearances": int((old_seen or {}).get("appearances", 0)) + 1,
        }

    archived["comparison"] = {
        "compared_to": (previous or {}).get("archive_id"),
        "new": added,
        "returned": returned,
        "up": moved_up,
        "down": moved_down,
        "steady": unchanged,
        "disappeared": len(set(previous_items) - set(_archive_items(archived))),
    }

    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    archive_id = f"{archive_date}_{stamp}"
    target = ARCHIVE_DIR / f"{archive_id}.json"
    suffix = 2
    while target.exists():
        archive_id = f"{archive_date}_{stamp}_{suffix:02d}"
        target = ARCHIVE_DIR / f"{archive_id}.json"
        suffix += 1
    archived["archive_id"] = archive_id
    target.write_text(json.dumps(archived, ensure_ascii=False, indent=2), encoding="utf-8")
    _cleanup_old_archives(now_local)
    return archived


def list_daily_archives() -> list[dict]:
    """返回轻量目录，最新记录在前，不把全部项目重复传给前端。"""
    result: list[dict] = []
    for path in reversed(_archive_paths()):
        payload = _read_json(path)
        if not payload:
            continue
        comparison = payload.get("comparison") or {}
        result.append({
            "archive_id": payload.get("archive_id") or path.stem,
            "date": payload.get("date") or path.name[:10],
            "generated_at": payload.get("generated_at") or "",
            "count": len(payload.get("items") or []),
            "comparison": comparison,
        })
    return result


def load_daily_archive(archive_id: str) -> dict | None:
    """按由本服务返回的归档 ID 读取，拒绝任何路径字符。"""
    if not archive_id or not all(ch.isdigit() or ch in "-_" for ch in archive_id):
        return None
    path = ARCHIVE_DIR / f"{archive_id}.json"
    if not path.is_file():
        return None
    return _read_json(path)
