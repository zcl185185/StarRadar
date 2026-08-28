# -*- coding: utf-8 -*-
"""项目速览（overview）抽取的单测：不触网，用内联 README 验证意图抽取。"""
import io
import sys

sys.path.insert(0, r"D:\star\StarRadar-main")

from src.search.readme_evidence import (  # noqa: E402
    _extract_commands,
    _extract_features,
    _extract_overview,
    _strip_markup,
    build_overview,
    split_readme,
)

README = """
# TechDigest

![badge](https://img.shields.io/badge/build-passing-brightgreen)

[![Stars](https://img.shields.io/github/stars/x)](https://github.com/x)

抓取多源技术新闻，AI 生成每日摘要并推送到你的邮箱与 Telegram。

## ✨ Features

- **多源聚合**：支持 RSS / GitHub Trending / HN 等十余个来源
- **AI 摘要**：调用 OpenAI 兼容接口生成中文日报
- **定时推送**：每天 08:00 自动发送

## Installation

```bash
pip install -r requirements.txt
python main.py --serve
```

配置 `config.yaml` 中的 API Key 后即可运行。

## License

MIT
"""


def test_strip_markup_removes_badges_and_emoji():
    dirty = "[![build](https://img.shields.io/x)](https://github.com) ✨ 抓取多源技术新闻"
    cleaned = _strip_markup(dirty)
    assert "shields.io" not in cleaned
    assert "抓取多源技术新闻" in cleaned


def test_extract_overview_picks_leading_paragraph():
    overview = _extract_overview(README, split_readme(README))
    assert overview is not None
    assert "每日摘要" in overview
    assert "shields" not in overview


def test_extract_features_reads_bullet_lists():
    features = _extract_features(_split(README), 3)
    assert len(features) == 3
    assert any("多源聚合" in item for item in features)
    assert not any(item.startswith("-") for item in features)


def test_extract_quickstart_reads_commands():
    commands = _extract_commands(_section(README, "Installation"))
    assert any("pip install" in command for command in commands)


def test_build_overview_full():
    result = build_overview(README, terms=["daily digest"], plan={})
    assert result["kind"] == "overview"
    assert result["what"]
    assert len(result["features"]) >= 2
    assert result["run"]
    assert result["run"][0].startswith("pip")


def test_build_overview_fallback_on_flat_readme():
    flat = "A tiny tool that syncs rss feeds into a local sqlite database every morning."
    result = build_overview(flat, terms=["rss sync"], plan={})
    assert result["kind"] == "overview"
    assert "rss" in result["what"].lower() or "sync" in result["what"].lower()
    assert result["features"] == []


def _split(text):
    from src.search.readme_evidence import split_readme
    return split_readme(text)


def _section(text, name):
    from src.search.readme_evidence import split_readme
    for heading, body in split_readme(text):
        if name.lower() in heading.lower():
            return body
    return text


if __name__ == "__main__":
    test_strip_markup_removes_badges_and_emoji()
    test_extract_overview_picks_leading_paragraph()
    test_extract_features_reads_bullet_lists()
    test_extract_quickstart_reads_commands()
    test_build_overview_full()
    test_build_overview_fallback_on_flat_readme()
    print("all overview tests passed")
