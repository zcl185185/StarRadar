"""反馈信号采集（情景记忆层，回答"我做过什么"）。

职责：
- 记录所有交互行为到 SQLite（interactions 表，支持时序查询）
- 行为权重映射（star=+0.10 / dismiss=-0.05 等）
- 周快照生成（兴趣分布，供漂移检测使用）
- 项目缓存表（避免重复调用 GitHub API）

隐式反馈：点击 / 停留时长 / 滚动 / 忽略
显式反馈：👍推荐更多 / ⭐加星 / 📋克隆 / ⑂Fork / 🙅不感兴趣 / 🚫屏蔽

参考：设计文档.md 第 8.2 章；docs/algorithm-personalized-memory.md §1 Layer 2
存储：data/profile/memory.db + data/profile/history.json
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from config import PROFILE_DIR

logger = logging.getLogger(__name__)

MEMORY_DB = PROFILE_DIR / "memory.db"
HISTORY_JSON = PROFILE_DIR / "history.json"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS interactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_full_name TEXT NOT NULL,
    action TEXT NOT NULL,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
    duration_s INTEGER DEFAULT 0,
    scroll_depth REAL DEFAULT 0,
    topics TEXT,
    language TEXT,
    stars_at_interaction INTEGER,
    week_key TEXT
);
CREATE INDEX IF NOT EXISTS idx_interactions_repo ON interactions(repo_full_name);
CREATE INDEX IF NOT EXISTS idx_interactions_action ON interactions(action);
CREATE INDEX IF NOT EXISTS idx_interactions_week ON interactions(week_key);

CREATE TABLE IF NOT EXISTS projects (
    full_name TEXT PRIMARY KEY,
    description TEXT,
    topics TEXT,
    language TEXT,
    stars INTEGER,
    forks INTEGER,
    first_seen DATETIME,
    last_updated DATETIME
);

CREATE TABLE IF NOT EXISTS interest_snapshots (
    week_key TEXT PRIMARY KEY,
    snapshot TEXT,
    topic_distribution TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS survey_profiles (
    uid TEXT PRIMARY KEY,
    survey TEXT NOT NULL,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS daily_snapshots (
    snapshot_date TEXT NOT NULL,
    full_name TEXT NOT NULL,
    stars INTEGER NOT NULL,
    topics TEXT,
    language TEXT,
    week_key TEXT,
    PRIMARY KEY (snapshot_date, full_name)
);
CREATE INDEX IF NOT EXISTS idx_snapshots_week ON daily_snapshots(week_key);

CREATE TABLE IF NOT EXISTS summary_cache (
    full_name TEXT PRIMARY KEY,
    stars_at_generation INTEGER NOT NULL,
    summary TEXT NOT NULL,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS saved_projects (
    full_name TEXT PRIMARY KEY,
    html_url TEXT NOT NULL,
    description TEXT,
    language TEXT,
    stars INTEGER,
    license TEXT,
    topics TEXT,
    recommendation TEXT,
    tag TEXT NOT NULL DEFAULT '待研究',
    note TEXT NOT NULL DEFAULT '',
    saved_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_saved_projects_saved_at ON saved_projects(saved_at DESC);

CREATE TABLE IF NOT EXISTS starred_project_metadata (
    full_name TEXT PRIMARY KEY,
    tags TEXT NOT NULL DEFAULT '[]',
    note TEXT NOT NULL DEFAULT '',
    favorite INTEGER NOT NULL DEFAULT 0,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_starred_metadata_favorite ON starred_project_metadata(favorite DESC, updated_at DESC);

CREATE TABLE IF NOT EXISTS learning_projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    source_url TEXT NOT NULL DEFAULT '',
    source_kind TEXT NOT NULL DEFAULT 'GitHub',
    project_type TEXT NOT NULL DEFAULT '未分类',
    tech_stack TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT '待研究',
    goal TEXT NOT NULL DEFAULT '',
    readme_summary TEXT NOT NULL DEFAULT '',
    analysis TEXT NOT NULL DEFAULT '{}',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_learning_projects_updated ON learning_projects(updated_at DESC);

CREATE TABLE IF NOT EXISTS learning_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL,
    logged_on TEXT NOT NULL,
    duration_minutes INTEGER NOT NULL DEFAULT 0,
    completed TEXT NOT NULL DEFAULT '',
    blockers TEXT NOT NULL DEFAULT '',
    changed_files TEXT NOT NULL DEFAULT '',
    next_step TEXT NOT NULL DEFAULT '',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(project_id) REFERENCES learning_projects(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_learning_logs_project_date ON learning_logs(project_id, logged_on DESC);
"""


