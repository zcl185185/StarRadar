"""本机 GitHub 星标接口的离线测试。"""

import json

from src.web import server


class _ProfileDir:
    def __truediv__(self, _name):
        return self

    def read_text(self, **_kwargs):
        return json.dumps({"login": "octocat", "token": "local-token"})


class _Response:
    headers = {"Link": '<https://api.github.com/user/starred?page=2>; rel="next"'}

    def read(self):
        return json.dumps([{
            "starred_at": "2026-08-28T00:00:00Z",
            "repo": {
                "full_name": "octocat/hello-world",
                "html_url": "https://github.com/octocat/hello-world",
                "description": "A demo repository",
                "stargazers_count": 42,
                "language": "Python",
                "topics": ["demo"],
                "pushed_at": "2026-08-27T00:00:00Z",
            },
        }]).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def test_github_starred_page_is_paginated_and_never_returns_token(monkeypatch):
    seen = {}

    def fake_urlopen(request, timeout):
        seen["authorization"] = request.get_header("Authorization")
        seen["url"] = request.full_url
        assert timeout == 20
        return _Response()

    monkeypatch.setattr(server, "PROFILE_DIR", _ProfileDir())
    monkeypatch.setattr(server, "urlopen", fake_urlopen)

    payload = server._github_starred_page(1, 30)

    assert seen["authorization"] == "Bearer local-token"
    assert "sort=created" in seen["url"]
    assert payload["has_next"] is True
    assert payload["items"][0]["starred_at"] == "2026-08-28T00:00:00Z"
    assert "token" not in payload["items"][0]
