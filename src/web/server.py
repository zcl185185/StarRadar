"""StarRadar 本地 Web 服务：静态托管 + 行为信号接收（纯标准库）。

运行：
    python src/main.py --serve                # 端口 8970
    python src/main.py --serve --port 8080

API：
    GET  /api/health     → {"ok": true}                  （前端健康门控）
    GET  /api/stats      → 近 30 天行为汇总               （验证用）
    POST /api/events     → 批量行为上报 {"uid","events"} → {"accepted","duplicates"}
    POST /api/gh/device  → 转发 GitHub Device Flow 授权码请求（同上 token 轮询）
    POST /api/idea-search → 自然语言实时搜索 GitHub 仓库（仅本地服务）
    GET  /api/github/starred → 已登录账户的 GitHub 星标列表（仅本地服务）
    GET/POST /api/library → 我的项目库读取 / 收藏（仅存本机，不影响 GitHub Star）
    GET/POST /api/starred-meta → 星标标签、笔记、收藏状态（仅存本机）
    GET /api/recommendations → 当前账号的推荐快照与查看笔记
    POST /api/recommendations/refresh → 手动换一批推荐
    POST /api/recommendations/reviews → 保存推荐查看状态和笔记
    GET  /api/personal/history → 每日发现归档目录 / 单次快照（仅本机）
    GET  /api/trending → GitHub Trending 今日/本周/本月榜（读取公开趋势页）

行为上报字段（来自前端 logAction）：
    repo, action, ts(ISO), topics[], language, owner, duration_s, stars, forks
CORS 全放开，GitHub Pages 静态站可跨域上报。
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qs, unquote, urlencode
from urllib.request import Request, urlopen

from config import IS_FROZEN, PERSONAL_DIR, PROFILE_DIR, STATIC_DIR, settings
from src.personal.daily_archive import list_daily_archives, load_daily_archive
from src.trending import fetch_trending
from src.trending import translate_descriptions
from src.profile.feedback_collector import (
    has_interaction,
    list_saved_projects,
    list_starred_metadata,
    load_latest_survey,
    log_project,
    record_interaction,
    save_project_to_library,
    save_starred_metadata,
    save_survey,
    summarize_history,
)
from src.recommendations import load_reviews, load_snapshot, refresh as refresh_recommendations, save_review

logger = logging.getLogger(__name__)

MIME_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".webp": "image/webp",
    ".txt": "text/plain; charset=utf-8",
}

_ACTION_WHITELIST = {
    "click", "click_deep", "click_short", "like", "dismiss",
    "star", "unstar", "fork", "clone", "note", "block",
}
_MAX_EVENTS_PER_BATCH = 200

# ===== OAuth 跳转登录（Authorization Code Flow，本地 server 模式） =====
_OAUTH_STATE_TTL = 300      # state 5 分钟过期
_OAUTH_TICKET_TTL = 60      # 一次性票据 60 秒过期
_GH_AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
_GH_TOKEN_URL = "https://github.com/login/oauth/access_token"
_oauth_states: dict = {}    # state -> (ts, redirect_uri)
_oauth_tickets: dict = {}   # ticket -> (ts, access_token)
_oauth_lock = threading.Lock()

# ===== 本地访问码门禁 =====
_ACCESS_SESSION_TTL = 12 * 3600
_access_sessions: dict[str, float] = {}
_access_lock = threading.Lock()

# ===== 想法搜索：全局并发闸 + 同查询结果缓存 =====
# 一次搜索最多消耗 15 个 GitHub 配额 + 2 次 LLM 调用；全局同时只放行一笔，
# 相同 query+filters 短窗口内直接复用结果，避免连点/重复提交烧尽配额。
_idea_gate = threading.Semaphore(1)
_idea_result_cache: dict[str, tuple[float, dict]] = {}
_IDEA_RESULT_TTL = 60    # 同一查询 60 秒内直接复用结果
_IDEA_DEADLINE = 45      # 单笔搜索总闸（秒），超时先回降级结果


def _oauth_configured() -> bool:
    return bool(settings.oauth.client_id and settings.oauth.client_secret)


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _github_starred_page(page: int, per_page: int) -> dict:
    """Fetch one safe, display-ready page of the local user's GitHub stars."""
    token_path = PROFILE_DIR / "gh_token.json"
    try:
        login = json.loads(token_path.read_text(encoding="utf-8"))
        token = str(login.get("token") or "").strip()
    except (OSError, json.JSONDecodeError, AttributeError) as exc:
        raise RuntimeError("未找到本机 GitHub 登录态，请先登录") from exc
    if not token:
        raise RuntimeError("本机 GitHub 登录凭据无效，请重新登录")

    page = max(1, min(int(page), 1000))
    per_page = max(1, min(int(per_page), 100))
    query = urlencode({"per_page": per_page, "page": page, "sort": "created", "direction": "desc"})
    request = Request(
        "https://api.github.com/user/starred?" + query,
        headers={
            "Authorization": "Bearer " + token,
            "Accept": "application/vnd.github.star+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "StarRadar",
        },
    )
    try:
        with urlopen(request, timeout=20) as response:
            raw_items = json.loads(response.read().decode("utf-8"))
            link_header = response.headers.get("Link", "")
    except HTTPError as exc:
        if exc.code in (401, 403):
            raise RuntimeError("GitHub 登录已失效或无权读取星标，请重新登录") from exc
        raise RuntimeError(f"GitHub 读取星标失败（HTTP {exc.code}）") from exc
    except Exception as exc:  # noqa: BLE001 - map network errors to a safe client message
        raise RuntimeError("GitHub 星标暂时无法读取，请稍后再试") from exc

    if not isinstance(raw_items, list):
        raise RuntimeError("GitHub 星标响应格式异常")
    items = []
    for entry in raw_items:
        if not isinstance(entry, dict):
            continue
        # GitHub's star media type returns {starred_at, repo}; the ordinary
        # media type returns the repository fields directly.
        repo = entry.get("repo") if isinstance(entry.get("repo"), dict) else entry
        if not repo.get("full_name"):
            continue
        items.append({
            "full_name": str(repo.get("full_name") or ""),
            "html_url": str(repo.get("html_url") or ""),
            "description": str(repo.get("description") or "暂无项目描述。"),
            "stars": int(repo.get("stargazers_count") or 0),
            "language": str(repo.get("language") or "未标注"),
            "topics": [str(topic) for topic in (repo.get("topics") or [])[:8]],
            "starred_at": str(entry.get("starred_at") or repo.get("starred_at") or ""),
            "updated_at": str(repo.get("pushed_at") or ""),
            "created_at": str(repo.get("created_at") or ""),
            "archived": bool(repo.get("archived")),
        })
    return {"ok": True, "items": items, "page": page, "per_page": per_page,
            "has_next": 'rel="next"' in link_header}