# ===== 数据库连接 =====

def _connect() -> sqlite3.Connection:
    MEMORY_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(MEMORY_DB))
    conn.executescript(_SCHEMA)
    return conn


def week_key(dt: datetime | None = None) -> str:
    """ISO 周键：'2026W30'。"""
    dt = dt or datetime.now(timezone.utc)
    iso_year, iso_week, _ = dt.isocalendar()
    return f"{iso_year}W{iso_week:02d}"


# ===== 行为记录 =====

def record_interaction(
    repo_full_name: str,
    action: str,
    *,
    duration_s: int = 0,
    scroll_depth: float = 0.0,
    topics: list[str] | None = None,
    language: str | None = None,
    stars: int | None = None,
    timestamp: datetime | None = None,
) -> None:
    """记录一次交互到 SQLite。"""
    ts = timestamp or datetime.now(timezone.utc)
    try:
        with _connect() as conn:
            conn.execute(
                "INSERT INTO interactions "
                "(repo_full_name, action, timestamp, duration_s, scroll_depth, "
                " topics, language, stars_at_interaction, week_key) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    repo_full_name,
                    action,
                    ts.isoformat(timespec="seconds"),
                    int(duration_s),
                    float(scroll_depth),
                    json.dumps(topics or [], ensure_ascii=False),
                    language,
                    stars,
                    week_key(ts),
                ),
            )
    except sqlite3.Error as e:
        logger.warning("交互记录失败 (%s/%s)：%s", repo_full_name, action, e)


def has_interaction(repo_full_name: str, action: str, ts_iso: str) -> bool:
    """同仓库同动作同秒是否已记录（上报幂等去重）。"""
    try:
        with _connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM interactions "
                "WHERE repo_full_name=? AND action=? AND timestamp=? LIMIT 1",
                (repo_full_name, action, ts_iso),
            ).fetchone()
        return row is not None
    except sqlite3.Error as e:
        logger.warning("幂等检查失败 (%s/%s)：%s", repo_full_name, action, e)
        return False


def log_project(
    repo_full_name: str,
    *,
    description: str | None = None,
    topics: list[str] | None = None,
    language: str | None = None,
    stars: int | None = None,
    forks: int | None = None,
) -> None:
    """缓存项目元数据（避免重复 API 调用）。"""
    try:
        with _connect() as conn:
            conn.execute(
                "INSERT INTO projects (full_name, description, topics, language, "
                "stars, forks, first_seen, last_updated) "
                "VALUES (?,?,?,?,?,?,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP) "
                "ON CONFLICT(full_name) DO UPDATE SET "
                "description=excluded.description, topics=excluded.topics, "
                "language=excluded.language, stars=excluded.stars, "
                "forks=excluded.forks, last_updated=CURRENT_TIMESTAMP",
                (
                    repo_full_name,
                    description,
                    json.dumps(topics or [], ensure_ascii=False),
                    language,
                    stars,
                    forks,
                ),
            )
    except sqlite3.Error as e:
        logger.warning("项目缓存写入失败 (%s)：%s", repo_full_name, e)


# ===== 我的项目库（与 GitHub Star 完全独立，仅存本机） =====

