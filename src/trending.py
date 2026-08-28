"""读取 GitHub Trending 公开页面。

GitHub 没有 Trending 的官方 REST API；这里读取公开的 /trending 页面并只提取其
已经展示的仓库、语言、总 Star 和「本周新增 Star」。不需要用户 Token。
"""
from __future__ import annotations

import html
import json
import logging
import re
import threading
import time
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from config import PROFILE_DIR, settings


_PERIODS = {"daily", "weekly", "monthly"}
_LANGUAGE_RE = re.compile(r"^[A-Za-z0-9+#. -]{1,40}$")
_REPO_PATH_RE = re.compile(r"^/([\w.-]+)/([\w.-]+)$")
_NUMBER_RE = re.compile(r"([\d,]+)")
_GAIN_RE = re.compile(r"([\d,]+)\s+stars?\s+this\s+(?:week|month|day)", re.I)
_CACHE_SECONDS = 10 * 60
_cache: dict[tuple[str, str], tuple[float, dict]] = {}
_cache_lock = threading.Lock()
_TRANSLATION_CACHE_PATH = PROFILE_DIR / "trending_translations.json"
logger = logging.getLogger(__name__)


def _num(value: str) -> int:
    match = _NUMBER_RE.search(value or "")
    return int(match.group(1).replace(",", "")) if match else 0