# ===== 个人版自动生成（后台调度） =====
# 目标：用户只需开着 --serve，数据每天自动更新（启动补跑 + 每日 06:00 定时），
# 也可通过 POST /api/personal/refresh 手动触发。生成在子进程异步执行，不阻塞服务。
_PERSONAL_DIR = PERSONAL_DIR
_personal_proc: subprocess.Popen | None = None
_personal_lock = threading.Lock()


def _load_local_llm_config() -> bool:
    """让本机页面保存的 LLM 配置也能服务 Trending 的后端翻译。"""
    config_path = PROFILE_DIR / "llm_config.json"
    if not config_path.is_file():
        return False
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    key = str(config.get("key") or "").strip()
    if not key:
        return False
    settings.llm.api_key = key
    settings.llm.enabled = True
    if config.get("base_url"):
        settings.llm.base_url = str(config["base_url"])
    if config.get("model"):
        settings.llm.model = str(config["model"])
    return True


def _personal_scores_gen_date() -> str:
    """个人雷达数据最后生成日期（YYYY-MM-DD），无数据返回空串。"""
    f = _PERSONAL_DIR / "scores.json"
    if not f.is_file():
        return ""
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
        return str(data.get("generated_at") or "")[:10]
    except (json.JSONDecodeError, OSError):
        return ""


def _spawn_personal() -> None:
    """子进程异步跑个人管道（不阻塞服务）；日志写 data/profile/personal_run.log。"""
    global _personal_proc
    with _personal_lock:
        if _personal_proc and _personal_proc.poll() is None:
            return  # 已在跑，防并发
        from config import PROFILE_DIR

        logf = open(PROFILE_DIR / "personal_run.log", "a", encoding="utf-8")
        if IS_FROZEN:
            # 打包版的 sys.executable 是 StarRadar.exe，而不是 Python 解释器。
            # 显式 worker 参数让 desktop.py 仅运行个人管道，不再创建第二个窗口。
            command = [sys.executable, "--personal-worker"]
            cwd = None
        else:
            root = Path(__file__).resolve().parent.parent.parent
            command = [sys.executable, "src/main.py", "--personal"]
            cwd = root
        _personal_proc = subprocess.Popen(
            command,
            cwd=cwd,
            stdout=logf,
            stderr=subprocess.STDOUT,
        )


def _maybe_run_personal(force: bool = False) -> bool:
    """触发个人管道（条件：已登录 + [force 或 今天未生成]）。返回是否触发。"""
    from config import PROFILE_DIR

    if not (PROFILE_DIR / "gh_token.json").is_file():
        return False  # 未登录不触发
    if not force and _personal_scores_gen_date() == datetime.now(timezone.utc).strftime("%Y-%m-%d"):
        return False  # 今天已生成
    _spawn_personal()
    return True


def _personal_scheduler() -> None:
    """后台线程：每小时检查，当日 06:00 后若今天还没生成 → 自动跑个人管道。"""
    while True:
        try:
            hour = datetime.now().hour
            if hour >= 6:
                _maybe_run_personal(force=False)
        except Exception:  # noqa: BLE001
            pass
        time.sleep(3600)


def _start_personal_scheduler() -> None:
    threading.Thread(target=_personal_scheduler, daemon=True).start()
    try:
        _maybe_run_personal(force=False)  # 启动即检查补跑（子进程异步，不阻塞）
    except Exception:  # noqa: BLE001
        pass