def save_project_to_library(project: dict[str, Any], *, tag: str = "待研究", note: str = "") -> None:
    """保存一个搜索到的项目到本机项目库；重复保存时只更新最新元数据。"""
    full_name = str(project.get("full_name") or "").strip()
    html_url = str(project.get("html_url") or "").strip()
    if not full_name or not html_url:
        raise ValueError("项目数据不完整")
    with _connect() as conn:
        conn.execute(
            "INSERT INTO saved_projects (full_name,html_url,description,language,stars,license,topics,recommendation,tag,note) "
            "VALUES (?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(full_name) DO UPDATE SET html_url=excluded.html_url,description=excluded.description, "
            "language=excluded.language,stars=excluded.stars,license=excluded.license,topics=excluded.topics, "
            "recommendation=excluded.recommendation,tag=excluded.tag,note=excluded.note",
            (full_name, html_url, str(project.get("description") or ""), str(project.get("language") or ""),
             int(project.get("stars") or 0), str(project.get("license") or ""),
             json.dumps(project.get("topics") or [], ensure_ascii=False), str(project.get("recommendation") or ""),
             str(tag or "待研究")[:32], str(note or "")[:1000]),
        )


def list_saved_projects() -> list[dict[str, Any]]:
    """读取个人项目库，按最近收藏排序。"""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT full_name,html_url,description,language,stars,license,topics,recommendation,tag,note,saved_at "
            "FROM saved_projects ORDER BY saved_at DESC"
        ).fetchall()
    keys = ("full_name", "html_url", "description", "language", "stars", "license", "topics", "recommendation", "tag", "note", "saved_at")
    projects = []
    for row in rows:
        item = dict(zip(keys, row))
        try:
            item["topics"] = json.loads(item["topics"] or "[]")
        except json.JSONDecodeError:
            item["topics"] = []
        projects.append(item)
    return projects


def delete_saved_project(full_name: str) -> None:
    """从本机项目库移除项目；不会影响 GitHub Star 或远程仓库。"""
    with _connect() as conn:
        conn.execute("DELETE FROM saved_projects WHERE full_name=?", (full_name,))


# ===== 学习档案（单用户、本机优先） =====

def _safe_json(value: Any, fallback: Any) -> Any:
    try:
        result = json.loads(value or "")
        return result if isinstance(result, type(fallback)) else fallback
    except (TypeError, json.JSONDecodeError):
        return fallback


def _project_from_row(row: tuple[Any, ...]) -> dict[str, Any]:
    keys = ("id", "name", "source_url", "source_kind", "project_type", "tech_stack", "status", "goal", "readme_summary", "analysis", "created_at", "updated_at")
    project = dict(zip(keys, row))
    project["tech_stack"] = _safe_json(project["tech_stack"], [])
    project["analysis"] = _safe_json(project["analysis"], {})
    return project


def list_learning_projects() -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id,name,source_url,source_kind,project_type,tech_stack,status,goal,readme_summary,analysis,created_at,updated_at "
            "FROM learning_projects ORDER BY updated_at DESC,id DESC"
        ).fetchall()
    return [_project_from_row(row) for row in rows]


def get_learning_project(project_id: int) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT id,name,source_url,source_kind,project_type,tech_stack,status,goal,readme_summary,analysis,created_at,updated_at "
            "FROM learning_projects WHERE id=?", (int(project_id),)
        ).fetchone()
    return _project_from_row(row) if row else None


def _clean_project_payload(payload: dict[str, Any], existing: dict[str, Any] | None = None) -> dict[str, Any]:
    def value(key: str, default: str, limit: int) -> str:
        raw = payload.get(key, existing.get(key, default) if existing else default)
        return str(raw or default).strip()[:limit]
    raw_stack = payload.get("tech_stack", existing.get("tech_stack", []) if existing else [])
    if isinstance(raw_stack, str):
        raw_stack = raw_stack.split(",")
    stack = [str(item).strip()[:40] for item in (raw_stack or []) if str(item).strip()][:16]
    return {"name": value("name", "", 120), "source_url": value("source_url", "", 1000),
            "source_kind": value("source_kind", "GitHub", 32), "project_type": value("project_type", "未分类", 64),
            "tech_stack": stack, "status": value("status", "待研究", 32), "goal": value("goal", "", 1200),
            "readme_summary": value("readme_summary", "", 12000)}


def create_learning_project(payload: dict[str, Any]) -> dict[str, Any]:
    project = _clean_project_payload(payload)
    if not project["name"]:
        raise ValueError("请填写项目名称")
    with _connect() as conn:
        cursor = conn.execute(
            "INSERT INTO learning_projects (name,source_url,source_kind,project_type,tech_stack,status,goal,readme_summary) VALUES (?,?,?,?,?,?,?,?)",
            (project["name"], project["source_url"], project["source_kind"], project["project_type"],
             json.dumps(project["tech_stack"], ensure_ascii=False), project["status"], project["goal"], project["readme_summary"]),
        )
    return get_learning_project(int(cursor.lastrowid)) or {}


