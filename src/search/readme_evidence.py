"""从 GitHub README 提取可展示的搜索证据。

这是想法搜索的第一层 RAG：不把 README 全文直接交给模型，而是先按标题
切分、用检索计划定位相关片段，再将有限的原文证据连同来源链接返回。
"""
from __future__ import annotations

import re
from typing import Any

from src.collector.github_api import GitHubAPIClient, Repository

_HEADING_RE = re.compile(r"(?m)^#{1,3}\s+(.+?)\s*#*\s*$")
_WORD_RE = re.compile(r"[a-z][a-z0-9+#.-]{1,}")
_IGNORED_WORDS = {"ai", "app", "tool", "project", "software", "open", "source"}


def _compact(text: str, limit: int = 460) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def split_readme(text: str, max_chunk_chars: int = 1_400) -> list[tuple[str, str]]:
    """优先按 Markdown 标题切块；超长章节再按段落拆开。"""
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
            sections.append((_compact(match.group(1), 100), text[match.end():end]))

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


def extract_readme_evidence(
    client: GitHubAPIClient,
    repo: Repository,
    *,
    terms: list[str],
    plan: dict[str, Any],
    limit: int = 2,
) -> list[dict[str, Any]]:
    """获取一个仓库的 README，并返回最相关的有限证据片段。"""
    try:
        readme = client.get_readme(repo.owner, repo.name, use_cache=True)
    except Exception:  # README 不存在、限流或网络失败都不能中断整次搜索
        return []
    queries = list(dict.fromkeys(terms + list(plan.get("expanded_terms", []))))
    conditions = list(plan.get("must_have", [])) + list(plan.get("preferences", []))
    ranked: list[tuple[int, str, str, list[str]]] = []
    for heading, chunk in split_readme(readme["text"]):
        score, matched = _score_chunk(chunk, queries, conditions)
        if score:
            ranked.append((score, heading, _compact(chunk), matched))
    ranked.sort(key=lambda item: item[0], reverse=True)
    return [
        {
            "section": heading,
            "text": chunk,
            "source_url": readme["html_url"],
            "matched_terms": matched[:4],
            "score": score,
        }
        for score, heading, chunk, matched in ranked[:limit]
    ]