class StarRadarHandler(BaseHTTPRequestHandler):
    server_version = "StarRadar/0.1"

    # ===== 基础 =====

    def log_message(self, fmt: str, *args) -> None:  # 精简日志
        logger.info("[%s] %s", self.address_string(), fmt % args)

    def _cors(self) -> None:
        """CORS 响应头：配置了 CORS_ORIGINS 白名单则按 Origin 校验，否则全放开。"""
        origins = settings.cors_origins
        if origins:
            origin = self.headers.get("Origin") or ""
            if origin in origins:
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")
        else:
            self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _bad(self, msg: str) -> None:
        self._json(400, {"ok": False, "error": msg})

    def _is_authorized(self) -> bool:
        """检查当前浏览器的临时本地会话；服务重启后会重新要求输入访问码。"""
        if not settings.access_required:
            return True
        match = re.search(r"(?:^|;\s*)starradar_access=([A-Za-z0-9_-]+)", self.headers.get("Cookie") or "")
        if not match:
            return False
        token = match.group(1)
        now = time.time()
        with _access_lock:
            expires = _access_sessions.get(token, 0)
            if expires <= now:
                _access_sessions.pop(token, None)
                return False
            return True

    def _unauthorized(self) -> None:
        self._json(401, {"ok": False, "error": "请先输入访问码"})

    def _access_status(self) -> None:
        self._json(200, {"ok": True, "authorized": self._is_authorized()})

    def _access_login(self) -> None:
        payload = self._read_json_body()
        if payload is None:
            return
        code = str(payload.get("code") or "")
        if not secrets.compare_digest(code, settings.access_code):
            self._json(401, {"ok": False, "error": "访问码不正确"})
            return
        token = secrets.token_urlsafe(32)
        with _access_lock:
            _access_sessions[token] = time.time() + _ACCESS_SESSION_TTL
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Set-Cookie", f"starradar_access={token}; Path=/; HttpOnly; SameSite=Strict")
        self._cors()
        self.end_headers()
        self.wfile.write(b'{"ok":true,"authorized":true}')

    # ===== 方法分发 =====

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self) -> None:
        raw = unquote(self.path)
        path = raw.split("?", 1)[0]
        query = parse_qs(raw.split("?", 1)[1]) if "?" in raw else {}
        if path == "/api/access/status":
            self._access_status()
        elif path.startswith("/api/") and not self._is_authorized():
            self._unauthorized()
        elif path == "/api/health":
            self._json(200, {"ok": True, "ts": _iso_now()})
        elif path == "/api/stats":
            self._json(200, summarize_history(30))
        elif path == "/api/oauth/start":
            self._oauth_start(query)
        elif path == "/api/oauth/callback":
            self._oauth_callback(query)
        elif path == "/api/oauth/ticket":
            self._oauth_ticket(query)
        elif path == "/api/personal/status":
            self._personal_status()
        elif path == "/api/survey/latest":
            self._survey_latest()
        elif path == "/api/personal/scores":
            self._personal_scores()
        elif path == "/api/personal/history":
            self._personal_history(query)
        elif path == "/api/personal/trends":
            self._personal_trends()
        elif path == "/api/trending":
            self._trending(query)
        elif path == "/api/library":
            self._library_list()
        elif path == "/api/github/starred":
            self._github_starred(query)
        elif path == "/api/starred-meta":
            self._starred_metadata_list()
        elif path == "/api/recommendations":
            self._recommendations_get()
        else:
            self._serve_static(path)

    def do_POST(self) -> None:
        path = unquote(self.path.split("?", 1)[0])
        if path == "/api/access":
            self._access_login()
            return
        if not self._is_authorized():
            self._unauthorized()
            return
        if path == "/api/survey":
            self._receive_survey()
            return
        if path == "/api/gh/device":
            self._gh_proxy(
                "https://github.com/login/device/code",
                need=("client_id",),
                optional=("scope",),
            )
            return
        if path == "/api/gh/token":
            self._gh_proxy(
                "https://github.com/login/oauth/access_token",
                need=("client_id", "device_code", "grant_type"),
                optional=(),
            )
            return
        if path == "/api/gh_token":
            self._save_gh_token()
            return
        if path == "/api/llm_key":
            self._save_llm_key()
            return
        if path == "/api/personal/refresh":
            started = _maybe_run_personal(force=True)
            self._json(200, {"ok": True, "started": started})
            return
        if path == "/api/recommendations/refresh":
            force = parse_qs(self.path.split("?", 1)[1]).get("force", ["0"])[0] == "1" if "?" in self.path else False
            self._json(200, {"ok": True, "snapshot": refresh_recommendations(force)})
            return
        if path == "/api/recommendations/reviews":
            payload = self._read_json_body()
            if payload is None: return
            name = str(payload.get("full_name") or "").strip()
            if not name or "/" not in name:
                self._bad("missing full_name"); return
            self._json(200, {"ok": True, "review": save_review(name, str(payload.get("status") or "seen"), str(payload.get("note") or ""))})
            return
        if path == "/api/translate-descriptions":
            payload = self._read_json_body(max_bytes=1 << 20)
            if payload is None: return
            items = payload.get("items") if isinstance(payload.get("items"), list) else []
            items = [x for x in items if isinstance(x, dict)][:50]
            try:
                _load_local_llm_config(); translated = translate_descriptions(items)
                self._json(200, {"ok": True, "items": items, "translated": bool(translated)})
            except Exception as exc:  # noqa: BLE001
                logger.warning("description translation failed: %s", exc)
                self._json(200, {"ok": True, "items": items, "translated": False})
            return
        if path == "/api/idea-search":
            self._idea_search()
            return
        if path == "/api/library":
            self._library_save()
            return
        if path == "/api/starred-meta":
            self._starred_metadata_save()
            return
        if path != "/api/events":
            self._bad("not found")
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            self._bad("bad content-length")
            return
        if length <= 0 or length > 1 << 20:  # 1MB 上限
            self._bad("payload too large")
            return
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._bad("invalid json")
            return
        events = payload.get("events")
        if not isinstance(events, list) or not events:
            self._bad("empty events")
            return
        events = events[:_MAX_EVENTS_PER_BATCH]
        accepted = duplicates = rejected = 0
        for ev in events:
            if not isinstance(ev, dict):
                rejected += 1
                continue
            repo = str(ev.get("repo") or "").strip()
            action = str(ev.get("action") or "").strip()
            if not repo or action not in _ACTION_WHITELIST:
                rejected += 1
                continue
            ts_raw = str(ev.get("ts") or "")
            try:
                ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
            except ValueError:
                ts = datetime.now(timezone.utc)
            ts_iso = ts.astimezone(timezone.utc).isoformat(timespec="seconds")
            if has_interaction(repo, action, ts_iso):
                duplicates += 1
                continue
            topics = ev.get("topics")
            if not isinstance(topics, list):
                topics = [t for t in str(topics or "").split(",") if t]
            record_interaction(
                repo,
                action,
                duration_s=int(ev.get("duration_s") or 0),
                topics=[str(t) for t in topics[:8]],
                language=str(ev.get("language") or "") or None,
                stars=int(ev.get("stars") or 0) or None,
                timestamp=ts,
            )
            if ev.get("stars") or ev.get("forks"):
                log_project(
                    repo,
                    description=None,
                    topics=[str(t) for t in topics[:8]],
                    language=str(ev.get("language") or "") or None,
                    stars=int(ev.get("stars") or 0) or None,
                    forks=int(ev.get("forks") or 0) or None,
                )
            accepted += 1
        self._json(200, {"ok": True, "accepted": accepted,
                         "duplicates": duplicates, "rejected": rejected})

    def _gh_proxy(self, url: str, *, need: tuple, optional: tuple) -> None:
        """转发 GitHub OAuth 端点（固定目标地址，不透传任意 URL，防 SSRF）。"""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            self._bad("bad content-length")
            return
        if length <= 0 or length > 1 << 16:
            self._bad("payload too large")
            return
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._bad("invalid json")
            return
        if not isinstance(payload, dict):
            self._bad("payload must be object")
            return
        body = {}
        for key in need:
            val = payload.get(key)
            if not isinstance(val, str) or not val:
                self._bad(f"missing field: {key}")
                return
            body[key] = val.strip()
        for key in optional:
            val = payload.get(key)
            if isinstance(val, str) and val.strip():
                body[key] = val.strip()
        try:
            req = Request(
                url,
                data=json.dumps(body).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "User-Agent": "StarRadar",
                },
                method="POST",
            )
            with urlopen(req, timeout=10) as resp:
                data = resp.read(1 << 16)
                logger.info("gh proxy %s → %s: %s", url, resp.status, data[:160])
        except Exception as exc:  # noqa: BLE001
            logger.warning("gh proxy %s failed: %s", url, exc)
            self._json(502, {"ok": False, "error": "upstream failed"})
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self._cors()
        self.end_headers()
        self.wfile.write(data)

    def do_DELETE(self) -> None:
        path = unquote(self.path.split("?", 1)[0]); query = parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
        if not self._is_authorized():
            self._unauthorized()
            return
        if path == "/api/gh_token":
            from config import PROFILE_DIR
            f = PROFILE_DIR / "gh_token.json"
            try:
                if f.is_file():
                    f.unlink()
            except OSError:
                pass
            self._json(200, {"ok": True, "logged_out": True})
            return
        if path == "/api/llm_key":
            from config import PROFILE_DIR
            f = PROFILE_DIR / "llm_config.json"
            try:
                if f.is_file():
                    f.unlink()
            except OSError:
                pass
            self._json(200, {"ok": True, "cleared": True})
            return
        if path == "/api/library":
            self._library_delete()
            return
        if path == "/api/recommendations":
            from src.recommendations import load_snapshot, save_snapshot
            name = (query.get("full_name") or [""])[0]; snap = load_snapshot()
            if snap and name:
                snap["items"] = [x for x in snap.get("items", []) if x.get("full_name") != name]; save_snapshot(snap)
            self._json(200, {"ok": True})
            return
        self._json(404, {"ok": False, "error": "not found"})

    def _read_json_body(self, max_bytes: int = 1 << 16) -> dict | None:
        """读取小型 JSON 请求体；错误响应由调用方直接返回。"""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            self._bad("bad content-length")
            return None
        if length <= 0 or length > max_bytes:
            self._bad("payload too large")
            return None
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._bad("invalid json")
            return None
        if not isinstance(payload, dict):
            self._bad("payload must be object")
            return None
        return payload

    # ===== 想法搜索 + 本机项目库 =====

    def _idea_search(self) -> None:
        payload = self._read_json_body()
        if payload is None:
            return
        query = str(payload.get("query") or "").strip()
        if not query or len(query) > 500:
            self._bad("请输入 1-500 字的搜索需求")
            return
        filters = payload.get("filters") if isinstance(payload.get("filters"), dict) else {}

        # 相同 query+filters 短窗口内直接回缓存：连点/手滑重复搜索零成本。
        cache_key = hashlib.sha1(
            json.dumps({"q": query, "f": filters}, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        hit = _idea_result_cache.get(cache_key)
        if hit and time.time() - hit[0] < _IDEA_RESULT_TTL:
            self._json(200, hit[1])
            return
        # 全局信号量非阻塞获取：拿不到立即 429，请求不排队堆积。
        if not _idea_gate.acquire(blocking=False):
            self._json(429, {"ok": False, "error": "已有一笔搜索在进行中，稍等几秒再试"})
            return
        try:
            from src.idea_search import search_ideas
            result = search_ideas(query, filters=filters, deadline=time.time() + _IDEA_DEADLINE)
        except ValueError as exc:
            self._bad(str(exc))
            return
        except Exception as exc:  # noqa: BLE001 - 上游 API 错误不应杀掉本地服务
            logger.warning("idea search failed: %s", exc)
            self._json(502, {"ok": False, "error": "GitHub 搜索暂时不可用，请稍后再试"})
            return
        finally:
            _idea_gate.release()
        if len(_idea_result_cache) > 32:
            _idea_result_cache.clear()
        _idea_result_cache[cache_key] = (time.time(), result)
        self._json(200, result)

    def _library_save(self) -> None:
        payload = self._read_json_body()
        if payload is None:
            return
        project = payload.get("project")
        if not isinstance(project, dict):
            self._bad("missing project")
            return
        try:
            save_project_to_library(
                project,
                tag=str(payload.get("tag") or "待研究"),
                note=str(payload.get("note") or ""),
            )
            self._json(200, {"ok": True, "saved": project.get("full_name")})
        except (ValueError, TypeError) as exc:
            self._bad(str(exc))
        except Exception as exc:  # noqa: BLE001
            logger.warning("library save failed: %s", exc)
            self._json(500, {"ok": False, "error": "保存失败"})

    def _library_list(self) -> None:
        try:
            self._json(200, {"ok": True, "items": list_saved_projects()})
        except Exception as exc:  # noqa: BLE001
            logger.warning("library list failed: %s", exc)
            self._json(500, {"ok": False, "error": "读取项目库失败"})

    def _starred_metadata_list(self) -> None:
        try:
            self._json(200, {"ok": True, "items": list_starred_metadata()})
        except Exception as exc:  # noqa: BLE001
            logger.warning("starred metadata list failed: %s", exc)
            self._json(500, {"ok": False, "error": "读取星标管理数据失败"})

    def _starred_metadata_save(self) -> None:
        payload = self._read_json_body()
        if payload is None:
            return
        full_name = str(payload.get("full_name") or "")
        raw_tags = payload.get("tags") or []
        if not isinstance(raw_tags, list):
            self._bad("tags must be a list")
            return
        try:
            item = save_starred_metadata(
                full_name,
                tags=[str(tag) for tag in raw_tags],
                note=str(payload.get("note") or ""),
                favorite=bool(payload.get("favorite")),
            )
            self._json(200, {"ok": True, "item": item})
        except ValueError as exc:
            self._bad(str(exc))
        except Exception as exc:  # noqa: BLE001
            logger.warning("starred metadata save failed: %s", exc)
            self._json(500, {"ok": False, "error": "保存星标管理数据失败"})

    def _github_starred(self, query: dict[str, list[str]]) -> None:
        """Return a paginated view of GitHub stars without exposing the token."""
        try:
            page = int((query.get("page") or ["1"])[0])
            per_page = int((query.get("per_page") or ["30"])[0])
        except (TypeError, ValueError):
            self._bad("page 和 per_page 必须是整数")
            return
        try:
            self._json(200, _github_starred_page(page, per_page))
        except RuntimeError as exc:
            logger.info("github starred list unavailable: %s", exc)
            self._json(502, {"ok": False, "error": str(exc)})

    def _library_delete(self) -> None:
        query = parse_qs(unquote(self.path).split("?", 1)[1] if "?" in self.path else "")
        full_name = str((query.get("repo") or [""])[0]).strip()
        if not full_name:
            self._bad("missing repo")
            return
        try:
            from src.profile.feedback_collector import delete_saved_project
            delete_saved_project(full_name)
            self._json(200, {"ok": True, "deleted": full_name})
        except Exception as exc:  # noqa: BLE001
            logger.warning("library delete failed: %s", exc)
            self._json(500, {"ok": False, "error": "删除失败"})

    def _receive_survey(self) -> None:
        """POST /api/survey：接收问卷档案（前端 saveSurvey/skipSurvey 上报）。"""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            self._bad("bad content-length")
            return
        if length <= 0 or length > 1 << 20:
            self._bad("payload too large")
            return
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._bad("invalid json")
            return
        survey = payload.get("survey")
        uid = str(payload.get("uid") or "anon").strip()[:64]
        if not isinstance(survey, dict):
            self._bad("survey must be object")
            return
        save_survey(uid, survey)
        self._json(200, {"ok": True, "saved": uid})

    # ===== OAuth 跳转登录 =====

    def _redirect(self, url: str) -> None:
        self.send_response(302)
        self.send_header("Location", url)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _build_redirect_uri(self) -> str:
        """跳转回调地址：OAUTH_REDIRECT_BASE 配置优先（服务器部署铺垫）；
        否则按 Host 推导，并把 localhost 规范化为 127.0.0.1（GitHub 推荐的 loopback 字面量，
        避免用户用 localhost 打开时与注册的 127.0.0.1 回调不匹配）。"""
        base = settings.oauth.redirect_base
        if base:
            return base.rstrip("/") + "/api/oauth/callback"
        host = self.headers.get("Host") or "127.0.0.1:8970"
        host = host.replace("localhost", "127.0.0.1")
        return f"http://{host}/api/oauth/callback"

    def _oauth_start(self, query: dict) -> None:
        """GET /api/oauth/start → 302 跳 GitHub 授权页；?probe=1 探测是否已配置。"""
        if query.get("probe", [""])[0] == "1":
            self._json(200, {"ok": True, "configured": _oauth_configured()})
            return
        if not _oauth_configured():
            self._json(400, {"ok": False, "error": "oauth not configured"})
            return
        redirect_uri = self._build_redirect_uri()
        state = secrets.token_urlsafe(16)
        with _oauth_lock:
            _oauth_states[state] = (time.time(), redirect_uri)
        params = urlencode({
            "client_id": settings.oauth.client_id,
            "redirect_uri": redirect_uri,
            "scope": "public_repo read:user",
            "state": state,
        })
        self._redirect(_GH_AUTHORIZE_URL + "?" + params)

    def _oauth_callback(self, query: dict) -> None:
        """GET /api/oauth/callback?code&state → 换 token → 302 回首页带一次性票据。"""
        code = (query.get("code") or [""])[0]
        state = (query.get("state") or [""])[0]
        if not code or not state:
            self._json(400, {"ok": False, "error": "missing code/state"})
            return
        with _oauth_lock:
            item = _oauth_states.pop(state, None)
        if not item:
            self._json(400, {"ok": False, "error": "bad state"})
            return
        ts, redirect_uri = item
        if time.time() - ts > _OAUTH_STATE_TTL:
            self._json(400, {"ok": False, "error": "state expired"})
            return
        try:
            req = Request(
                _GH_TOKEN_URL,
                data=urlencode({
                    "client_id": settings.oauth.client_id,
                    "client_secret": settings.oauth.client_secret,
                    "code": code,
                    "redirect_uri": redirect_uri,
                }).encode("utf-8"),
                headers={"Accept": "application/json", "User-Agent": "StarRadar"},
                method="POST",
            )
            with urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read(1 << 16).decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("oauth token exchange failed: %s", exc)
            self._json(502, {"ok": False, "error": "token exchange failed"})
            return
        token = data.get("access_token")
        if not token:
            self._json(400, {
                "ok": False,
                "error": data.get("error_description") or data.get("error") or "no token",
            })
            return
        ticket = secrets.token_urlsafe(16)
        with _oauth_lock:
            _oauth_tickets[ticket] = (time.time(), token)
        self._redirect("/?gh_ticket=" + ticket)

    def _oauth_ticket(self, query: dict) -> None:
        """GET /api/oauth/ticket?t=… → 一次性领取 token（前端转存 localStorage）。"""
        t = (query.get("t") or [""])[0]
        if not t:
            self._json(400, {"ok": False, "error": "missing ticket"})
            return
        with _oauth_lock:
            item = _oauth_tickets.pop(t, None)
        if not item:
            self._json(404, {"ok": False, "error": "bad or expired ticket"})
            return
        ts, token = item
        if time.time() - ts > _OAUTH_TICKET_TTL:
            self._json(404, {"ok": False, "error": "ticket expired"})
            return
        self._json(200, {"ok": True, "token": token})

    # ===== 个人特化版（本地后端，数据不公开） =====

    def _save_gh_token(self) -> None:
        """POST /api/gh_token：建立本地后端登录态。
        两种模式：
        - {use_local: true}        用 .env 的 GITHUB_TOKEN 建立登录（无需 OAuth App），
                                  返回 login + token 给浏览器（本机场景）
        - {login, token}           浏览器 GitHub 登录成功后上交给后端
        存 data/profile/gh_token.json（已被 .gitignore 忽略，绝不上传）。
        """
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            self._bad("bad content-length")
            return
        if length <= 0 or length > 1 << 16:
            self._bad("payload too large")
            return
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._bad("invalid json")
            return

        from config import PROFILE_DIR

        if payload.get("use_local"):
            token = settings.github.token
            if not token:
                self._bad("GITHUB_TOKEN 未配置（.env）")
                return
            try:
                import urllib.request
                req = urllib.request.Request(
                    "https://api.github.com/user",
                    headers={"Authorization": "Bearer " + token, "User-Agent": "StarRadar"},
                )
                with urllib.request.urlopen(req, timeout=15) as resp:
                    login = json.loads(resp.read().decode("utf-8")).get("login", "")
            except Exception as exc:  # noqa: BLE001
                logger.warning("local token 验证失败: %s", exc)
                self._json(502, {"ok": False, "error": "token 验证失败"})
                return
            if not login:
                self._json(502, {"ok": False, "error": "token 无效"})
                return
            try:
                (PROFILE_DIR / "gh_token.json").write_text(
                    json.dumps({"login": login, "token": token}, ensure_ascii=False),
                    encoding="utf-8",
                )
            except OSError:
                self._json(500, {"ok": False, "error": "write failed"})
                return
            self._json(200, {"ok": True, "saved": login, "login": login, "token": token})
            return

        token = str(payload.get("token") or "").strip()
        login = str(payload.get("login") or "").strip()[:64]
        if not token or not login:
            self._bad("missing token/login")
            return
        try:
            (PROFILE_DIR / "gh_token.json").write_text(
                json.dumps({"login": login, "token": token}, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError:
            self._json(500, {"ok": False, "error": "write failed"})
            return
        self._json(200, {"ok": True, "saved": login})

    def _save_llm_key(self) -> None:
        """POST /api/llm_key：浏览器配置的 LLM key 上交本地后端（供 --personal 管道调用）。
        存 data/profile/llm_config.json（.gitignore 忽略，绝不上传）。
        body: {base_url?, key, model?} —— 与前端 localStorage starradar:llm_config 同构。
        """
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            self._bad("bad content-length")
            return
        if length <= 0 or length > 1 << 16:
            self._bad("payload too large")
            return
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._bad("invalid json")
            return
        if not isinstance(payload, dict):
            self._bad("payload must be object")
            return
        key = str(payload.get("key") or "").strip()
        if not key:
            self._bad("missing key")
            return
        from config import PROFILE_DIR

        cfg = {
            "key": key,
            "base_url": str(payload.get("base_url") or "").strip() or None,
            "model": str(payload.get("model") or "").strip() or None,
        }
        try:
            (PROFILE_DIR / "llm_config.json").write_text(
                json.dumps(cfg, ensure_ascii=False), encoding="utf-8"
            )
        except OSError:
            self._json(500, {"ok": False, "error": "write failed"})
            return
        _load_local_llm_config()
        self._json(200, {"ok": True, "saved": True})

    def _survey_latest(self) -> None:
        """GET /api/survey/latest：返回最近一条问卷（浏览器 localStorage 丢失时恢复用）。
        前端启动时若本地无问卷且此处有 → 自动恢复，避免「重启后问卷又弹、又要重填」。
        """
        try:
            survey = load_latest_survey()
        except Exception as exc:  # noqa: BLE001
            logger.warning("survey latest failed: %s", exc)
            self._json(500, {"ok": False, "error": "load failed"})
            return
        if survey is None:
            self._json(200, {"ok": True, "survey": None})
            return
        self._json(200, {"ok": True, "survey": survey})

    def _personal_status(self) -> None:
        """GET /api/personal/status：个人版状态（登录 / LLM / 数据是否就绪）。"""
        from config import PROFILE_DIR
        gh = PROFILE_DIR / "gh_token.json"
        scores = PERSONAL_DIR / "scores.json"
        logged = gh.is_file()
        login = ""
        try:
            if logged:
                login = json.loads(gh.read_text(encoding="utf-8")).get("login", "")
        except (json.JSONDecodeError, OSError):
            pass
        self._json(200, {
            "ok": True,
            "logged_in": logged,
            "login": login,
            "llm_configured": bool(settings.llm.enabled and settings.llm.api_key),
            "data_exists": scores.is_file(),
        })

    def _recommendations_get(self) -> None:
        """GET /api/recommendations：读取当前账号的独立推荐快照与查看笔记。"""
        snapshot = load_snapshot(); account = (snapshot or {}).get("account")
        reviews = load_reviews(account)
        self._json(200, {"ok": True, "snapshot": snapshot or {"status": "never_loaded", "items": []}, "reviews": reviews})

    def _personal_scores(self) -> None:
        """GET /api/personal/scores：返回个人版雷达数据（仅本机可访问）。"""
        scores = PERSONAL_DIR / "scores.json"
        if not scores.is_file():
            self._json(404, {"ok": False, "error": "personal data not generated"})
            return
        try:
            data = scores.read_text(encoding="utf-8")
        except OSError:
            self._json(500, {"ok": False, "error": "read failed"})
            return
        # 兼容已经生成过的旧个人雷达：首次读取时补齐中文项目介绍，之后写回本机文件，
        # 不需要用户手动重新生成一遍所有推荐。旧版本曾把“请配置 AI 翻译”写进
        # description；优先用保留的英文原文重新走 LLM 翻译，不能把旧提示展示给用户。
        try:
            payload = json.loads(data)
            repos = [item.get("repo") for item in payload.get("items") or [] if isinstance(item, dict) and isinstance(item.get("repo"), dict)]
            legacy_markers = ("请先配置 AI 翻译", "本地中文翻译准备中")
            refresh_repos = [
                repo for repo in repos
                if repo.get("description_original") and (
                    repo.get("description") == repo.get("description_original")
                    or any(marker in str(repo.get("description") or "") for marker in legacy_markers)
                )
            ]
            if refresh_repos:
                for repo in refresh_repos:
                    repo["description"] = repo["description_original"]
                translate_descriptions(repos)
                data = json.dumps(payload, ensure_ascii=False, indent=2)
                scores.write_text(data, encoding="utf-8")
        except (json.JSONDecodeError, OSError, TypeError, AttributeError):
            pass
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data.encode("utf-8"))))
        self._cors()
        self.end_headers()
        self.wfile.write(data.encode("utf-8"))

    def _personal_history(self, query: dict[str, list[str]]) -> None:
        """GET /api/personal/history：每日发现目录，或读取指定的历史快照。"""
        archive_id = (query.get("archive") or [""])[0]
        if archive_id:
            payload = load_daily_archive(archive_id)
            if payload is None:
                self._json(404, {"ok": False, "error": "archive not found"})
                return
            self._json(200, {"ok": True, "archive": payload})
            return
        self._json(200, {"ok": True, "archives": list_daily_archives(), "retention_days": 90})

    def _personal_trends(self) -> None:
        """GET /api/personal/trends：个人版每周趋势（未生成时返回空周报）。"""
        trends = PERSONAL_DIR / "trends.json"
        if not trends.is_file():
            self._json(200, {"weeks": []})
            return
        try:
            data = trends.read_text(encoding="utf-8")
        except OSError:
            self._json(200, {"weeks": []})
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data.encode("utf-8"))))
        self._cors()
        self.end_headers()
        self.wfile.write(data.encode("utf-8"))

    def _trending(self, query: dict[str, list[str]]) -> None:
        """GET /api/trending：代理 GitHub 公开 Trending 页面，避免浏览器跨域。"""
        _load_local_llm_config()
        since = (query.get("since") or ["weekly"])[0]
        language = (query.get("language") or [""])[0]
        force = (query.get("refresh") or [""])[0] == "1"
        try:
            data = fetch_trending(since=since, language=language, force=force)
        except ValueError as exc:
            self._bad(str(exc))
        except Exception as exc:  # noqa: BLE001
            logger.warning("github trending fetch failed: %s", exc)
            self._json(502, {"ok": False, "error": "GitHub Trending 暂时无法读取，请稍后重试"})
        else:
            self._json(200, data)

    # ===== 静态文件 =====

    def _serve_static(self, path: str) -> None:
        if path in ("", "/"):
            path = "/index.html"
        # 公榜 JSON 即便文件仍在本地，也不允许在未输入访问码时直接读取。
        if path.startswith("/data/") and not self._is_authorized():
            self._unauthorized()
            return
        try:
            file = (STATIC_DIR / path.lstrip("/")).resolve()
            file.relative_to(STATIC_DIR.resolve())
        except (ValueError, OSError):
            self._json(404, {"ok": False, "error": "not found"})
            return
        if not file.is_file():
            self._json(404, {"ok": False, "error": "not found"})
            return
        mime = MIME_TYPES.get(file.suffix.lower(), "application/octet-stream")
        try:
            body = file.read_bytes()
        except OSError:
            self._json(500, {"ok": False, "error": "read failed"})
            return
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._cors()
        self.end_headers()
        self.wfile.write(body)