def update_learning_project(project_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    existing = get_learning_project(project_id)
    if not existing:
        raise ValueError("项目不存在")
    project = _clean_project_payload(payload, existing)
    analysis = payload.get("analysis", existing["analysis"])
    if not isinstance(analysis, dict):
        analysis = existing["analysis"]
    with _connect() as conn:
        conn.execute(
            "UPDATE learning_projects SET name=?,source_url=?,source_kind=?,project_type=?,tech_stack=?,status=?,goal=?,readme_summary=?,analysis=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (project["name"], project["source_url"], project["source_kind"], project["project_type"],
             json.dumps(project["tech_stack"], ensure_ascii=False), project["status"], project["goal"], project["readme_summary"],
             json.dumps(analysis, ensure_ascii=False), int(project_id)),
        )
    return get_learning_project(project_id) or existing


def delete_learning_project(project_id: int) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM learning_logs WHERE project_id=?", (int(project_id),))
        conn.execute("DELETE FROM learning_projects WHERE id=?", (int(project_id),))


def list_learning_logs(project_id: int) -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id,project_id,logged_on,duration_minutes,completed,blockers,changed_files,next_step,created_at "
            "FROM learning_logs WHERE project_id=? ORDER BY logged_on DESC,id DESC", (int(project_id),)
        ).fetchall()
    keys = ("id", "project_id", "logged_on", "duration_minutes", "completed", "blockers", "changed_files", "next_step", "created_at")
    return [dict(zip(keys, row)) for row in rows]


