"""学习档案的数据层：项目、日志与仪表盘必须可在本机 SQLite 中追溯。"""
from __future__ import annotations

from src.profile import feedback_collector as collector
from src.web import server


def test_learning_archive_project_log_and_dashboard(tmp_path, monkeypatch):
    monkeypatch.setattr(collector, "MEMORY_DB", tmp_path / "memory.db")
    project = collector.create_learning_project({
        "name": "AI 文档问答助手",
        "source_url": "https://github.com/example/rag-demo",
        "source_kind": "GitHub",
        "project_type": "AI 应用",
        "tech_stack": ["Next.js", "FastAPI", "OpenAI API"],
        "status": "复刻中",
        "goal": "理解 RAG 的完整数据流",
        "readme_summary": "上传 PDF，检索文档后带引用回答。",
    })
    assert project["id"] > 0
    assert project["tech_stack"] == ["Next.js", "FastAPI", "OpenAI API"]

    log = collector.add_learning_log(project["id"], {
        "logged_on": "2026-08-29", "duration_minutes": 90,
        "completed": "跑通文本切分", "blockers": "理解 chunk overlap",
        "changed_files": "src/parser.py", "next_step": "比较检索结果",
    })
    assert log["duration_minutes"] == 90
    saved = collector.get_learning_project(project["id"])
    assert saved and saved["status"] == "复刻中"
    assert collector.list_learning_logs(project["id"])[0]["completed"] == "跑通文本切分"

    dashboard = collector.learning_dashboard()
    assert dashboard["project_count"] == 1
    assert dashboard["in_progress"] == 1
    assert ("Next.js", 1) in dashboard["stack_distribution"]


def test_github_learning_import_collects_bounded_evidence(monkeypatch):
    def fake_json(url):
        if url.endswith("/git/trees/main?recursive=1"):
            return {"tree": [
                {"type": "blob", "path": "package.json"},
                {"type": "blob", "path": "src/app/page.tsx"},
                {"type": "blob", "path": "README.md"},
            ]}
        return {"name": "rag-demo", "html_url": "https://github.com/acme/rag-demo", "description": "Document Q&A", "default_branch": "main", "language": "TypeScript"}

    def fake_text(owner, repo, path, branch):
        if path == "README.md":
            return "Upload documents and ask questions with citations."
        if path == "package.json":
            return '{"dependencies":{"next":"15","openai":"4","@prisma/client":"6"}}'
        return ""

    monkeypatch.setattr(server, "_github_learning_json", fake_json)
    monkeypatch.setattr(server, "_github_learning_text", fake_text)
    result = server.inspect_github_learning_source("https://github.com/acme/rag-demo")

    assert result["draft"]["name"] == "rag-demo"
    assert result["draft"]["project_type"] == "AI 应用"
    assert {"Next.js", "OpenAI API", "Prisma"}.issubset(result["draft"]["tech_stack"])
    assert "【README】" in result["draft"]["readme_summary"]
    assert result["scan"]["files_read"] == ["package.json"]


def test_github_learning_import_rejects_non_repository_url():
    try:
        server._github_repo_from_url("https://example.com/not-a-repository")
    except ValueError as error:
        assert "GitHub" in str(error)
    else:
        raise AssertionError("non-GitHub URL must be rejected")
