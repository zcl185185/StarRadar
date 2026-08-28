"""从 GitHub README 提取结构化的「项目速览」证据。

这是想法搜索的第一层 RAG：不把 README 全文直接交给模型或前端，而是
按用户关心的三个问题（这是什么 / 核心功能 / 怎么跑起来）做意图抽取，
返回干净、可扫读的速览条目。所有内容均来自 README 原文——抽不到就
降级为清洗后的关键词命中片段，绝不编造。

英文 README 的速览会通过用户自配的 LLM 翻译为中文（带本地缓存）；
未配置 Key 或翻译失败时展示原文，绝不静默丢弃内容。
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

from config import PROFILE_DIR
from src.collector.github_api import GitHubAPIClient, Repository

logger = logging.getLogger(__name__)

_TRANSLATION_CACHE_PATH = PROFILE_DIR / "overview_translations.json"
_TRANSLATION_CACHE_TTL = 30 * 24 * 3600  # 译文缓存 30 天
_TRANSLATION_TIMEOUT = 20

_HEADING_RE = re.compile(r"(?m)^#{1,4}\s+(.+?)\s*#*\s*$")
_WORD_RE = re.compile(r"[a-z][a-z0-9+#.-]{1,}")
_IGNORED_WORDS = {"ai", "app", "tool", "project", "software", "open", "source"}

# 常见「安装前依赖」标题：其内容是元任务而不是项目功能，禁止当作 features。
_META_HEADINGS = ("installation", "getting started", "quick start", "usage", "deploy",
                  "prerequisites", "requirements", "configuration", "contributing", "license")
_BADGE_RE = re.compile(
    r"\[!\[[^\]]*\]\([^)]*\)\]\([^)]*\)"      # [![badge](shields)](link)
    r"|!\[[^\]]*\]\([^)]*\)"                   # ![alt](img)
    r"|\[!?[^\]]{0,40}\]\((?:https?:)?//[^)]*\)"  # 链接与裸徽章
)
_HTML_RE = re.compile(r"<[^>]{1,160}>")
_EMOJI_RE = re.compile(
    r"[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF\U00002B00-\U00002BFF\U0000FE00-\U0000FE0F\U0001F900-\U0001F9FF]"
)
_TEMPLATE_LINE_RE = re.compile(
    r"^(?:#|\-{3,}|\*{3,}|=|<|>|\||!\[|\[!\[|\s*(?:screenshots?|demo|preview|screenshot)\s*:?\s*$)",
    re.I,
)


def _strip_markup(text: str) -> str:
    """去掉徽章 / HTML / emoji / 强调语法，只留可读文字。"""
    text = _BADGE_RE.sub(" ", text)
    text = _HTML_RE.sub(" ", text)
    text = _EMOJI_RE.sub("", text)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text)          # 残留图片
    text = re.sub(r"\[([^\]]{1,80})\]\([^)]*\)", r"\1", text)  # [文字](链接) → 文字
    text = re.sub(r"`{1,3}([^`]*)`{1,3}", r"\1", text)          # 代码标记 → 内容
    text = re.sub(r"^\s*[-*+]\s+", "", text, flags=re.M)        # 列表符号（保留文字）
    text = re.sub(r"\s+", " ", text)
    return text.strip(" -–—:|")


def _clean_line(text: str, limit: int = 160) -> str:
    """单行清洗：去列表符号/强调/注释，压到一行。"""
    line = _strip_markup(text)
    line = re.sub(r"\*\*?([^*]*)\*\*?", r"\1", line)  # **加粗** 残留
    line = re.sub(r"\s+", " ", line).strip(" -–—:|•")
    return line if len(line) <= limit else line[: limit - 1].rstrip() + "…"


def _readable(text: str) -> bool:
    """跳过图片行、表格行、纯命令块、单字符行等不可读内容。"""
    if not text or len(text.strip()) < 6:
        return False
    if _TEMPLATE_LINE_RE.match(text.strip()):
        return False
    stripped = _strip_markup(text)
    return len(stripped) >= 6


def _pick_bullets(section_text: str, limit: int) -> list[str]:
    """优先取列表条目（功能列表的常见形态），不足时退回句子。"""
    bullets: list[str] = []
    for line in section_text.splitlines():
        stripped = line.strip()
        if re.match(r"^[-*+]\s+\S", stripped) or re.match(r"^\d+[.)]\s+\S", stripped):
            cleaned = _clean_line(stripped)
            if cleaned and _readable(stripped) and len(cleaned) > 4 and _looks_like_feature(cleaned):
                bullets.append(cleaned)
        if len(bullets) >= limit:
            break
    return bullets


def _extract_commands(section_text: str, limit: int = 2) -> list[str]:
    """抽取代码块与行内代码里的可执行命令（shell 命令形态优先）。"""
    commands: list[str] = []
    fenced = re.findall(r"```[a-zA-Z]*\n(.*?)```", section_text, re.S)
    source = "\n".join(fenced) if fenced else section_text
    for match in re.finditer(r"`([^`\n]{3,120})`", source):
        candidate = match.group(1).strip()
        if candidate and candidate not in commands:
            commands.append(candidate)
        if len(commands) >= limit * 3:
            break
    if not commands and fenced:
        for line in fenced[0].splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                commands.append(line[:120])
                if len(commands) >= limit:
                    break
    # 只保留像命令的：包管理器/git/运行时开头，或含 install 调用
    starters = ("pip ", "pip3 ", "npm ", "npx ", "yarn ", "pnpm ", "docker ", "git ",
                "cargo ", "go ", "make ", "python ", "python3 ", "node ", "brew ", "sudo ")
    picked = [c for c in commands if c.startswith(starters) or ("install" in c.lower() and " " in c)]
    if not picked:
        picked = commands
    # 二次过滤：丢掉分支名 / 纯 flag / 配置片段 / 路径这类伪命令
    def _is_real_command(c: str) -> bool:
        if c.startswith("--") or c.startswith("-"):
            return False
        if " " not in c and len(c) < 12:
            return False
        if re.fullmatch(r"[\w./-]+", c) and ("/" in c or c in {"master", "main", "dev"}):
            return False
        if ":" in c and ("{" in c or "type" in c.lower() or "display" in c.lower()):
            return False
        return True
    picked = [c for c in picked if _is_real_command(c)] or picked[:1]
    # 去重保序、压数量
    seen: list[str] = []
    for c in picked:
        if c not in seen:
            seen.append(c)
        if len(seen) >= limit:
            break
    return seen


def _is_meta_heading(heading: str) -> bool:
    lowered = heading.lower()
    return any(word in lowered for word in _META_HEADINGS)


def _looks_like_description(text: str) -> bool:
    """简介形态：一段完整陈述句，不含代码块/表格/语言清单/徽章残骸，长度适中。"""
    if len(text) > 900 or "```" in text:
        return False
    clean = _strip_markup(text)
    if not (24 <= len(clean) <= 460):
        return False
    # 拒绝非句子内容：语言清单（大量 "|" 或 "X | Y | Z"）、短词堆叠、大写缩写连发
    if clean.count("|") >= 2:
        return False
    words = clean.split()
    if len(words) >= 4 and sum(1 for w in words if len(w) <= 2) / len(words) > 0.5:
        return False
    # 至少含一个空格分隔的词组且不以 ">" 引用开头
    return not clean.startswith(">")


def _looks_like_feature(text: str) -> bool:
    """功能条目形态：描述性短语，排除命令、分支名、配置片段。"""
    stripped = text.strip()
    if not stripped or len(stripped) < 8:
        return False
    # 命令形态：以包管理器/git 开头，或含 "git@"/"http" 链接
    lowered = stripped.lower()
    if lowered.startswith(("git ", "pip ", "npm ", "npx ", "yarn ", "pnpm ", "docker ", "cargo ", "go ", "make ", "python ", "brew ", "sudo ", "cd ", "-")):
        return False
    if "git@" in stripped or "clone" == stripped.split(" ")[0].lower():
        return False
    # 分支名 / 配置键形态：单词或 "key: value" 且无动词短语
    if " " not in stripped and len(stripped) < 20:
        return False
    if re.match(r"^[a-z][\w.-]*\s*[:=]\s*\{?\s*['\"]?\w", stripped) and len(stripped) < 60:
        return False
    # 纯路径 / 分支形态
    if re.match(r"^[\w./-]+$", stripped) and ("/" in stripped or "." in stripped) and " " not in stripped:
        return False
    return True


def split_readme(text: str, max_chunk_chars: int = 1_400) -> list[tuple[str, str]]:
    """优先按 Markdown 标题切块；超长章节再按段落拆开。（保留原排序用途）"""
    text = text.replace("\r\n", "\n").strip()
    if not text:
        return []
    matches = list(_HEADING_RE.finditer(text))
    sections: list[tuple[str, str]] = []
    if not matches:
        sections.append(("README", text))
    else:
        if text[:matches[0].start()].strip():
            sections.append(("README", text[:matches[0].start()]))
        for index, match in enumerate(matches):
            end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
            sections.append((re.sub(r"\s+", " ", match.group(1)).strip()[:100], text[match.end():end]))

    chunks: list[tuple[str, str]] = []
    for heading, body in sections:
        body = body.strip()
        if not body:
            continue
        if len(body) <= max_chunk_chars:
            chunks.append((heading, body))
            continue
        current = ""
        for paragraph in re.split(r"\n\s*\n", body):
            paragraph = paragraph.strip()
            if not paragraph:
                continue
            if current and len(current) + len(paragraph) + 2 > max_chunk_chars:
                chunks.append((heading, current))
                current = ""
            current = (current + "\n\n" + paragraph).strip()
        if current:
            chunks.append((heading, current))
    return chunks


def _keywords(value: str) -> list[str]:
    return [word for word in _WORD_RE.findall(value.lower()) if word not in _IGNORED_WORDS]


def _score_chunk(text: str, queries: list[str], conditions: list[str]) -> tuple[int, list[str]]:
    """返回轻量词法证据分数与实际命中的查询/条件，避免无依据的语义断言。"""
    lower = text.lower()
    score = 0
    matched: list[str] = []
    for phrase in queries:
        words = _keywords(phrase)
        if not words:
            continue
        if phrase.lower() in lower or all(word in lower for word in words):
            score += 2 + min(len(words), 3)
            matched.append(phrase)
    for condition in conditions:
        words = _keywords(condition)
        if words and all(word in lower for word in words):
            score += 5
            matched.append(condition)
    return score, list(dict.fromkeys(matched))


def _extract_overview(readme_text: str, sections: list[tuple[str, str]]) -> str | None:
    """「这是什么」：标题后第一段陈述（项目自己的定位句），不从功能/安装章节取。"""
    # ① 标题之前 / 之后的导语
    head = re.split(r"(?m)^#{1,2}\s+", readme_text)[0]
    for candidate_text in (head, sections[0][1] if sections and sections[0][0].lower() in {"readme", "about", "introduction", "简介"} else ""):
        for paragraph in re.split(r"\n\s*\n", candidate_text or ""):
            paragraph = paragraph.strip()
            if paragraph and _looks_like_description(paragraph):
                cleaned = _clean_line(paragraph, 200)
                if cleaned and len(cleaned) > 20:
                    return cleaned
    # ② 常见简介标题
    for heading, body in sections:
        if heading.lower() in {"about", "introduction", "overview", "简介", "介绍", "关于"} and _looks_like_description(body):
            return _clean_line(body, 200)
    # ③ 首个非 meta 章节的首段
    for heading, body in sections:
        if heading.lower() == "readme" or _is_meta_heading(heading):
            continue
        for paragraph in re.split(r"\n\s*\n", body):
            if paragraph.strip() and _looks_like_description(paragraph):
                cleaned = _clean_line(paragraph, 200)
                if cleaned:
                    return cleaned
    return None


def _extract_features(sections: list[tuple[str, str]], limit: int = 3) -> list[str]:
    """「核心功能」：Features / 特性 / 功能 标题下的列表条目。"""
    candidates: list[tuple[str, str]] = []
    for heading, body in sections:
        lowered = heading.lower()
        if _is_meta_heading(lowered):
            continue
        if any(word in lowered for word in ("feature", "function", "highligh", "特性", "功能", "亮点", "能力")):
            candidates.append((heading, body))
    for _, body in candidates:
        items = _pick_bullets(body, limit)
        if items:
            return items
    # 回退：第一个非 meta 章节的列表（很多 README 功能列表没有 Features 标题）
    for heading, body in sections:
        if heading.lower() == "readme" or _is_meta_heading(heading):
            continue
        items = _pick_bullets(body, limit)
        if len(items) >= 2:
            return items
    return []


_REQUIREMENT_PATTERNS = (
    (re.compile(r"python\s*(?:3\.[5-9]|3\.1[0-9]|3)[\s.+年]*|python\s*>=?\s*3", re.I), "python"),
    (re.compile(r"node(?:\.js)?\s*(?:>=?)?\s*(?:1[4-9]|2[0-9])", re.I), "node"),
    (re.compile(r"docker", re.I), "docker"),
    (re.compile(r"(?:open)?api[_\s-]?key", re.I), "apiKey"),
    (re.compile(r"(?:gpu|cuda)", re.I), "gpu"),
    (re.compile(r"(\d+)\s*gb?(?:\s*ram|\s*memory|内存)", re.I), "ram"),
)


def _extract_requirements(sections: list[tuple[str, str]]) -> list[str]:
    """「跑起来需要」：从安装/前提/配置章节里找硬件与环境要求，用大白话表达。

    只报告 README 明确写了的内容（Python 版本、Node、Docker、API Key、
    GPU/CUDA、内存 GB），找不到就不显示——绝不猜测。
    """
    found: list[str] = []
    seen: set[str] = set()
    preferred = ("prerequisites", "requirements", "requirements", "installation", "install",
                 "getting started", "quick start", "deploy", "configuration", " setup",
                 "前提", "要求", "环境", "安装", "部署", "配置")
    targets = [body for heading, body in sections if any(w in heading.lower() for w in preferred)]
    if not targets:
        targets = [body for heading, body in sections if not _is_meta_heading(heading)][:2]
    haystack = "\n".join(targets)
    # 长度限制：避免超大 README 拖慢正则
    haystack = haystack[:6000]
    for pattern, kind in _REQUIREMENT_PATTERNS:
        match = pattern.search(haystack)
        if not match or kind in seen:
            continue
        seen.add(kind)
        text = match.group(0).strip()
        if kind == "python":
            version = re.search(r"3(?:\.[5-9]|\.1[0-9])?", text)
            found.append("需要装 Python" + (f" {version.group(0)} 及以上" if version else "（3.x）"))
        elif kind == "node":
            version = re.search(r"1[4-9]|2[0-9]", text)
            found.append("需要装 Node.js" + (f" {version.group(0)} 及以上" if version else ""))
        elif kind == "docker":
            found.append("装有 Docker 更省事（官方提供容器方式）")
        elif kind == "apiKey":
            found.append("要自己申请一个 AI 服务的 API Key（如 OpenAI / DeepSeek）")
        elif kind == "gpu":
            found.append("重度使用建议有 NVIDIA 显卡（GPU/CUDA）")
        elif kind == "ram":
            gb = re.search(r"(\d+)", text)
            if gb:
                found.append(f"内存至少 {gb.group(1)} GB")
    return found[:4]


def _extract_quickstart(sections: list[tuple[str, str]], limit: int = 2) -> list[str]:
    """「怎么跑起来」：安装/使用章节的关键命令。"""
    preferred = ("installation", "install", "quick start", "quickstart", "getting started",
                 "usage", "getting started", "快速开始", "安装", "部署", "使用")
    for heading, body in sections:
        lowered = heading.lower()
        if any(word in lowered for word in preferred):
            commands = _extract_commands(body, limit)
            if commands:
                return commands
    for heading, body in sections:
        if _is_meta_heading(heading):
            continue
        commands = _extract_commands(body, 1)
        if commands:
            return commands
    return []


def _zh_ratio(text: str) -> float:
    """中文字符占比，用于判断是否需要翻译。"""
    if not text:
        return 1.0
    zh = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    letters = sum(1 for ch in text if ch.isascii() and ch.isalpha())
    total = zh + letters
    return zh / total if total else 1.0


def _load_translation_cache() -> dict:
    try:
        data = json.loads(_TRANSLATION_CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_translation_cache(cache: dict) -> None:
    try:
        _TRANSLATION_CACHE_PATH.write_text(
            json.dumps(cache, ensure_ascii=False), encoding="utf-8"
        )
    except OSError:
        pass


def _idea_search_llm_config() -> tuple[str, str, str] | None:
    """读想法搜索同一份 LLM 配置（data/profile/llm_config.json）。"""
    cfg_path = PROFILE_DIR / "llm_config.json"
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        key = str(cfg.get("key") or "").strip()
        base_url = str(cfg.get("base_url") or "").strip()
        model = str(cfg.get("model") or "").strip()
        if key and base_url and model:
            return key, base_url, model
    except (OSError, json.JSONDecodeError):
        pass
    return None


def _translate_overview_texts(texts: list[str]) -> dict[str, str]:
    """把英文速览条目批量翻译为中文；带 30 天本地缓存。

    返回 {原文: 译文}。未配置 Key、LLM 失败时返回空 dict——调用方
    展示原文，绝不阻塞搜索。
    """
    pending: list[str] = []
    cache = _load_translation_cache()
    now = time.time()
    result: dict[str, str] = {}
    for text in texts:
        if not text or _zh_ratio(text) >= 0.3:
            continue
        entry = cache.get(text)
        if isinstance(entry, dict) and now - float(entry.get("ts") or 0) < _TRANSLATION_CACHE_TTL:
            zh = str(entry.get("zh") or "").strip()
            if zh:
                result[text] = zh
                continue
        pending.append(text)
    if not pending:
        return result

    cfg = _idea_search_llm_config()
    if cfg is None:
        return result
    key, base_url, model = cfg
    try:
        from openai import OpenAI
        client = OpenAI(api_key=key, base_url=base_url, timeout=_TRANSLATION_TIMEOUT, max_retries=1)
        # 每批最多 6 条，控制延迟与输出稳定性
        for start in range(0, len(pending), 6):
            batch = pending[start:start + 6]
            prompt = (
                "把以下 JSON 字符串数组中的每条开源项目 README 片段翻译成自然、简洁的简体中文。"
                "保留项目名、命令、代码标识、路径和技术名词原文；不要解释，不要编造原文没有的内容。"
                "只输出与输入同顺序、同数量的 JSON 字符串数组。\n\n"
                + json.dumps(batch, ensure_ascii=False)
            )
            response = client.chat.completions.create(
                model=model,
                temperature=0,
                max_tokens=1200,
                messages=[
                    {"role": "system", "content": "你是严谨的软件文档翻译器。"},
                    {"role": "user", "content": prompt},
                ],
            )
            content = (response.choices[0].message.content or "").strip()
            if content.startswith("```"):
                content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content, flags=re.I)
            left, right = content.find("["), content.rfind("]")
            if left < 0 or right <= left:
                raise ValueError("LLM 未返回 JSON 数组")
            arr = json.loads(content[left:right + 1])
            if not isinstance(arr, list) or len(arr) != len(batch):
                raise ValueError("LLM 未返回等长数组")
            for source, zh in zip(batch, arr):
                zh_text = str(zh).strip().replace("\n", " ")
                if zh_text:
                    result[source] = zh_text
                    cache[source] = {"zh": zh_text, "ts": now}
    except Exception as exc:  # noqa: BLE001 - 翻译失败不影响搜索主链路
        logger.info("overview translation unavailable: %s", exc)
        return result
    _save_translation_cache(cache)
    return result


def _localize_overview(data: dict[str, Any]) -> dict[str, Any]:
    """速览中文化：是什么/功能条目可译；needs 已是大白话中文；安装命令保持原样。"""
    texts: list[str] = []
    if data.get("what"):
        texts.append(str(data["what"]))
    texts.extend(str(item) for item in data.get("features") or [])
    if not texts:
        return data
    zh_map = _translate_overview_texts(texts)
    if not zh_map:
        return data
    if data.get("what") and str(data["what"]) in zh_map:
        data["what"] = zh_map[str(data["what"])]
    data["features"] = [zh_map.get(str(item), str(item)) for item in data.get("features") or []]
    return data


def build_overview(
    readme_text: str,
    *,
    terms: list[str],
    plan: dict[str, Any],
    limit: int = 3,
) -> dict[str, Any]:
    """按「这是什么 / 核心功能 / 怎么跑起来 / 跑起来需要」抽取速览；全部失败时降级关键词片段。"""
    sections = split_readme(readme_text)
    overview = _extract_overview(readme_text, sections)
    features = _extract_features(sections, limit)
    quickstart = _extract_quickstart(sections)
    requirements = _extract_requirements(sections)

    result: dict[str, Any] = {
        "kind": "overview",
        "what": overview,
        "features": features,
        "run": quickstart,
        "needs": requirements,
        "source_url": None,
    }
    if overview or features or quickstart:
        return _localize_overview(result)

    # —— 降级：关键词命中的清洗片段（保留原排序逻辑，但只给清洗后的文字）——
    queries = list(dict.fromkeys(terms + list(plan.get("expanded_terms", []))))
    conditions = list(plan.get("must_have", [])) + list(plan.get("preferences", []))
    ranked: list[tuple[int, str, str, list[str]]] = []
    for heading, chunk in sections:
        score, matched = _score_chunk(chunk, queries, conditions)
        if score:
            cleaned = _clean_line(chunk, 200)
            if cleaned:
                ranked.append((score, heading, cleaned, matched))
    ranked.sort(key=lambda item: item[0], reverse=True)
    fallback = ranked[:1]
    return {
        "kind": "overview",
        "what": fallback[0][2] if fallback else None,
        "features": [],
        "run": [],
        "needs": [],
        "source_url": None,
    }


def extract_readme_evidence(
    client: GitHubAPIClient,
    repo: Repository,
    *,
    terms: list[str],
    plan: dict[str, Any],
    limit: int = 2,
) -> list[dict[str, Any]]:
    """获取一个仓库的 README，并返回结构化「项目速览」。

    返回格式（保持 list[dict] 形状以兼容现有管线与测试）：
        [{"section": "overview", "text": <这是什么>,
          "features": [...], "run": [...], "source_url": ...,
          "matched_terms": [...], "score": n}]
    """
    try:
        readme = client.get_readme(repo.owner, repo.name, use_cache=True)
    except Exception:  # README 不存在、限流或网络失败都不能中断整次搜索
        return []
    data = build_overview(readme["text"], terms=terms, plan=plan)
    if not (data.get("what") or data.get("features") or data.get("run")):
        return []
    queries = list(dict.fromkeys(terms + list(plan.get("expanded_terms", []))))
    matched_terms: list[str] = []
    haystack = " ".join([str(data.get("what") or ""), " ".join(data.get("features") or [])]).lower()
    for phrase in queries:
        for word in _keywords(phrase):
            if len(word) > 2 and word in haystack and word not in matched_terms:
                matched_terms.append(word)
            if len(matched_terms) >= 4:
                break
        if len(matched_terms) >= 4:
            break
    text = data.get("what") or ""
    score = 1 if text else 0
    if data.get("features"):
        score += 1
    if data.get("run"):
        score += 1
    return [{
        "section": "overview",
        "text": text,
        "features": data.get("features") or [],
        "run": data.get("run") or [],
        "needs": data.get("needs") or [],
        "source_url": readme["html_url"],
        "matched_terms": matched_terms,
        "score": score,
    }]