def add_learning_log(project_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    if not get_learning_project(project_id):
        raise ValueError("项目不存在")
    try:
        minutes = max(0, min(int(payload.get("duration_minutes") or 0), 1440))
    except (TypeError, ValueError):
        minutes = 0
    logged_on = str(payload.get("logged_on") or datetime.now().date().isoformat())[:10]
    values = (int(project_id), logged_on, minutes, str(payload.get("completed") or "")[:2000],
              str(payload.get("blockers") or "")[:2000], str(payload.get("changed_files") or "")[:2000],
              str(payload.get("next_step") or "")[:2000])
    with _connect() as conn:
        cursor = conn.execute(
            "INSERT INTO learning_logs (project_id,logged_on,duration_minutes,completed,blockers,changed_files,next_step) VALUES (?,?,?,?,?,?,?)", values
        )
        conn.execute("UPDATE learning_projects SET updated_at=CURRENT_TIMESTAMP WHERE id=?", (int(project_id),))
    return {"id": int(cursor.lastrowid), "project_id": values[0], "logged_on": values[1], "duration_minutes": values[2],
            "completed": values[3], "blockers": values[4], "changed_files": values[5], "next_step": values[6]}


def learning_dashboard() -> dict[str, Any]:
    projects = list_learning_projects()
    with _connect() as conn:
        minutes, logs = conn.execute(
            "SELECT COALESCE(SUM(duration_minutes),0),COUNT(*) FROM learning_logs WHERE logged_on >= date('now','-29 days')"
        ).fetchone()
    stacks: dict[str, int] = {}; statuses: dict[str, int] = {}
    for project in projects:
        statuses[project["status"]] = statuses.get(project["status"], 0) + 1
        for stack in project["tech_stack"]:
            stacks[stack] = stacks.get(stack, 0) + 1
    return {"project_count": len(projects), "in_progress": statuses.get("研究中", 0) + statuses.get("复刻中", 0),
            "recent_minutes": int(minutes or 0), "recent_logs": int(logs or 0), "stack_distribution": sorted(stacks.items(), key=lambda item: (-item[1], item[0]))[:8],
            "status_distribution": statuses}


# ===== GitHub 星标管理（只存本机，不改 GitHub 原始星标） =====

def list_starred_metadata() -> dict[str, dict[str, Any]]:
    """Return locally managed tags, notes, and favorites keyed by repository."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT full_name,tags,note,favorite,updated_at FROM starred_project_metadata"
        ).fetchall()
    items: dict[str, dict[str, Any]] = {}
    for full_name, tags, note, favorite, updated_at in rows:
        try:
            parsed_tags = json.loads(tags or "[]")
        except json.JSONDecodeError:
            parsed_tags = []
        if not isinstance(parsed_tags, list):
            parsed_tags = []
        items[str(full_name)] = {
            "tags": [str(tag)[:32] for tag in parsed_tags if str(tag).strip()][:12],
            "note": str(note or "")[:1000],
            "favorite": bool(favorite),
            "updated_at": str(updated_at or ""),
        }
    return items


def save_starred_metadata(
    full_name: str, *, tags: list[str] | None = None, note: str = "", favorite: bool = False,
) -> dict[str, Any]:
    """Upsert local metadata for a GitHub-starred repository.

    The repository itself stays on GitHub. This is deliberately a local-only
    layer so a browser login never has to grant more than normal star access.
    """
    name = str(full_name or "").strip()
    if not name or "/" not in name or len(name) > 256:
        raise ValueError("仓库名称无效")
    clean_tags: list[str] = []
    for tag in tags or []:
        cleaned = str(tag or "").strip()[:32]
        if cleaned and cleaned not in clean_tags:
            clean_tags.append(cleaned)
        if len(clean_tags) >= 12:
            break
    clean_note = str(note or "").strip()[:1000]
    with _connect() as conn:
        conn.execute(
            "INSERT INTO starred_project_metadata (full_name,tags,note,favorite,updated_at) "
            "VALUES (?,?,?,?,CURRENT_TIMESTAMP) "
            "ON CONFLICT(full_name) DO UPDATE SET tags=excluded.tags,note=excluded.note, "
            "favorite=excluded.favorite,updated_at=CURRENT_TIMESTAMP",
            (name, json.dumps(clean_tags, ensure_ascii=False), clean_note, int(bool(favorite))),
        )
    return {"tags": clean_tags, "note": clean_note, "favorite": bool(favorite)}


# ===== 问卷档案（前端上报，供冷启动画像） =====

def save_survey(uid: str, survey: dict[str, Any]) -> None:
    """保存/更新一份问卷档案（uid 为前端匿名标识）。"""
    try:
        with _connect() as conn:
            conn.execute(
                "INSERT INTO survey_profiles (uid, survey, updated_at) "
                "VALUES (?,?,CURRENT_TIMESTAMP) "
                "ON CONFLICT(uid) DO UPDATE SET "
                "survey=excluded.survey, updated_at=CURRENT_TIMESTAMP",
                (uid, json.dumps(survey, ensure_ascii=False)),
            )
    except sqlite3.Error as e:
        logger.warning("问卷保存失败 (%s)：%s", uid, e)


def load_latest_survey() -> dict[str, Any] | None:
    """读取最近提交的问卷档案（按更新时间倒序）。"""
    try:
        with _connect() as conn:
            row = conn.execute(
                "SELECT survey FROM survey_profiles ORDER BY updated_at DESC LIMIT 1"
            ).fetchone()
        if not row:
            return None
        data = json.loads(row[0])
        return data if isinstance(data, dict) else None
    except (sqlite3.Error, json.JSONDecodeError) as e:
        logger.warning("问卷读取失败：%s", e)
        return None


# ===== 每日快照（周报对比数据源） =====

def save_daily_snapshot(
    full_name: str,
    stars: int,
    *,
    topics: list[str] | None = None,
    language: str | None = None,
    snapshot_date: str | None = None,
) -> None:
    """记录某项目在某日的 star 快照（upsert，供每周趋势对比）。"""
    date = snapshot_date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        ts = datetime.fromisoformat(date + "T00:00:00+00:00")
    except ValueError:
        ts = datetime.now(timezone.utc)
    wk = week_key(ts)
    try:
        with _connect() as conn:
            conn.execute(
                "INSERT INTO daily_snapshots "
                "(snapshot_date, full_name, stars, topics, language, week_key) "
                "VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(snapshot_date, full_name) DO UPDATE SET "
                "stars=excluded.stars, topics=excluded.topics, "
                "language=excluded.language, week_key=excluded.week_key",
                (date, full_name, int(stars),
                 json.dumps(topics or [], ensure_ascii=False),
                 language, wk),
            )
    except sqlite3.Error as e:
        logger.warning("每日快照写入失败 (%s/%s)：%s", date, full_name, e)


def load_week_snapshots(week: str | None = None) -> dict[str, dict[str, Any]]:
    """加载某周（默认本周）所有项目快照：{full_name: {stars, topics, language, date}}。

    同项目多日记录时取最后一次（当周最新状态）。
    """
    wk = week or week_key()
    try:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT full_name, stars, topics, language, snapshot_date "
                "FROM daily_snapshots WHERE week_key=? "
                "ORDER BY snapshot_date ASC",
                (wk,),
            ).fetchall()
    except sqlite3.Error as e:
        logger.warning("周快照读取失败 (%s)：%s", wk, e)
        return {}
    out: dict[str, dict[str, Any]] = {}
    for full_name, stars, topics, language, date in rows:
        try:
            topics_list = json.loads(topics or "[]")
        except json.JSONDecodeError:
            topics_list = []
        out[full_name] = {
            "stars": int(stars),
            "topics": topics_list,
            "language": language,
            "date": date,
        }
    return out


def list_snapshot_weeks(limit: int = 12) -> list[str]:
    """按时间倒序列出有快照数据的周。"""
    try:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT week_key FROM daily_snapshots "
                "ORDER BY week_key DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [r[0] for r in rows]
    except sqlite3.Error as e:
        logger.warning("周列表读取失败：%s", e)
        return []


def load_week_daily_totals(week: str) -> list[dict[str, Any]]:
    """某周每日快照汇总（按日升序）：[{date, count, total}, ...]。

    供周报 timeline（本周逐日热度走势）使用。
    """
    try:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT snapshot_date, COUNT(*) AS c, SUM(stars) AS s "
                "FROM daily_snapshots WHERE week_key=? "
                "GROUP BY snapshot_date ORDER BY snapshot_date",
                (week,),
            ).fetchall()
    except sqlite3.Error as e:
        logger.warning("每日汇总读取失败 (%s)：%s", week, e)
        return []
    return [
        {"date": date[5:], "count": int(c), "total": int(s or 0)}
        for date, c, s in rows
    ]


# ===== LLM 解读缓存（每日增量：已解读项目不重复调用） =====

def get_cached_summary(full_name: str, stars: int) -> str | None:
    """读取缓存的 LLM 解读；星数变化超过 20% 视为过期（项目明显变化需重读）。"""
    try:
        with _connect() as conn:
            row = conn.execute(
                "SELECT stars_at_generation, summary FROM summary_cache "
                "WHERE full_name=?",
                (full_name,),
            ).fetchone()
    except sqlite3.Error as e:
        logger.warning("解读缓存读取失败 (%s)：%s", full_name, e)
        return None
    if not row:
        return None
    cached_stars, summary = row
    if stars and cached_stars and abs(stars - cached_stars) / max(1, cached_stars) > 0.2:
        return None
    return summary


def set_cached_summary(full_name: str, stars: int, summary: str) -> None:
    """写入/更新 LLM 解读缓存。"""
    try:
        with _connect() as conn:
            conn.execute(
                "INSERT INTO summary_cache (full_name, stars_at_generation, summary) "
                "VALUES (?,?,?) "
                "ON CONFLICT(full_name) DO UPDATE SET "
                "stars_at_generation=excluded.stars_at_generation, "
                "summary=excluded.summary, updated_at=CURRENT_TIMESTAMP",
                (full_name, int(stars), summary),
            )
    except sqlite3.Error as e:
        logger.warning("解读缓存写入失败 (%s)：%s", full_name, e)


# ===== 查询 =====

def query_interactions(
    *,
    action: str | None = None,
    language: str | None = None,
    since_days: int | None = None,
    repo_full_name: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """查询交互历史（支持条件过滤）。"""
    sql = "SELECT * FROM interactions WHERE 1=1"
    args: list[Any] = []
    if action:
        sql += " AND action = ?"
        args.append(action)
    if language:
        sql += " AND language = ?"
        args.append(language)
    if since_days:
        cutoff = datetime.now(timezone.utc) - timedelta(days=since_days)
        sql += " AND timestamp >= ?"
        args.append(cutoff.isoformat())
    if repo_full_name:
        sql += " AND repo_full_name = ?"
        args.append(repo_full_name)
    sql += " ORDER BY timestamp DESC LIMIT ?"
    args.append(limit)
    try:
        with _connect() as conn:
            rows = conn.execute(sql, args).fetchall()
        cols = [d[0] for d in conn.execute("SELECT * FROM interactions LIMIT 0").description]
        return [dict(zip(cols, row)) for row in rows]
    except sqlite3.Error as e:
        logger.warning("交互查询失败：%s", e)
        return []


def topic_distribution(since_days: int = 7) -> dict[str, float]:
    """近 N 天主题分布（用于漂移检测 / 周快照）。"""
    dist: dict[str, float] = {}
    for row in query_interactions(since_days=since_days, limit=1000):
        try:
            topics = json.loads(row.get("topics") or "[]")
        except json.JSONDecodeError:
            topics = []
        for t in topics:
            dist[t] = dist.get(t, 0.0) + 1.0
    return dist


# ===== 周快照 =====

def save_weekly_snapshot(profile_data: dict[str, Any]) -> None:
    """生成本周兴趣快照（供漂移检测）。"""
    wk = week_key()
    try:
        with _connect() as conn:
            conn.execute(
                "INSERT INTO interest_snapshots (week_key, snapshot, topic_distribution) "
                "VALUES (?,?,?) "
                "ON CONFLICT(week_key) DO UPDATE SET "
                "snapshot=excluded.snapshot, topic_distribution=excluded.topic_distribution",
                (
                    wk,
                    json.dumps(profile_data, ensure_ascii=False),
                    json.dumps(topic_distribution(7), ensure_ascii=False),
                ),
            )
    except sqlite3.Error as e:
        logger.warning("周快照写入失败：%s", e)


def load_snapshots(limit: int = 16) -> list[tuple[str, dict[str, float]]]:
    """按时间升序加载最近 N 个周快照 [(week_key, topic_distribution)]。"""
    try:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT week_key, topic_distribution FROM interest_snapshots "
                "ORDER BY week_key ASC LIMIT ?",
                (limit,),
            ).fetchall()
        out: list[tuple[str, dict[str, float]]] = []
        for wk, dist_json in rows:
            try:
                out.append((wk, json.loads(dist_json or "{}")))
            except json.JSONDecodeError:
                continue
        return out
    except sqlite3.Error as e:
        logger.warning("周快照读取失败：%s", e)
        return []


# ===== JSON 历史（轻量兜底，无 SQLite 依赖场景） =====

def append_history_json(entry: dict[str, Any]) -> None:
    """追加一条历史记录到 history.json（结构：{history: [...]}）。"""
    try:
        if HISTORY_JSON.exists():
            data = json.loads(HISTORY_JSON.read_text(encoding="utf-8"))
        else:
            data = {"history": []}
        data.setdefault("history", []).append(entry)
        # 只保留最近 1000 条
        data["history"] = data["history"][-1000:]
        HISTORY_JSON.parent.mkdir(parents=True, exist_ok=True)
        HISTORY_JSON.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("history.json 追加失败：%s", e)


def summarize_history(since_days: int = 30) -> dict[str, Any]:
    """汇总近期行为（供推荐解释 / LLM 上下文）。"""
    rows = query_interactions(since_days=since_days, limit=1000)
    counts: dict[str, int] = {}
    langs: dict[str, int] = {}
    repos: set[str] = set()
    for r in rows:
        counts[r.get("action", "?")] = counts.get(r.get("action", "?"), 0) + 1
        if r.get("language"):
            langs[r["language"]] = langs.get(r["language"], 0) + 1
        if r.get("repo_full_name"):
            repos.add(r["repo_full_name"])
    return {
        "total_interactions": len(rows),
        "by_action": counts,
        "top_languages": sorted(langs.items(), key=lambda x: -x[1])[:5],
        "unique_repos": len(repos),
    }
