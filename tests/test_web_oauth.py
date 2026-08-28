"""OAuth 登录链路测试（本地 server 模式）。

覆盖：
- 默认配置（无 secret）→ probe configured=false / start 400（前端走设备流）
- 配 secret + Host=localhost → redirect_uri 规范化为 127.0.0.1（GitHub loopback 要求）
- OAUTH_REDIRECT_BASE 覆盖 Host 推导（未来服务器部署铺垫）
- CORS：默认白名单（Pages 域名 + 本机回环），配置后按 Origin 校验

运行：pytest tests/test_web_oauth.py -v
"""
from __future__ import annotations

import http.client
import json
import threading
import time
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

import pytest

import config
from src.web.server import StarRadarHandler


@pytest.fixture()
def oauth_server():
    """起一个本地 server；修改 settings 后测试结束恢复。"""
    saved = {
        "client_id": config.settings.oauth.client_id,
        "client_secret": config.settings.oauth.client_secret,
        "redirect_base": config.settings.oauth.redirect_base,
        "cors_origins": config.settings.cors_origins,
        "access_required": config.settings.access_required,
        "access_code": config.settings.access_code,
    }
    # OAuth 链路测试不覆盖本地 UI 门禁，避免每条既有断言重复登录。
    config.settings.access_required = False
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), StarRadarHandler)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        yield port
    finally:
        httpd.shutdown()
        config.settings.oauth.client_id = saved["client_id"]
        config.settings.oauth.client_secret = saved["client_secret"]
        config.settings.oauth.redirect_base = saved["redirect_base"]
        config.settings.cors_origins = saved["cors_origins"]
        config.settings.access_required = saved["access_required"]
        config.settings.access_code = saved["access_code"]


def req(port: int, path: str, host: str | None = None) -> tuple[int, dict, str]:
    """http.client 不自动跟随重定向，能拿到原始 302 + Location。"""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request("GET", path, headers={"Host": host} if host else {})
    resp = conn.getresponse()
    body = resp.read().decode("utf-8", "replace")
    headers = {k.lower(): v for k, v in resp.getheaders()}
    status = resp.status
    conn.close()
    return status, headers, body


def test_default_config_falls_back_to_device_flow(oauth_server):
    """无 secret 时：probe=false（前端走设备流），start 拒绝。"""
    config.settings.oauth.client_secret = None
    s, _, body = req(oauth_server, "/api/oauth/start?probe=1")
    assert s == 200
    assert json.loads(body)["configured"] is False
    s, _, _ = req(oauth_server, "/api/oauth/start")
    assert s == 400


def test_redirect_uri_normalizes_localhost(oauth_server):
    """配 secret 后：Host=localhost 时 redirect_uri 规范化为 127.0.0.1
    （GitHub 要求 loopback 字面量，否则与注册回调不匹配）。"""
    config.settings.oauth.client_secret = "test_secret"
    s, _, body = req(oauth_server, "/api/oauth/start?probe=1", host="localhost:8970")
    assert json.loads(body)["configured"] is True
    s, h, _ = req(oauth_server, "/api/oauth/start", host="localhost:8970")
    assert s == 302
    assert "redirect_uri=http%3A%2F%2F127.0.0.1%3A8970" in h["location"]
    assert "client_id=Ov23lidxHa5chVTqVBXX" in h["location"]


def test_redirect_base_overrides_host(oauth_server):
    """OAUTH_REDIRECT_BASE 优先于 Host 推导（服务器部署铺垫）。"""
    config.settings.oauth.client_secret = "test_secret"
    config.settings.oauth.redirect_base = "https://example.com"
    s, h, _ = req(oauth_server, "/api/oauth/start", host="localhost:8970")
    assert "redirect_uri=https%3A%2F%2Fexample.com%2Fapi%2Foauth%2Fcallback" in h["location"]


