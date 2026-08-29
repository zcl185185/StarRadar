"""本地 For You 推荐快照与后台刷新。

只使用 GitHub 支持的 REST API；推荐是派生快照，与 Stars 主表、笔记分开保存。
"""
from __future__ import annotations

import json
import logging
import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from config import PROFILE_DIR

log = logging.getLogger(__name__)
MAX_ITEMS, MAX_SEEDS, MAX_QUERIES, MAX_RESULTS = 20, 12, 6, 100
COOLDOWN = 15 * 60
_refresh_lock = threading.Lock()


def _credentials() -> tuple[str, str]:
    try:
        data = json.loads((PROFILE_DIR / "gh_token.json").read_text(encoding="utf-8"))
        return str(data.get("login") or "").strip().lower(), str(data.get("token") or "").strip()
    except (OSError, json.JSONDecodeError, AttributeError):
        return "", ""


def _path(login: str) -> Path:
    return PROFILE_DIR / "recommendations" / f"{login or 'local'}.json"


def load_snapshot(login: str | None = None) -> dict | None:
    login = (login or _credentials()[0]).lower() or "local"
    try:
        snapshot = json.loads(_path(login).read_text(encoding="utf-8"))
        changed = False
        for item in snapshot.get("items", []) if isinstance(snapshot, dict) else []:
            if not isinstance(item, dict):
                continue
            if not item.get("summary_zh") or not item.get("novelty"):
                shared = list((item.get("_recommendation") or {}).get("sharedResearch") or item.get("topics") or [])
                summary, novelty = _summary(item, [str(x) for x in shared])
                item["summary_zh"] = summary; item["novelty"] = novelty
                changed = True
        if changed and isinstance(snapshot, dict):
            save_snapshot(snapshot)
        return snapshot
    except (OSError, json.JSONDecodeError):
        return None


def save_snapshot(snapshot: dict) -> None:
    login = str(snapshot.get("account") or "local").lower()
    p = _path(login); p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


def load_reviews(login: str | None = None) -> dict:
    login = (login or _credentials()[0]).lower() or "local"
    p = _path(login).with_name(f"{login}.reviews.json")
    try: return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError): return {}


def save_review(full_name: str, status: str, note: str, login: str | None = None) -> dict:
    account = (login or _credentials()[0]).lower() or "local"; reviews = load_reviews(account)
    reviews[full_name] = {"status": status if status in {"seen", "worth_research", "dismissed"} else "seen", "note": str(note or "")[:2000], "updated_at": datetime.now().isoformat()}
    p = _path(account).with_name(f"{account}.reviews.json"); p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(reviews, ensure_ascii=False, indent=2), encoding="utf-8"); return reviews[full_name]


def _gh(token: str, path: str, params: dict | None = None):
    url = "https://api.github.com" + path
    if params: url += "?" + urlencode(params)
    req = Request(url, headers={"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json", "User-Agent": "StarRadar"})
    with urlopen(req, timeout=20) as r: return json.loads(r.read().decode("utf-8"))


def _words(text: str) -> list[str]:
    import re
    return [w for w in re.findall(r"[a-zA-Z][a-zA-Z0-9_-]{2,}", text.lower()) if w not in {"the", "and", "for", "with", "from", "this", "github"}][:8]


def _summary(repo: dict, shared: list[str]) -> tuple[str, str]:
    topics = [str(x) for x in repo.get("topics") or []][:4]; lang = repo.get("language") or "通用技术"
    focus = "、".join(shared[:3] or topics[:3]) or lang
    zh = f"这是一个使用{lang}、聚焦{focus}方向的开源项目。"
    novelty = f"新颖点：公开主题明确指向{focus}，并采用{lang}实现；这是基于仓库公开元数据提炼的可核对信息。" if focus else "新颖点：当前公开元数据较少，已把可确认的信息直接列出。"
    return zh, novelty


