"""面向个人项目库的 GitHub 想法搜索。

将一句自然语言需求转换为少量 GitHub 仓库查询，再为结果给出轻量、
可解释的使用建议。这个模块刻意不依赖项目每日采集的数据，因此搜索
结果始终来自 GitHub 当前索引。
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any

from config import PROFILE_DIR
from src.collector.github_api import GitHubAPIClient, Repository, RateLimitError
from src.search.readme_evidence import extract_readme_evidence

logger = logging.getLogger(__name__)


def _local_github_token() -> str | None:
    """Return the locally saved GitHub login token when one is available.

    Idea search runs in the local server, so it can safely reuse the same
    credential supplied during GitHub login. A missing or invalid local login
    deliberately falls back to the normal ``GITHUB_TOKEN`` configuration.
    """
    token_path = PROFILE_DIR / "gh_token.json"
    try:
        data = json.loads(token_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    token = str(data.get("token") or "").strip() if isinstance(data, dict) else ""
    return token or None


# 搜索后的调研规则。这里刻意没有「不要立刻帮我写代码」这句话：
# StarRadar 本身不写代码，只负责给出项目选择与最小可行路径。
_RESEARCH_PROMPT = """你是一个 GitHub 开源项目调研与二次开发顾问。

当用户说“我要做一个 XXX 项目”时，先去 GitHub 搜索是否已经有可直接使用、可 Fork 二次开发，或值得参考学习的开源项目。

请按下面流程完成：
1. 理解项目想法，提取核心功能、目标用户、技术方向和关键词。
2. 优先寻找功能高度相似、近期仍在维护、有清晰 README/部署说明/开源许可证、适合个人二次开发的项目。
3. 对每个项目说明：名称和链接、已有功能、缺少的需求功能、维护状态、部署难度、许可证是否适合 Fork、可复用模块。
4. 将项目分为：可以直接使用、建议 Fork 后二次开发、仅值得参考学习。
5. 给出明确结论：应该直接使用还是基于现有项目继续开发；推荐哪个项目；最应该先改哪些功能。
6. 给出最简单 MVP：核心功能、第一版不做的功能、从 Fork 到跑起来再到第一个可用版本的最短步骤。