def _read_translation_cache() -> dict[str, dict[str, object]]:
    try:
        data = json.loads(_TRANSLATION_CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    cache: dict[str, dict[str, object]] = {}
    for key, value in data.items():
        if not str(key).strip():
            continue
        # 兼容旧版仅缓存中文简介的 {原文: "译文"} 格式。
        if isinstance(value, str) and value.strip():
            cache[str(key)] = {"translation": value.strip(), "value": "", "keywords": []}
        elif isinstance(value, dict) and isinstance(value.get("translation"), str) and value["translation"].strip():
            keywords = value.get("keywords")
            cache[str(key)] = {
                "translation": value["translation"].strip(),
                "value": str(value.get("value") or "").strip(),
                "keywords": [str(item).strip() for item in keywords if str(item).strip()][:5]
                if isinstance(keywords, list) else [],
            }
    return cache


def _write_translation_cache(cache: dict[str, dict[str, object]]) -> None:
    try:
        _TRANSLATION_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _TRANSLATION_CACHE_PATH.write_text(
            json.dumps(dict(list(cache.items())[-500:]), ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError:
        pass  # 翻译缓存失效不应影响热榜本身


_LLM_BASE_URLS = {
    "deepseek": "https://api.deepseek.com",
    "glm": "https://open.bigmodel.cn/api/paas/v4",
    "qwen": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "moonshot": "https://api.moonshot.cn/v1",
}


def _llm_base_url() -> str:
    if settings.llm.base_url:
        return settings.llm.base_url
    model = settings.llm.model.lower()
    return next((url for prefix, url in _LLM_BASE_URLS.items() if model.startswith(prefix)), "https://api.openai.com/v1")


def _parse_translation_response(content: str, expected_count: int) -> list[dict[str, object]]:
    """兼容 OpenAI 兼容模型常见的数组 / 对象包装 / Markdown JSON 返回。"""
    cleaned = content.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.I)
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError:
        # 某些模型会在 JSON 前后附一句说明；提取最外层数组再解析。
        left, right = cleaned.find("["), cleaned.rfind("]")
        if left < 0 or right <= left:
            raise ValueError("LLM 未返回 JSON 数组")
        payload = json.loads(cleaned[left:right + 1])
    if isinstance(payload, dict):
        payload = payload.get("items") or payload.get("translations") or payload.get("results")
    if not isinstance(payload, list) or len(payload) != expected_count:
        raise ValueError("LLM 未返回等长的信息数组")

    parsed: list[dict[str, object]] = []
    for item in payload:
        if not isinstance(item, dict):
            raise ValueError("LLM 返回的项目不是对象")
        # Qwen 有时会遵循中文字段名；两种格式都接受。
        translation = item.get("translation") or item.get("description") or item.get("中文简介")
        value = item.get("value") or item.get("one_line_value") or item.get("一句话价值") or ""
        keywords = item.get("keywords") or item.get("技术关键词") or []
        if not isinstance(translation, str) or not translation.strip():
            raise ValueError("LLM 返回缺少中文简介")
        if isinstance(keywords, str):
            keywords = re.split(r"[、,，;；]\s*", keywords)
        parsed.append({
            "translation": translation.strip().replace("\n", " ")[:260],
            "value": str(value).strip().replace("\n", " ")[:60],
            "keywords": [str(word).strip()[:24] for word in keywords if str(word).strip()][:4]
            if isinstance(keywords, list) else [],
        })
    return parsed


def _translate_batch(descriptions: list[str]) -> list[dict[str, object]] | None:
    """用 LLM 生成中文简介、价值点和关键词，并保留原文缓存键。"""
    if not descriptions:
        return []
    if not settings.llm.enabled or not settings.llm.api_key:
        logger.info("LLM 未配置，保留项目简介原文")
        return None
    try:
        from openai import OpenAI
    except ImportError:
        logger.warning("openai 库未安装，保留项目简介原文")
        return None

    client = OpenAI(api_key=settings.llm.api_key, base_url=_llm_base_url())
    translated: list[dict[str, object]] = []
    # 每批最多 8 条，兼顾 API 上下文长度、延迟和输出稳定性。
    for start in range(0, len(descriptions), 8):
        batch = descriptions[start:start + 8]
        prompt = (
            "分析以下 GitHub 项目简介，并为每项输出："
            "translation（自然简洁的简体中文简介）、value（不超过28字的一句话价值）、"
            "keywords（2-4个中文或通用技术关键词）。"
            "保留项目名、技术名、链接、Markdown 和代码标识；不要编造未提及的能力。"
            "必须只返回与输入相同顺序、相同数量的 JSON 对象数组。\n\n"
            + json.dumps(batch, ensure_ascii=False)
        )
        try:
            response = client.chat.completions.create(
                model=settings.llm.model,
                messages=[
                    {"role": "system", "content": "你是严谨的软件文档翻译器。"},
                    {"role": "user", "content": prompt},
                ],
                temperature=0,
                max_tokens=900,
            )
            content = response.choices[0].message.content or ""
            translated.extend(_parse_translation_response(content, len(batch)))
        except Exception as exc:  # 翻译失败不影响 Trending 榜单
            logger.warning("LLM 项目简介翻译失败: %s", exc)
            return None
    return translated


def translate_descriptions(items: list[dict]) -> bool:
    """写入 ``description_zh``；返回是否成功得到中文翻译。"""
    cache = _read_translation_cache()
    missing: list[str] = []
    for item in items:
        source = str(item.get("description") or "").strip()
        if not source:
            item["description_zh"] = "暂无项目介绍"
        elif re.search(r"[\u4e00-\u9fff]", source):
            item["description_zh"] = source
        elif source in cache:
            cached = cache[source]
            # 旧缓存只有译文；首次升级时补齐价值点与关键词，之后继续命中缓存。
            if (not cached.get("value") or not cached.get("keywords")) and source not in missing:
                missing.append(source)
        elif source not in missing:
            missing.append(source)
    translated = _translate_batch(missing)
    if translated:
        cache.update(dict(zip(missing, translated)))
        _write_translation_cache(cache)
    for item in items:
        source = str(item.get("description") or "").strip()
        cached = cache.get(source)
        if cached:
            item["description_zh"] = str(cached["translation"])
            item["value_zh"] = str(cached.get("value") or "")
            item["keywords_zh"] = cached.get("keywords") or []
        elif not item.get("description_zh"):
            # LLM 未配置或请求失败时展示原文，绝不留下会永久卡住的占位文本。
            item["description_zh"] = source
        # 对 API 消费方默认返回中文；LLM 不可用时回退原文。
        if source:
            item["description_original"] = source
        item["description"] = item["description_zh"]
    return bool(translated) or not missing


class _TrendingParser(HTMLParser):
    """面向 GitHub Trending 当前语义标记的轻量解析器。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.items: list[dict] = []
        self.current: dict | None = None
        self.stack: list[tuple[str, dict]] = []
        self.h2_depth = 0
        self.description_depth = 0
        self.language_depth = 0
        self.active_link: str = ""

    def handle_starttag(self, tag: str, attrs_list: list[tuple[str, str | None]]) -> None:
        attrs = {k: v or "" for k, v in attrs_list}
        classes = attrs.get("class", "")
        self.stack.append((tag, attrs))
        if tag == "article" and "Box-row" in classes:
            self.current = {"texts": [], "description": [], "language": [], "stars": [], "forks": [], "repo": ""}
            return
        if not self.current:
            return
        if tag == "h2":
            self.h2_depth += 1
        if tag == "p" and "color-fg-muted" in classes:
            self.description_depth += 1
        if attrs.get("itemprop") == "programmingLanguage":
            self.language_depth += 1
        if tag == "a":
            href = attrs.get("href", "")
            match = _REPO_PATH_RE.match(href)
            if self.h2_depth and match:
                self.current["repo"] = f"{match.group(1)}/{match.group(2)}"
                self.active_link = "repo"
            elif href.endswith("/stargazers"):
                self.active_link = "stars"
            elif href.endswith("/forks"):
                self.active_link = "forks"
            else:
                self.active_link = ""

    def handle_endtag(self, tag: str) -> None:
        if not self.stack:
            return
        closing, _ = self.stack.pop()
        if not self.current:
            return
        if tag == "a":
            self.active_link = ""
        if tag == "h2" and self.h2_depth:
            self.h2_depth -= 1
        if tag == "p" and self.description_depth:
            self.description_depth -= 1
        if self.language_depth and closing in {"span", "a", "div"}:
            # itemprop 本身的 span 结束时收口；下一段文本不会误记作语言。
            self.language_depth -= 1
        if tag == "article":
            item = self._finish_current()
            if item:
                self.items.append(item)
            self.current = None

    def handle_data(self, data: str) -> None:
        if not self.current:
            return
        text = " ".join(data.split())
        if not text:
            return
        self.current["texts"].append(text)
        if self.description_depth:
            self.current["description"].append(text)
        if self.language_depth:
            self.current["language"].append(text)
        if self.active_link in {"stars", "forks"}:
            self.current[self.active_link].append(text)

    def _finish_current(self) -> dict | None:
        assert self.current is not None
        repo = str(self.current["repo"] or "")
        if not repo:
            return None
        whole_text = " ".join(self.current["texts"])
        gain_match = _GAIN_RE.search(whole_text)
        return {
            "full_name": repo,
            "url": f"https://github.com/{repo}",
            "description": html.unescape(" ".join(self.current["description"])).strip(),
            "language": " ".join(self.current["language"]).strip(),
            "stars": _num(" ".join(self.current["stars"])),
            "forks": _num(" ".join(self.current["forks"])),
            "stars_gained": _num(gain_match.group(1)) if gain_match else 0,
        }


def _cached_items_need_enrichment(items: list[dict]) -> bool:
    """判断旧缓存是否缺少 LLM 轻量信息卡字段。"""
    for item in items:
        source = str(item.get("description_original") or item.get("description") or "").strip()
        if source and not re.search(r"[\u4e00-\u9fff]", source):
            if not item.get("description_zh") or not item.get("value_zh") or not item.get("keywords_zh"):
                return True
    return False


def _enrich_cached_items(items: list[dict]) -> bool:
    """以保留的英文原文补全旧缓存，不重新请求 GitHub。"""
    for item in items:
        original = str(item.get("description_original") or item.get("description") or "").strip()
        if original:
            item["description"] = original
            item.pop("description_zh", None)
            item.pop("value_zh", None)
            item.pop("keywords_zh", None)
    return translate_descriptions(items)


def fetch_trending(*, since: str = "weekly", language: str = "", force: bool = False) -> dict:
    """取 GitHub Trending；相同筛选缓存 10 分钟，避免重复请求页面。"""
    since = since.lower().strip()
    if since not in _PERIODS:
        raise ValueError("since must be daily, weekly or monthly")
    language = language.strip()
    if language and not _LANGUAGE_RE.fullmatch(language):
        raise ValueError("invalid language")
    key = (since, language.lower())
    with _cache_lock:
        cached = _cache.get(key)
        if cached and not force and time.time() - cached[0] < _CACHE_SECONDS:
            payload = cached[1]
            # 服务启动前缓存的英文结果也应在 LLM 配置就绪后立即补全；
            # 无需等缓存过期，更不需要再次请求 GitHub。
            if settings.llm.enabled and settings.llm.api_key and _cached_items_need_enrichment(payload.get("items") or []):
                payload["translated"] = _enrich_cached_items(payload["items"])
            return {**payload, "cached": True}

    params = {"since": since}
    if language:
        params["l"] = language
    url = "https://github.com/trending?" + urlencode(params)
    request = Request(url, headers={"User-Agent": "StarRadar/1.0 (+local personal use)", "Accept-Language": "en-US,en;q=0.8"})
    with urlopen(request, timeout=20) as response:
        page = response.read().decode("utf-8", "replace")
    parser = _TrendingParser()
    parser.feed(page)
    translated = translate_descriptions(parser.items)
    # 用户浏览时优先看到经过长期验证的项目；热度仍保留为右侧的本周新增 Star。
    parser.items.sort(key=lambda item: (-int(item.get("stars") or 0), str(item.get("full_name") or "").lower()))
    payload = {
        "ok": True,
        "source": "GitHub Trending",
        "source_url": url,
        "since": since,
        "language": language,
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "cached": False,
        "translated": translated,
        "sort": "stars_desc",
        "items": parser.items,
    }
    with _cache_lock:
        _cache[key] = (time.time(), payload)
    return payload
