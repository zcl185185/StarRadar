"""想法搜索的离线质量护栏：不访问 GitHub，只验证输入计划和意图规则。"""

import json
import time
from datetime import datetime, timezone
from types import SimpleNamespace

from src import idea_search
from src.idea_search import _intent_rule, _sanitize_plan


def test_sanitize_plan_drops_dirty_values_and_clamps_filters():
    plan = _sanitize_plan({
        "core_queries": ["video editor", 12],
        "must_have": ["Docker"],
        "filters": {
            "languages": ["Python", 7],
            "licenses": ["mit"],
            "active_within_days": "not-a-number",
        },
    })
    assert plan["core_queries"] == ["video editor"]
    assert plan["filters"] == {"languages": ["python"], "licenses": ["MIT"], "active_within_days": 0}


def test_video_intent_has_domain_guardrails():
    rule = _intent_rule("我想做一个 AI 自动剪辑工具")
    assert rule is not None
    assert "video" in rule["include"]
    assert "assignment" in rule["exclude"]


class _FakeProfileDir:
    """In-memory profile directory so token tests do not need Windows Temp."""

    def __init__(self, token: str):
        self.token = token

    def __truediv__(self, _name):
        return self

    def read_text(self, **_kwargs):
        return json.dumps({"login": "octocat", "token": self.token})


def test_idea_search_reuses_locally_saved_github_token(monkeypatch):
    """浏览器登录保存的 token 应优先供本地想法搜索使用。"""
    monkeypatch.setattr(idea_search, "PROFILE_DIR", _FakeProfileDir("local-search-token"))

    assert idea_search._local_github_token() == "local-search-token"


def test_search_passes_local_token_to_search_and_readme_clients(monkeypatch):
    """同一次搜索的仓库与 README 请求都应使用本机登录 token。"""
    monkeypatch.setattr(idea_search, "PROFILE_DIR", _FakeProfileDir("local-search-token"))
    tokens = []

    class FakeGitHubClient:
        def __init__(self, *, token=None, **_kwargs):
            tokens.append(token)

        def search_repositories(self, *_args, **_kwargs):
            return SimpleNamespace(items=[])

    monkeypatch.setattr(idea_search, "GitHubAPIClient", FakeGitHubClient)
    monkeypatch.setattr(idea_search, "_llm_plan", lambda _query: {})
    monkeypatch.setattr(idea_search, "_llm_research", lambda *_args: None)

    result = idea_search.search_ideas("找一个 RSS 阅读器")

    assert result["ok"] is True
    assert tokens == ["local-search-token", "local-search-token"]


def test_explicit_filters_are_a_hard_post_recall_gate():
    repo = SimpleNamespace(
        stars=49, language="Python", license="MIT", archived=False,
        pushed_at=datetime.now(timezone.utc),
    )
    filters = {"min_stars": "50", "updated_days": "30", "language": "Python", "license": "MIT"}

    assert idea_search._matches_explicit_filters(repo, filters) is False


def test_direct_use_requires_readme_usage_evidence():
    assert idea_search._has_usage_evidence([{"section": "Install", "text": "pip install example"}])
    assert not idea_search._has_usage_evidence([{"section": "Overview", "text": "A useful project"}])


def _fake_repo(name="octocat/project"):
    return SimpleNamespace(
        full_name=name,
        description="A test project for rss readers",
        html_url=f"https://github.com/{name}",
        stars=100, forks=10, language="Python", license="MIT",
        topics=["rss"],
        created_at=datetime.now(timezone.utc),
        pushed_at=datetime.now(timezone.utc), archived=False,
    )


def _patch_search_client(monkeypatch, items=None):
    """Mock 掉 GitHub 客户端：搜索返回固定结果，不触网。"""

    class FakeGitHubClient:
        def __init__(self, *, token=None, **_kwargs):
            self.token = token

        def search_repositories(self, *_args, **_kwargs):
            return SimpleNamespace(items=list(items or []))

    monkeypatch.setattr(idea_search, "GitHubAPIClient", FakeGitHubClient)


def test_fallback_terms_dictionary_and_raw_query():
    """降级词典命中中文领域词；无命中时原句兜底；覆盖不足时原句作独立末位候选。"""
    terms = idea_search._fallback_terms("想要一个知识库")
    assert any("knowledge base" in term for term in terms)
    raw = "我想找一个本地部署的备忘录工具"
    fallback = idea_search._fallback_terms(raw)
    assert fallback[-1] == raw  # 原句是独立候选，不与关键词混合
    assert idea_search._fallback_terms("量子波动速读助手") == ["量子波动速读助手"]


def test_deadline_stops_evidence_and_skips_llm(monkeypatch):
    """deadline 已过：跳过 LLM 规划与调研，README 证据阶段直接降级。"""
    _patch_search_client(monkeypatch, items=[_fake_repo()])
    monkeypatch.setattr(idea_search, "PROFILE_DIR", _FakeProfileDir("t"))
    monkeypatch.setattr(idea_search, "extract_readme_evidence", lambda *_a, **_k: [])
    monkeypatch.setattr(idea_search, "_llm_plan", lambda _q: {"core_queries": ["rss reader"]})
    research_called = []
    monkeypatch.setattr(idea_search, "_llm_research", lambda *_a: research_called.append(1) or None)

    result = idea_search.search_ideas("rss 阅读器", deadline=time.time() - 1)

    assert result["ok"] is True
    assert result["evidence_status"] == "unavailable"
    assert result["llm_used"] is False
    assert result["result_count"] == 1  # 元数据结果仍返回
    assert research_called == []  # 调研阶段被跳过


def test_llm_used_flag_reflects_plan(monkeypatch):
    """无 LLM 规划时 llm_used=False，前端据此展示词典模式提示。"""
    _patch_search_client(monkeypatch)
    monkeypatch.setattr(idea_search, "PROFILE_DIR", _FakeProfileDir("t"))
    monkeypatch.setattr(idea_search, "extract_readme_evidence", lambda *_a, **_k: [])
    monkeypatch.setattr(idea_search, "_llm_plan", lambda _q: {})
    monkeypatch.setattr(idea_search, "_llm_research", lambda *_a: None)

    assert idea_search.search_ideas("rss reader")["llm_used"] is False

    monkeypatch.setattr(idea_search, "_llm_plan", lambda _q: {"core_queries": ["rss reader"]})
    assert idea_search.search_ideas("rss reader")["llm_used"] is True


def test_results_carry_matched_terms(monkeypatch):
    """每条结果带 matched_terms，解释第一条为什么排第一。"""
    _patch_search_client(monkeypatch, items=[_fake_repo()])
    monkeypatch.setattr(idea_search, "PROFILE_DIR", _FakeProfileDir("t"))
    monkeypatch.setattr(idea_search, "extract_readme_evidence", lambda *_a, **_k: [])
    monkeypatch.setattr(idea_search, "_llm_plan", lambda _q: {})
    monkeypatch.setattr(idea_search, "_llm_research", lambda *_a: None)

    result = idea_search.search_ideas("rss reader")

    assert result["items"][0]["matched_terms"]  # "rss" / "reader" 命中描述