def test_cors_default_whitelist(oauth_server):
    """默认白名单：Pages 域名放行，第三方 Origin 与无 Origin 请求不带 CORS 头。"""
    config.settings.cors_origins = [
        "https://2bingling.github.io", "http://127.0.0.1:8970", "http://localhost:8970",
    ]
    conn = http.client.HTTPConnection("127.0.0.1", oauth_server, timeout=10)
    conn.request("GET", "/api/health")
    resp = conn.getresponse()
    resp.read()
    assert resp.getheader("Access-Control-Allow-Origin") is None
    conn.close()

    conn = http.client.HTTPConnection("127.0.0.1", oauth_server, timeout=10)
    conn.request("GET", "/api/health", headers={"Origin": "https://evil.example"})
    resp = conn.getresponse()
    resp.read()
    assert resp.getheader("Access-Control-Allow-Origin") is None
    conn.close()

    conn = http.client.HTTPConnection("127.0.0.1", oauth_server, timeout=10)
    conn.request("GET", "/api/health", headers={"Origin": "https://2bingling.github.io"})
    resp = conn.getresponse()
    resp.read()
    assert resp.getheader("Access-Control-Allow-Origin") == "https://2bingling.github.io"
    conn.close()


def test_cors_whitelist_restricts_origin(oauth_server):
    """配置 CORS_ORIGINS 后：白名单内 Origin 放行，其他不带 CORS 头。"""
    config.settings.cors_origins = ["https://allowed.example"]
    conn = http.client.HTTPConnection("127.0.0.1", oauth_server, timeout=10)
    conn.request("GET", "/api/health", headers={"Origin": "https://allowed.example"})
    resp = conn.getresponse()
    resp.read()
    assert resp.getheader("Access-Control-Allow-Origin") == "https://allowed.example"
    conn.close()

    conn = http.client.HTTPConnection("127.0.0.1", oauth_server, timeout=10)
    conn.request("GET", "/api/health", headers={"Origin": "https://evil.example"})
    resp = conn.getresponse()
    resp.read()
    assert resp.getheader("Access-Control-Allow-Origin") is None
    conn.close()


def test_access_code_protects_private_api(oauth_server):
    """开启门禁后，必须先验证 0526 才能读取个人 API。"""
    config.settings.access_required = True
    config.settings.access_code = "0526"
    conn = http.client.HTTPConnection("127.0.0.1", oauth_server, timeout=10)
    conn.request("GET", "/api/personal/status")
    assert conn.getresponse().status == 401
    conn.close()

    conn = http.client.HTTPConnection("127.0.0.1", oauth_server, timeout=10)
    conn.request("POST", "/api/access", body=json.dumps({"code": "0526"}), headers={"Content-Type": "application/json"})
    response = conn.getresponse()
    response.read()
    assert response.status == 200
    cookie = response.getheader("Set-Cookie").split(";", 1)[0]
    conn.close()

    conn = http.client.HTTPConnection("127.0.0.1", oauth_server, timeout=10)
    conn.request("GET", "/api/personal/status", headers={"Cookie": cookie})
    assert conn.getresponse().status == 200
    conn.close()


def _post_json(port: int, path: str, payload: dict) -> int:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request("POST", path, body=json.dumps(payload), headers={"Content-Type": "application/json"})
    resp = conn.getresponse()
    resp.read()
    status = resp.status
    conn.close()
    return status