def _bind_server(host: str, port: int) -> ThreadingHTTPServer:
    """绑定端口并启动 HTTP server。

    Windows 下必须显式 SO_EXCLUSIVEADDRUSE 独占绑定——否则多个 --serve 实例
    会同时监听同一端口、请求随机分发（设备流/行为上报随机 502），这是真实踩过的坑。
    """
    import socket

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):  # Windows only
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    try:
        sock.bind((host, port))
    except OSError as exc:
        sock.close()
        print(f"  ✗ 端口 {port} 已被占用——可能已有 StarRadar 在运行。")
        print("    请先关闭旧实例（或换端口：python src/main.py --serve --port 8971）")
        print("    若旧进程残留：任务管理器结束 python.exe 后重试")
        raise SystemExit(1) from exc
    httpd = ThreadingHTTPServer((host, port), StarRadarHandler, bind_and_activate=False)
    httpd.socket.close()  # 丢弃内部未绑定 socket，替换为已独占绑定的
    httpd.socket = sock
    httpd.server_activate()
    return httpd


def _tls_self_check() -> None:
    """启动时 HTTPS 自检：当前 Python 环境无法访问 HTTPS 时提前警告。

    真实案例：conda 环境 Python 3.10 + OpenSSL 3.6 组合 TLS 全坏（ASN1:
    NOT_ENOUGH_DATA），所有 HTTPS 请求失败——server 转发 GitHub 全部 502，
    新登录（设备流/跳转）静默不可用，且极难排查。

    注意：浏览器能访问 GitHub 不代表 Python 环境正常（两者 TLS 实现独立）；
    已登录浏览/加星/Fork 是浏览器直连 api.github.com，不走本服务，不受影响。
    连测 2 次均失败才警告（防偶发网络抖动误报）。
    """
    from urllib.request import Request, urlopen

    def probe() -> bool:
        try:
            with urlopen(
                Request("https://api.github.com", headers={"User-Agent": "StarRadar"}),
                timeout=5,
            ) as resp:
                resp.read(64)
                return True
        except HTTPError:
            # 收到 HTTP 响应（403 限流等）→ TLS 握手已成功，环境正常
            return True
        except Exception:  # noqa: BLE001
            return False

    if probe():
        return
    if probe():  # 第二次确认（首次可能偶发抖动）
        return
    print("  ⚠️ HTTPS 自检失败——当前 Python 环境无法访问 HTTPS（已确认两次）：")
    print("    SSLError [ASN1: NOT_ENOUGH_DATA] 一类错误 = Python 与 OpenSSL 版本不兼容")
    print("    影响：仅「新登录」（设备流/跳转的授权码获取与 token 交换）会失败；")
    print("          已登录的浏览 / 加星 / Fork 走浏览器直连，不受影响。")
    print("    自验：python -c \"import urllib.request; print(urllib.request.urlopen")
    print("          ('https://api.github.com').status)\"  → 报错即环境问题")
    print("    解决：conda deactivate 后用系统 Python 运行，或升级本环境：")
    print("          conda install python=3.12")