def refresh(force: bool = False) -> dict:
    account, token = _credentials()
    old = load_snapshot(account)
    if not token: return old or {"account": account or "local", "status": "not_configured", "items": []}
    now = time.time(); now_ms = int(now * 1000)
    if old and not force and old.get("nextAllowedAt", 0) > now_ms: return old
    try:
        raw = _gh(token, "/user/starred", {"per_page": 100, "page": 1, "sort": "created", "direction": "desc"})
        seeds = raw if isinstance(raw, list) else []
        seeds = [((x.get("repo") if isinstance(x, dict) and isinstance(x.get("repo"), dict) else x)) for x in seeds]
        seeds = [x for x in seeds if isinstance(x, dict)][:MAX_SEEDS]
        starred = {str(x.get("full_name")) for x in seeds}
        topics = sorted({str(t).lower() for s in seeds for t in (s.get("topics") or [])})[:2]
        langs = sorted({str(s.get("language")) for s in seeds if s.get("language")})[:2]
        queries = [f"topic:{t} stars:>=10 archived:false fork:false" for t in topics] + [f"language:{l} stars:>=25 archived:false fork:false" for l in langs]
        queries = queries[:MAX_QUERIES]; candidates = {}
        # Search 请求彼此独立，并行执行，避免网络慢时“换一批”长时间无反馈。
        def search_one(q: str):
            return _gh(token, "/search/repositories", {"q": q, "sort": "stars", "order": "desc", "per_page": MAX_RESULTS, "page": 1})
        with ThreadPoolExecutor(max_workers=min(3, len(queries) or 1)) as pool:
            for future in as_completed([pool.submit(search_one, q) for q in queries]):
                data = future.result()
                for item in (data.get("items") or [])[:MAX_RESULTS]:
                    name = item.get("full_name")
                    if name: candidates[name] = item
        items = []
        for item in candidates.values():
            name = item.get("full_name", "")
            if name in starred or item.get("fork") or item.get("archived"): continue
            ctopics = {str(t).lower() for t in item.get("topics") or []}; shared = sorted(ctopics.intersection(topics))
            score = 100 * len(shared) + 10 * sum(1 for s in seeds if s.get("language") and s.get("language") == item.get("language")) + 6 * math.log10(int(item.get("stargazers_count") or 0) + 1)
            if not score: continue
            summary, novelty = _summary(item, shared)
            item["summary_zh"], item["novelty"] = summary, novelty
            item["_recommendation"] = {"score": score, "seed": seeds[0].get("full_name", "相关星标") if seeds else "相关星标", "sharedResearch": shared or topics[:1], "reasons": (["共同研究方向：" + "、".join(shared)] if shared else ["与你的星标语言和技术关键词相关"])}
            items.append(item)
        items.sort(key=lambda x: (-x["_recommendation"]["score"], -int(x.get("stargazers_count") or 0), x.get("full_name", "")))
        snap = {"account": account, "status": "fresh", "generated_at": datetime.now().isoformat(), "attempted_at": datetime.now().isoformat(), "seeds": len(seeds), "queries": len(queries), "items": items[:MAX_ITEMS], "nextAllowedAt": 0}
        save_snapshot(snap); return snap
    except Exception as exc:  # 保留上一份完整快照
        log.warning("recommendation refresh failed: %s", exc)
        failed = dict(old or {"account": account, "items": []}); failed.update({"status": "stale", "error": str(exc), "attempted_at": datetime.now().isoformat(), "nextAllowedAt": int((time.time() + COOLDOWN) * 1000)})
        save_snapshot(failed); return failed


def refresh_if_due() -> dict | None:
    """启动补偿/每日维护：仅在首次、跨日且本地时间已过 08:00 时请求。"""
    account, token = _credentials(); old = load_snapshot(account)
    if not token: return old
    now = datetime.now(); today = now.date().isoformat()
    generated = str((old or {}).get("generated_at") or "")[:10]
    attempted = str((old or {}).get("attempted_at") or "")[:10]
    if old and generated == today: return old
    if now.hour < 8 and attempted == today: return old
    return refresh(False)


def start_scheduler() -> None:
    if getattr(start_scheduler, "started", False): return
    start_scheduler.started = True
    def loop():
        refresh_if_due()  # 服务启动补偿
        while True:
            now = datetime.now(); target = now.replace(hour=8, minute=0, second=0, microsecond=0)
            if target <= now: target += timedelta(days=1)
            time.sleep(max(1, (target - now).total_seconds()))
            refresh_if_due()
    threading.Thread(target=loop, name="RecommendationScheduler", daemon=True).start()
