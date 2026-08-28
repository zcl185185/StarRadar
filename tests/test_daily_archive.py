from __future__ import annotations

from src.personal import daily_archive


def _item(name: str) -> dict:
    owner, repo = name.split("/", 1)
    return {
        "repo": {"full_name": name, "owner": owner, "name": repo, "stars": 10},
        "score": {"score": 66.0, "explanation": "测试项目"},
    }


def test_each_refresh_keeps_an_immutable_daily_snapshot(tmp_path, monkeypatch):
    """同一天连续刷新也保留两次结果，第二次能标出相对上一次的新项目。"""
    monkeypatch.setattr(daily_archive, "ARCHIVE_DIR", tmp_path / "daily_history")
    first = daily_archive.save_daily_archive({
        "generated_at": "2026-08-25T01:00:00+00:00",
        "login": "zhao",
        "items": [_item("acme/one"), _item("acme/two")],
    })
    second = daily_archive.save_daily_archive({
        "generated_at": "2026-08-25T02:00:00+00:00",
        "login": "zhao",
        "items": [_item("acme/two"), _item("acme/three")],
    })

    directory = daily_archive.list_daily_archives()
    assert len(directory) == 2
    assert first["archive_id"] != second["archive_id"]
    assert daily_archive.load_daily_archive(first["archive_id"])["items"][0]["repo"]["full_name"] == "acme/one"
    assert second["comparison"] == {
        "compared_to": first["archive_id"],
        "new": 1,
        "returned": 0,
        "up": 1,
        "down": 0,
        "steady": 0,
        "disappeared": 1,
    }
    assert second["items"][0]["daily_history"]["status"] == "up"
    assert second["items"][1]["daily_history"]["status"] == "new"


def test_archive_loader_rejects_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(daily_archive, "ARCHIVE_DIR", tmp_path)
    assert daily_archive.load_daily_archive("../scores") is None