def create_server(*, port: int = 8970, host: str = "127.0.0.1") -> ThreadingHTTPServer:
    """创建已绑定的 HTTP 服务，供 CLI 与桌面壳共同使用。"""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    _tls_self_check()
    _start_personal_scheduler()  # 个人版自动生成（启动补跑 + 每日 06:00）
    from src.recommendations import start_scheduler as _start_recommendation_scheduler
    _start_recommendation_scheduler()  # 页面关闭后仍运行：启动补偿 + 本地每日 08:00
    return _bind_server(host, port)


def _print_access_hint() -> None:
    """启动时告知本机访问码来源；打包版无控制台，内容进入 desktop.log。"""
    if not settings.access_required:
        return
    print(
        f"  本机访问码：{settings.access_code}"
        f"（存于 data/profile/access_code.txt，可用 STARRADAR_ACCESS_CODE 覆盖）"
    )


def serve(*, port: int = 8970, host: str = "127.0.0.1") -> None:
    httpd = create_server(port=port, host=host)
    print(f"StarRadar 服务已启动 → http://{host}:{port}/")
    print(f"  静态站点：{STATIC_DIR}")
    print(f"  行为信号：POST /api/events（memory.db 落库）")
    _print_access_hint()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
        httpd.server_close()


if __name__ == "__main__":
    import argparse
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
    parser = argparse.ArgumentParser(description="StarRadar Web 服务")
    parser.add_argument("--port", type=int, default=8970)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    serve(port=args.port, host=args.host)