优先给出少量高质量项目，不要堆很多链接。结论要明确、务实，目标是避免重复造轮子，并让用户尽快开始实践。"""

_ZH_HINTS = {
    # 具体产品词放在前面，避免被泛化为一个无意义的 "ai" 查询。
    "ai剪辑": "ai video editor", "ai 视频剪辑": "ai video editor",
    "视频剪辑": "video editor", "剪辑": "video editor", "视频": "video",
    "智能体": "agent", "代理": "agent", "工作流": "workflow",
    "大模型": "llm", "模型": "llm", "日报": "daily digest",
    "新闻": "news aggregator", "搜索": "search", "知识库": "knowledge base",
    "爬虫": "crawler", "自动化": "automation", "向量": "vector database",
    "网页": "web", "前端": "frontend", "后端": "backend",
    "python": "python", "rust": "rust", "javascript": "javascript",
    "笔记": "notes app", "聊天": "chatbot", "面试": "interview",
    "简历": "resume", "记账": "expense tracker", "博客": "blog",
    "翻译": "translation", "语音": "speech", "监控": "monitoring",
    "部署": "deployment", "游戏": "game", "音乐": "music player",
}

# 产品型搜索的领域约束。GitHub 的全文搜索很容易把教程/作业 README
# 当成相关结果，因此对明确的产品意图增加轻量硬过滤。
_INTENT_RULES = {
    "video_editing": {
        "triggers": ("ai剪辑", "ai 视频剪辑", "视频剪辑", "视频编辑", "video editor", "video editing", "自动剪辑"),
        "queries": ("ai video editor", "video editing ffmpeg", "automatic video editing"),
        "include": ("video", "editor", "editing", "ffmpeg", "subtitle", "caption", "transcription", "moviepy", "remotion"),
        "exclude": ("assignment", "homework", "course", "tutorial", "aws", "shell", "exam", "university"),
    },
}


def _intent_rule(query: str) -> dict[str, Any] | None:
    text = query.lower().replace(" ", "")
    for rule in _INTENT_RULES.values():
        if any(trigger.replace(" ", "") in text for trigger in rule["triggers"]):
            return rule
    return None


def _fallback_terms(query: str) -> list[str]:
    """没有配置 LLM 时仍可用的中英关键词降级。"""
    q = query.lower()
    terms = [value for key, value in _ZH_HINTS.items() if key in q]
    latin = re.findall(r"[a-z][a-z0-9+.#-]{1,}", q)
    terms.extend(latin)
    terms = list(dict.fromkeys(terms))
    # 多条短查询优于把所有词硬拼成一条：GitHub 会将空格视为 AND，
    # "ai video editor" 比 "ai video editor video" 的召回更可靠。
    if "ai video editor" in terms:
        return ["ai video editor", "ai video editing", "video editor"]
    if "video editor" in terms:
        return ["video editor", "video editing"]
    if not terms:
        # GitHub 搜索对中文项目同样有效；无任何可用关键词时原句兑底。
        return [query.strip()] if query.strip() else []
    top = terms[:3]
    candidates = [" ".join(top)]
    # 原句作为独立的最后一个候选：仅在关键词覆盖不足（< 3 条）且带来新信息时追加，
    # 避免与主查询混合（AND 语义会收窄召回），也保持配额消耗可控。
    raw = query.strip()
    if raw and len(top) < 3 and raw.lower() not in {t.lower() for t in top}:
        candidates.append(raw)
    return list(dict.fromkeys(candidates))


def _llm_plan(query: str) -> dict[str, Any]:
    """把自然语言需求拆成可检索、可解释的计划；失败时返回空计划。"""
    cfg_path = PROFILE_DIR / "llm_config.json"
    if not cfg_path.is_file():
        return {}

    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        key = str(cfg.get("key") or "").strip()
        base_url = str(cfg.get("base_url") or "").strip()
        model = str(cfg.get("model") or "").strip()
        if not (key and base_url and model):
            return {}
        from openai import OpenAI
        client = OpenAI(api_key=key, base_url=base_url, timeout=20, max_retries=1)
        response = client.chat.completions.create(
            model=model,
            temperature=0,
            max_tokens=380,
            messages=[
                {"role": "system", "content": (
                    _RESEARCH_PROMPT + "\n\n当前只执行第 1 步的检索准备：把用户需求转换成 1 到 3 条简短的英文 "
                    "当前只做检索规划，不要推荐项目、不要解释。将用户需求拆解成 GitHub 可检索的英文表达和"
                    "判断条件。只输出 JSON，不要 Markdown，格式必须为："
                    "{\"core_queries\":[\"...\"],\"expanded_terms\":[\"...\"],"
                    "\"must_have\":[\"...\"],\"preferences\":[\"...\"],\"exclude\":[\"...\"],"
                    "\"filters\":{\"languages\":[\"...\"],\"licenses\":[\"...\"],\"active_within_days\":365}}。"
                    "core_queries 为 1-3 条简短英文 GitHub 仓库搜索词，不能包含 GitHub 限定符；"
                    "must_have / preferences / exclude 均使用简短英文条件；没有明确条件时返回空数组，"
                    "不要猜测。"
                )},
                {"role": "user", "content": query},
            ],
        )
        raw = (response.choices[0].message.content or "").strip()
        data = json.loads(re.search(r"\{.*\}", raw, re.S).group(0))
        if not isinstance(data, dict):
            return {}
        def strings(key: str, limit: int) -> list[str]:
            values = data.get(key)
            if not isinstance(values, list):
                return []
            return [str(value).strip()[:120] for value in values if str(value).strip()][:limit]
        raw_filters = data.get("filters") if isinstance(data.get("filters"), dict) else {}
        def filter_strings(key: str) -> list[str]:
            values = raw_filters.get(key)
            if not isinstance(values, list):
                return []
            return [str(value).strip()[:40] for value in values if str(value).strip()][:4]
        try:
            active_within_days = int(raw_filters.get("active_within_days") or 0)
        except (TypeError, ValueError):
            active_within_days = 0
        return {
            "core_queries": strings("core_queries", 3),
            "expanded_terms": strings("expanded_terms", 8),
            "must_have": strings("must_have", 6),
            "preferences": strings("preferences", 6),
            "exclude": strings("exclude", 6),
            "filters": {
                "languages": filter_strings("languages"),
                "licenses": filter_strings("licenses"),
                "active_within_days": max(0, min(active_within_days, 3650)),
            },
        }
    except Exception as exc:  # noqa: BLE001 - 搜索必须可降级
        logger.info("idea search LLM expansion unavailable: %s", exc)
        return {}


def _sanitize_plan(plan: dict[str, Any]) -> dict[str, Any]:
    """限制 LLM 计划的形状和值，避免脏数据进入查询或排序。"""
    if not isinstance(plan, dict):
        return {}
    def strings(name: str, limit: int, max_len: int = 80) -> list[str]:
        values = plan.get(name)
        if not isinstance(values, list):
            return []
        return [str(v).strip()[:max_len] for v in values if isinstance(v, str) and v.strip()][:limit]
    raw_filters = plan.get("filters") if isinstance(plan.get("filters"), dict) else {}
    languages = [v.lower() for v in raw_filters.get("languages", []) if isinstance(v, str) and v.strip()][:4]
    licenses = [v.upper() for v in raw_filters.get("licenses", []) if isinstance(v, str) and v.strip()][:4]
    try:
        active_days = int(raw_filters.get("active_within_days") or 0)
    except (TypeError, ValueError):
        active_days = 0
    return {
        "core_queries": strings("core_queries", 3), "expanded_terms": strings("expanded_terms", 8),
        "must_have": strings("must_have", 6), "preferences": strings("preferences", 6),
        "exclude": strings("exclude", 6),
        "filters": {"languages": languages, "licenses": licenses, "active_within_days": max(0, min(active_days, 3650))},
    }


def _llm_research(query: str, projects: list[dict[str, Any]], plan: dict[str, Any]) -> dict[str, Any] | None:
    """根据 GitHub 已返回的可验证元数据生成调研结论；失败时保留规则结果。"""
    cfg_path = PROFILE_DIR / "llm_config.json"
    if not cfg_path.is_file() or not projects:
        return None
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        key = str(cfg.get("key") or "").strip()
        base_url = str(cfg.get("base_url") or "").strip()
        model = str(cfg.get("model") or "").strip()
        if not (key and base_url and model):
            return None
        evidence = json.dumps(projects, ensure_ascii=False, separators=(",", ":"))
        from openai import OpenAI
        response = OpenAI(api_key=key, base_url=base_url, timeout=20, max_retries=1).chat.completions.create(
            model=model,
            temperature=0.2,
            max_tokens=900,
            messages=[
                {"role": "system", "content": _RESEARCH_PROMPT + "\n\n只能依据提供的 GitHub 元数据和 README 证据片段判断；"
                 "没有 README/部署证据时必须写“需查看 README 确认”，不要编造。"
                 "尤其要逐项对照检索计划中的 must_have、preferences 和 exclude；"
                 "元数据不能证实的条件不能判定为满足。"
                 "只输出 JSON，格式为：{\"conclusion\":\"...\",\"mvp\":[\"...\"],\"projects\":[{\"full_name\":\"...\",\"category\":\"direct|fork|reference\",\"existing\":\"...\",\"missing\":\"...\",\"maintenance\":\"...\",\"deployment\":\"...\",\"reuse\":\"...\",\"reason\":\"...\"}]}"},
                {"role": "user", "content": "用户想法：" + query + "\n\n检索计划：" + json.dumps(plan, ensure_ascii=False) + "\n\nGitHub 搜索结果：" + evidence},
            ],
        )
        raw = (response.choices[0].message.content or "").strip()
        data = json.loads(re.search(r"\{.*\}", raw, re.S).group(0))
        return data if isinstance(data, dict) else None
    except Exception as exc:  # noqa: BLE001
        logger.info("idea search LLM research unavailable: %s", exc)
        return None


def _recommendation(repo: Repository, query_terms: list[str]) -> tuple[str, str]:
    """基于公开元数据给出保守建议，不声称已经读过 README。"""
    age_days = max(0, (datetime.now(timezone.utc) - repo.pushed_at).days)
    haystack = " ".join([
        repo.full_name, repo.description or "", " ".join(repo.topics), repo.language or "",
    ]).lower()
    keywords = {
        word for term in query_terms for word in re.findall(r"[a-z][a-z0-9+#.-]{1,}", term.lower())
        if word not in {"ai", "app", "tool", "project"}
    }
    hits = sum(1 for word in keywords if word in haystack)
    active = age_days <= 180 and not repo.archived
    permissive = (repo.license or "").upper() in {"MIT", "APACHE-2.0", "BSD-2-CLAUSE", "BSD-3-CLAUSE", "ISC"}

    if active and permissive and hits >= 2:
        return "适合二开", "近期仍在维护，且许可证较友好；先阅读 README 再决定是否 Fork。"
    if active and hits >= 1:
        return "值得研究", "方向匹配且项目仍活跃；先确认许可证和部署成本。"
    return "参考灵感", "可作为相关实现参考；建议先确认当前维护状态。"


def _matches_explicit_filters(repo: Repository, filters: dict[str, str]) -> bool:
    """Use selected UI conditions as a final hard filter after GitHub recall."""
    min_stars = filters["min_stars"]
    if min_stars.isdigit() and repo.stars < int(min_stars):
        return False
    language = filters["language"]
    if language and (repo.language or "").casefold() != language.casefold():
        return False
    license_name = filters["license"]
    if license_name and (repo.license or "").casefold() != license_name.casefold():
        return False
    updated_days = filters["updated_days"]
    if updated_days.isdigit() and (datetime.now(timezone.utc) - repo.pushed_at).days > min(int(updated_days), 3650):
        return False
    return not repo.archived


def _has_usage_evidence(evidence: list[dict[str, Any]]) -> bool:
    """Only README usage/deployment evidence supports a direct-use suggestion."""
    usage_terms = ("install", "usage", "quick start", "quickstart", "getting started", "deploy", "安装", "使用", "部署")
    text = " ".join(
        str(entry.get("section") or "") + " " + str(entry.get("text") or "")
        for entry in evidence
    ).casefold()
    return any(term in text for term in usage_terms)


def _condition_hits(text: str, conditions: list[str]) -> int:
    """在公开元数据中寻找条件证据；没有证据时不将其误判为不符合。"""
    hits = 0
    for condition in conditions:
        words = re.findall(r"[a-z][a-z0-9+#.-]{1,}", condition.lower())
        useful = [word for word in words if word not in {"app", "tool", "project", "software"}]
        if useful and all(word in text for word in useful):
            hits += 1
    return hits


def _plan_score(repo: Repository, plan: dict[str, Any]) -> int:
    """用检索计划进行温和加权，不将 README 未披露的信息作为硬性淘汰条件。"""
    text = " ".join([repo.full_name, repo.description or "", " ".join(repo.topics), repo.language or ""]).lower()
    score = _condition_hits(text, plan.get("must_have", [])) * 5
    score += _condition_hits(text, plan.get("preferences", [])) * 2
    score -= _condition_hits(text, plan.get("exclude", [])) * 4
    filters = plan.get("filters", {})
    languages = {str(value).lower() for value in filters.get("languages", [])}
    licenses = {str(value).upper() for value in filters.get("licenses", [])}
    if languages and (repo.language or "").lower() in languages:
        score += 2
    if licenses and (repo.license or "").upper() in licenses:
        score += 2
    active_days = int(filters.get("active_within_days") or 0)
    if active_days:
        age_days = max(0, (datetime.now(timezone.utc) - repo.pushed_at).days)
        score += 1 if age_days <= active_days else -1
    return score


def _timed_out(deadline: float | None) -> bool:
    """超过总闸时间时放弃剩余阶段，先回已获取的结果。"""
    return deadline is not None and time.time() > deadline


def search_ideas(query: str, *, limit: int = 5, filters: dict[str, Any] | None = None,
                 deadline: float | None = None) -> dict[str, Any]:
    """实时搜索 GitHub 并返回前端所需的安全、扁平化结果。

    deadline 为可选的 Unix 时间戳总闸：规划、README 证据、LLM 调研三个
    阶段在边界处检查，超时即停止后续深检索并降级返回已有结果。
    """
    query = query.strip()
    filters = filters if isinstance(filters, dict) else {}
    if not query:
        raise ValueError("请输入想找的项目方向")
    # LLM 负责理解并先给出更准确的核心查询；本地词典保住中文领域词的召回。
    # 已超时（deadline 已过）则直接走词典模式，不再等待 LLM。
    fallback_terms = _fallback_terms(query)
    plan = {} if _timed_out(deadline) else _sanitize_plan(_llm_plan(query))
    # 已知产品领域的本地词典优先于 LLM 规划，避免模型偶尔把“自动剪辑”
    # 误解成泛化的 automation / assignment / AWS 查询。
    rule = _intent_rule(query)
    if rule:
        terms = list(rule["queries"])
    else:
        terms = list(dict.fromkeys(fallback_terms + plan.get("core_queries", [])))[:3] or [query]
    # Reuse the local GitHub login. Without one, GitHubAPIClient retains its
    # existing .env GITHUB_TOKEN (then anonymous) fallback behaviour.
    github_token = _local_github_token()
    client = GitHubAPIClient(token=github_token, cache_ttl_days=1)
    # 这些条件来自前端显式选择；不再隐藏地固定 stars:>5。
    min_stars = str(filters.get("min_stars") or "").strip()
    updated_days = str(filters.get("updated_days") or "").strip()
    language = str(filters.get("language") or "").strip()
    license_name = str(filters.get("license") or "").strip()
    qualifiers: list[str] = []
    # 想法搜索固定排除归档项目；前端不再提供“包含已归档”选项。
    include_archived = False
    qualifiers.append("archived:false")
    if min_stars.isdigit() and int(min_stars) > 0:
        qualifiers.append(f"stars:>={int(min_stars)}")
    if updated_days.isdigit() and int(updated_days) > 0:
        from datetime import timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(days=min(int(updated_days), 3650))).date().isoformat()
        qualifiers.append(f"pushed:>{cutoff}")
    if language:
        qualifiers.append(f"language:{language}")
    if license_name:
        qualifiers.append(f"license:{license_name}")
    # LLM 只能通过经过校验的结构化 filters 提供建议；用户显式选择优先。
    plan_filters = plan.get("filters", {})
    if not language:
        qualifiers.extend(f"language:{value}" for value in plan_filters.get("languages", [])[:1])
    if not license_name:
        qualifiers.extend(f"license:{value}" for value in plan_filters.get("licenses", [])[:1])
    if not updated_days and int(plan_filters.get("active_within_days") or 0) > 0:
        from datetime import timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(days=int(plan_filters["active_within_days"]))).date().isoformat()
        qualifiers.append(f"pushed:>{cutoff}")
    seen: dict[str, Repository] = {}
    github_queries: list[str] = []
    for term in terms[:3]:
        query_qualifiers = list(qualifiers)
        if rule:
            query_qualifiers.extend("-" + word for word in rule["exclude"])
        github_query = " ".join([term] + query_qualifiers)
        github_queries.append(github_query)
        result = client.search_repositories(github_query, sort=None, order="desc", per_page=15, use_cache=True)
        for repo in result.items:
            if include_archived or not repo.archived:
                seen.setdefault(repo.full_name.lower(), repo)

    explicit_filters = {
        "min_stars": min_stars, "updated_days": updated_days,
        "language": language, "license": license_name,
    }
    items = [repo for repo in seen.values() if _matches_explicit_filters(repo, explicit_filters)]
    if rule:
        # 至少命中一个视频/剪辑领域词才进入候选，防止普通课程仓库污染结果。
        def is_relevant(repo: Repository) -> bool:
            text = " ".join([repo.full_name, repo.description or "", " ".join(repo.topics), repo.language or ""]).lower()
            return any(word in text for word in rule["include"])
        items = [repo for repo in items if is_relevant(repo)]
    # 排序优先级：查询词命中数 > 检索计划匹配分 > 最近活跃 > Star 数。
    # 元组按字典序比较：命中始终压过一切；Star 仅在同分且同活跃度时起作用。
    def rank(repo: Repository) -> tuple[int, int, float, int]:
        text = " ".join([repo.full_name, repo.description or "", " ".join(repo.topics)]).lower()
        matched = sum(
            1 for term in terms
            for word in re.findall(r"[a-z][a-z0-9+#.-]{1,}", term.lower())
            if word not in {"ai", "app", "tool", "project"} and word in text
        )
        return (matched, _plan_score(repo, plan), repo.pushed_at.timestamp(), repo.stars)
    items.sort(key=rank, reverse=True)

    # 第一层 RAG：只为初排靠前的候选抓取 README，避免一次搜索耗尽 GitHub 配额。
    # README 采用更长的缓存周期；命中的原文片段会进入重排和 LLM 调研，而不是整篇塞进提示词。
    evidence_client = GitHubAPIClient(token=github_token, cache_ttl_days=7, timeout=10)
    evidence_by_repo: dict[str, list[dict[str, Any]]] = {}
    # 混合候选池：初排、计划匹配和近期活跃项目共同进入 README 深检索。
    ranked_seed = items[:6]
    plan_seed = sorted(items, key=lambda repo: _plan_score(repo, plan), reverse=True)[:3]
    active_seed = sorted(items, key=lambda repo: repo.pushed_at.timestamp(), reverse=True)[:3]
    evidence_candidates: list[Repository] = []
    for repo in ranked_seed + plan_seed + active_seed:
        if repo.full_name.lower() not in {item.full_name.lower() for item in evidence_candidates}:
            evidence_candidates.append(repo)
    evidence_failed = False
    evidence_degraded = False
    for repo in evidence_candidates[:12]:
        if _timed_out(deadline):
            evidence_degraded = True
            break
        try:
            evidence_by_repo[repo.full_name.lower()] = extract_readme_evidence(
                evidence_client, repo, terms=terms, plan=plan,
            )
        except Exception as exc:  # noqa: BLE001 - 限流或单仓库错误时保留元数据结果
            evidence_failed = True
            evidence_degraded = evidence_degraded or isinstance(exc, RateLimitError)
            logger.info("README evidence unavailable for %s: %s", repo.full_name, exc)
            evidence_by_repo[repo.full_name.lower()] = []
    def evidence_rank(repo: Repository) -> tuple[int, int, int, float, int]:
        evidence_score = sum(int(item.get("score") or 0) for item in evidence_by_repo.get(repo.full_name.lower(), []))
        return (evidence_score,) + rank(repo)
    items.sort(key=evidence_rank, reverse=True)

    payload = []
    for repo in items[:max(1, min(limit, 5))]:
        label, reason = _recommendation(repo, terms)
        evidence = evidence_by_repo.get(repo.full_name.lower(), [])
        # 匹配依据：复用排序的命中逻辑，让用户知道第一条为什么排第一。
        text = " ".join([repo.full_name, repo.description or "", " ".join(repo.topics), repo.language or ""]).lower()
        matched = sorted({
            w for t in terms
            for w in re.findall(r"[a-z][a-z0-9+#.-]{1,}", t.lower())
            if len(w) > 2 and w in text
        })[:6]
        payload.append({
            "full_name": repo.full_name,
            "description": repo.description or "暂无项目描述。",
            "html_url": repo.html_url,
            "stars": repo.stars,
            "forks": repo.forks,
            "language": repo.language or "未标注",
            "license": repo.license or "未标注",
            "topics": repo.topics[:8],
            "created_at": repo.created_at.date().isoformat(),
            "pushed_at": repo.pushed_at.date().isoformat(),
            "recommendation": label,
            "recommendation_reason": reason,
            "matched_terms": matched,
            "evidence": evidence,
        })
    # 已超时则不再发起 LLM 调研；保留规则结论。
    research = None if _timed_out(deadline) else _llm_research(query, payload[:6], plan)
    if research:
        by_name = {str(item.get("full_name") or "").lower(): item for item in research.get("projects", []) if isinstance(item, dict)}
        labels = {"direct": "可以直接使用", "fork": "建议 Fork 二开", "reference": "参考学习"}
        for item in payload:
            analysis = by_name.get(item["full_name"].lower())
            if not analysis:
                continue
            category = str(analysis.get("category") or "")
            if category == "direct" and not _has_usage_evidence(item["evidence"]):
                item["recommendation"] = "建议 Fork 二开"
                item["recommendation_reason"] = "项目方向匹配，但未找到可验证的 README 使用或部署证据；建议先 Fork 验证。"
            else:
                item["recommendation"] = labels.get(category, item["recommendation"])
                item["recommendation_reason"] = str(analysis.get("reason") or item["recommendation_reason"])
    evidence_count = sum(1 for values in evidence_by_repo.values() if values)
    if evidence_degraded and evidence_count:
        evidence_status = "degraded"
    elif evidence_failed and evidence_count:
        evidence_status = "partial"
    elif evidence_degraded or evidence_failed:
        evidence_status = "unavailable"
    else:
        evidence_status = "complete"
    llm_used = bool(plan.get("core_queries")) or research is not None
    return {"ok": True, "query": query, "search_terms": terms,
            "evidence_status": evidence_status, "evidence_count": evidence_count,
            "llm_used": llm_used,
            "items": payload, "result_count": len(payload)}