def test_idea_search_gate_and_cache(oauth_server, monkeypatch):
    """想法搜索后端闸：同查询 60s 内命中缓存秒回；不同查询并发收 429。"""
    import threading
    from src.web import server as web_server

    config.settings.access_required = False
    calls = []

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def search_repositories(self, *_args, **_kwargs):
            return SimpleNamespace(items=[])

    def fake_search(query, *, filters=None, deadline=None):
        calls.append(query)
        time.sleep(0.2)  # 模拟真实搜索耗时，确保并发窗口存在
        return {"ok": True, "query": query, "search_terms": [query],
                "evidence_status": "unavailable", "evidence_count": 0,
                "llm_used": False, "items": [], "result_count": 0}

    monkeypatch.setattr(web_server, "_idea_result_cache", {})
    monkeypatch.setattr("src.idea_search.GitHubAPIClient", FakeClient)
    monkeypatch.setattr("src.idea_search.search_ideas", fake_search)

    # 1) 同一查询连发两次：第二次命中缓存秒回，底层只执行一次
    first = _post_json(oauth_server, "/api/idea-search", {"query": "todo app"})
    assert first == 200
    t0 = time.time()
    second = _post_json(oauth_server, "/api/idea-search", {"query": "todo app"})
    assert second == 200
    assert time.time() - t0 < 0.1  # 缓存秒回，不再等 0.2s
    assert calls == ["todo app"]

    # 2) 两笔不同查询并发：一笔执行，另一笔被闸挡住返回 429
    results = {}
    def worker(name, q):
        results[name] = _post_json(oauth_server, "/api/idea-search", {"query": q})
    monkeypatch.setattr(web_server, "_idea_result_cache", {})  # 清缓存避免干扰
    calls.clear()
    t1 = threading.Thread(target=worker, args=("a", "app one"))
    t2 = threading.Thread(target=worker, args=("b", "app two"))
    t1.start(); t2.start(); t1.join(); t2.join()
    assert sorted(results.values()) == [200, 429]
    assert len(calls) == 1  # 闸内只放行了一笔


def test_llm_key_save_and_delete(oauth_server, monkeypatch, tmp_path):
    """POST /api/llm_key 落库（供 --personal 管道）+ DELETE 清除。

    用 tmp_path 隔离 PROFILE_DIR，避免真实 llm_config.json 被测试清除。
    """
    import config
    fake_profile = tmp_path / "profile"
    fake_profile.mkdir()
    monkeypatch.setattr(config, "PROFILE_DIR", fake_profile)
    f = fake_profile / "llm_config.json"
    assert _post_json(oauth_server, "/api/llm_key", {
        "base_url": "https://api.deepseek.com/v1", "key": "sk-test", "model": "deepseek-chat",
    }) == 200
    assert f.is_file()
    saved = json.loads(f.read_text(encoding="utf-8"))
    assert saved["key"] == "sk-test"
    assert saved["base_url"] == "https://api.deepseek.com/v1"
    assert saved["model"] == "deepseek-chat"
    # 缺 key 拒绝
    assert _post_json(oauth_server, "/api/llm_key", {"base_url": "x"}) == 400
    # DELETE 清除
    conn = http.client.HTTPConnection("127.0.0.1", oauth_server, timeout=10)
    conn.request("DELETE", "/api/llm_key")
    resp = conn.getresponse()
    resp.read()
    assert resp.status == 200
    conn.close()
    assert not f.is_file()


def test_personal_pipeline_loads_llm_config(oauth_server, tmp_path, monkeypatch):
    """--personal 管道读取 llm_config.json 覆盖 settings.llm（仅个人管道，公版不受影响）。"""
    from src.personal import pipeline

    saved = (config.settings.llm.api_key, config.settings.llm.base_url, config.settings.llm.model)
    try:
        cfg_file = tmp_path / "llm_config.json"
        cfg_file.write_text(json.dumps({
            "key": "sk-personal", "base_url": "https://api.example.com/v1", "model": "custom-model",
        }), encoding="utf-8")
        monkeypatch.setattr(pipeline, "LLM_CFG_PATH", cfg_file)
        pipeline._load_llm_config()
        assert config.settings.llm.api_key == "sk-personal"
        assert config.settings.llm.base_url == "https://api.example.com/v1"
        assert config.settings.llm.model == "custom-model"
        # 无配置文件时保持原样
        monkeypatch.setattr(pipeline, "LLM_CFG_PATH", tmp_path / "none.json")
        config.settings.llm.api_key = "orig-key"
        pipeline._load_llm_config()
        assert config.settings.llm.api_key == "orig-key"
    finally:
        config.settings.llm.api_key, config.settings.llm.base_url, config.settings.llm.model = saved
