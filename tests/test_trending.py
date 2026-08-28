from __future__ import annotations

from src import trending
from src.trending import _TrendingParser


def test_trending_parser_extracts_public_github_fields():
    page = """
    <article class="Box-row">
      <h2><a href="/zcl185185/star-radar"><span>zcl185185 /</span> star-radar</a></h2>
      <p class="col-9 color-fg-muted my-1">A personal GitHub project finder.</p>
      <span itemprop="programmingLanguage">Python</span>
      <a href="/zcl185185/star-radar/stargazers">12,345</a>
      <a href="/zcl185185/star-radar/forks">678</a>
      <span>1,234 stars this week</span>
    </article>
    """
    parser = _TrendingParser()
    parser.feed(page)

    assert parser.items == [{
        "full_name": "zcl185185/star-radar",
        "url": "https://github.com/zcl185185/star-radar",
        "description": "A personal GitHub project finder.",
        "language": "Python",
        "stars": 12345,
        "forks": 678,
        "stars_gained": 1234,
    }]


def test_translated_description_replaces_api_description(monkeypatch):
    """热榜接口默认给前端中文介绍，英文原句仅作为备用字段保留。"""
    monkeypatch.setattr(trending, "_read_translation_cache", lambda: {})
    monkeypatch.setattr(trending, "_write_translation_cache", lambda cache: None)
    monkeypatch.setattr(trending, "_translate_batch", lambda texts: [{
        "translation": "适合个人使用的 GitHub 项目发现工具。",
        "value": "快速发现值得关注的项目",
        "keywords": ["GitHub", "项目发现"],
    }])
    items = [{"description": "A personal GitHub project finder."}]

    assert trending.translate_descriptions(items) is True
    assert items[0]["description"] == "适合个人使用的 GitHub 项目发现工具。"
    assert items[0]["description_zh"] == "适合个人使用的 GitHub 项目发现工具。"
    assert items[0]["description_original"] == "A personal GitHub project finder."
    assert items[0]["value_zh"] == "快速发现值得关注的项目"
    assert items[0]["keywords_zh"] == ["GitHub", "项目发现"]


def test_failed_translation_keeps_original_description(monkeypatch):
    """没有 LLM Key 或翻译失败时不应把“准备中”写入页面数据。"""
    monkeypatch.setattr(trending, "_read_translation_cache", lambda: {})
    monkeypatch.setattr(trending, "_translate_batch", lambda texts: None)
    items = [{"description": "A personal GitHub project finder."}]

    assert trending.translate_descriptions(items) is False
    assert items[0]["description"] == "A personal GitHub project finder."
    assert items[0]["description_zh"] == "A personal GitHub project finder."


def test_translation_parser_accepts_qwen_object_and_chinese_keys():
    result = trending._parse_translation_response(
        '```json\n{"items":[{"中文简介":"终端 AI 编程代理", "一句话价值":"加速命令行开发", "技术关键词":"AI Coding、CLI、Agent"}]}\n```',
        1,
    )
    assert result == [{
        "translation": "终端 AI 编程代理",
        "value": "加速命令行开发",
        "keywords": ["AI Coding", "CLI", "Agent"],
    }]


def test_cached_english_item_requires_llm_enrichment():
    assert trending._cached_items_need_enrichment([{
        "description": "An AI coding agent.",
        "description_original": "An AI coding agent.",
        "description_zh": "AI 编程代理。",
    }]) is True
    assert trending._cached_items_need_enrichment([{
        "description_original": "An AI coding agent.",
        "description_zh": "AI 编程代理。",
        "value_zh": "加速命令行开发",
        "keywords_zh": ["AI Coding"],
    }]) is False
