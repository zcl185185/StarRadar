# -*- coding: utf-8 -*-
import sys
sys.path.insert(0, r"D:\star\StarRadar-main")
sys.stdout.reconfigure(encoding="utf-8")
from src.search.readme_evidence import split_readme, build_overview

README = """
# SuperRAG

面向私有文档的检索增强问答系统。

## Features

- 多格式文档解析（PDF / Markdown / Word）
- 混合检索（BM25 + 向量）

## Requirements

- Python 3.10 or higher
- At least 16GB RAM recommended
- OpenAI API Key required

## Installation

```bash
pip install -r requirements.txt
python app.py
```
"""
r = build_overview(README, terms=["rag"], plan={})
print("needs:", r["needs"])
assert any("Python 3.10" in n for n in r["needs"]), r["needs"]
assert any("16 GB" in n for n in r["needs"]), r["needs"]
assert any("API Key" in n for n in r["needs"]), r["needs"]
print("needs test passed")
