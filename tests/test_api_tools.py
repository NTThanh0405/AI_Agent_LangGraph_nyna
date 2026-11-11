# tests/test_api_tools.py
# Kiểm tra tool lấy dữ liệu qua API của các khối source=api (app/agent/tools/api_tools.py), dùng cấu hình
# tương lai có house/misa (fixture api_scopes_config) vì cấu hình thật hiện chỉ có coffee.
# Phần gọi HTTP thật chạy với một server aiohttp cục bộ (127.0.0.1), không ra Internet.

import asyncio
import pytest
from aiohttp import web

from app.agent import grants
from app.agent.tools import api_tools as A


def run(coro):
    return asyncio.run(coro)


def cfg_for(role, uid="7"):
    return {"configurable": {"user_role": role, "user_id": uid}}


@pytest.fixture
def env(monkeypatch, api_scopes_config):
    """Khối được cấp giả + biến môi trường + request HTTP giả (ghi lại để kiểm tra)."""
    granted: dict[str, set] = {}
    calls: list = []

    async def fake_get_grants(uid):
        return set(granted.get(str(uid), set()))

    async def fake_http(method, url, *, params, headers, timeout):
        calls.append({"method": method, "url": url, "params": params, "headers": headers, "timeout": timeout})
        return {"data": [{"day": "2026-11-10", "revenue": 1500000}], "meta": {"x": 1}}

    monkeypatch.setattr(grants, "get_grants", fake_get_grants)
    monkeypatch.setattr(A, "_http_request", fake_http)
    for k, v in {"TEST_HOUSE_URL": "https://house.example.com/", "TEST_HOUSE_TOKEN": "h-secret",
                 "TEST_MISA_URL": "https://misa.example.com", "TEST_MISA_TOKEN": "m-secret"}.items():
        monkeypatch.setenv(k, v)
    return {"granted": granted, "calls": calls}


async def call(scope, endpoint, start, end, role="staff", uid="7"):
    return await A.call_business_api.ainvoke(
        {"scope": scope, "endpoint": endpoint, "start_date": start, "end_date": end}, cfg_for(role, uid))


# ---------------------------------------------------------------- quyền
def test_staff_without_scope_cannot_call(env):
    out = run(call("house", "revenue_by_day", "2026-11-01", "2026-11-07"))
    assert "không có quyền xem khối house" in out and env["calls"] == []


def test_staff_with_house_can_call_house_but_not_misa(env):
    env["granted"]["7"] = {"house"}
    out = run(call("house", "revenue_by_day", "2026-11-01", "2026-11-07"))
    assert "Dữ liệu API khối house" in out and '"revenue":1500000' in out
    assert '"meta"' not in out                     # result_path = data
    out = run(call("misa", "revenue_summary", "2026-11-01", "2026-11-07"))
    assert "không có quyền xem khối misa" in out and len(env["calls"]) == 1


def test_coffee_grant_does_not_open_api_scopes(env):
    env["granted"]["7"] = {"coffee"}
    assert "không có quyền" in run(call("house", "revenue_by_day", "2026-11-01", "2026-11-02"))


@pytest.mark.parametrize("role", ["boss", "manager"])
def test_boss_and_manager_can_call_every_api(env, role):
    assert "Dữ liệu API khối misa" in run(call("misa", "revenue_summary", "2026-11-01", "2026-11-30", role=role))


def test_unknown_role_and_missing_user(env):
    env["granted"][""] = {"house"}
    assert "không có quyền" in run(call("house", "revenue_by_day", "2026-11-01", "2026-11-02", role="hacker"))
    assert "không có quyền" in run(call("house", "revenue_by_day", "2026-11-01", "2026-11-02", uid=""))


# ---------------------------------------------------------------- chỉ gọi đúng endpoint khai báo
def test_request_is_built_from_config_only(env):
    run(call("house", "revenue_by_day", "2026-11-01", "2026-11-07", role="boss"))
    c = env["calls"][-1]
    assert c["method"] == "GET" and c["url"] == "https://house.example.com/rev"
    assert c["params"] == {"from": "2026-11-01", "to": "2026-11-07"}
    assert c["headers"]["Authorization"] == "Bearer h-secret"
    run(call("misa", "revenue_summary", "2026-11-01", "2026-11-07", role="boss"))
    c = env["calls"][-1]
    assert c["method"] == "POST" and c["url"] == "https://misa.example.com/sum"
    assert c["params"] == {"from_date": "01/11/2026", "to_date": "07/11/2026", "token": "m-secret"}


@pytest.mark.parametrize("scope,endpoint,msg", [
    ("coffee", "x", "không phải khối dữ liệu API"),
    ("kho", "x", "không phải khối dữ liệu API"),
    ("house", "../admin", "không có endpoint"),
    ("house", "https://evil.com", "không có endpoint"),
])
def test_unknown_scope_or_endpoint(env, scope, endpoint, msg):
    out = run(call(scope, endpoint, "2026-11-01", "2026-11-02", role="boss"))
    assert msg in out and env["calls"] == []


@pytest.mark.parametrize("start,end,msg", [
    ("2026-11-07", "2026-11-01", "trước hoặc bằng"),
    ("2026-01-01", "2026-03-01", "tối đa 31 ngày"),
    ("01/11/2026", "2026-11-02", "YYYY-MM-DD"),
    ("hôm qua", "2026-11-02", "YYYY-MM-DD"),
])
def test_bad_dates(env, start, end, msg):
    out = run(call("house", "revenue_by_day", start, end, role="boss"))
    assert msg in out and env["calls"] == []


def test_not_configured_connector(env, monkeypatch):
    monkeypatch.delenv("TEST_HOUSE_URL")
    monkeypatch.setattr(A, "_dotenv", lambda: {})
    out = run(call("house", "revenue_by_day", "2026-11-01", "2026-11-02", role="boss"))
    assert "chưa được cấu hình (TEST_HOUSE_URL trống)" in out and env["calls"] == []
    monkeypatch.setenv("TEST_HOUSE_URL", "https://house.example.com")
    monkeypatch.delenv("TEST_HOUSE_TOKEN")
    out = run(call("house", "revenue_by_day", "2026-11-01", "2026-11-02", role="boss"))
    assert "thiếu khoá (TEST_HOUSE_TOKEN trống)" in out and "h-secret" not in out


def test_long_result_is_truncated(env, monkeypatch):
    async def big(*a, **k):
        return {"data": ["x" * 100] * 1000}
    monkeypatch.setattr(A, "_http_request", big)
    out = run(call("house", "revenue_by_day", "2026-11-01", "2026-11-02", role="boss"))
    assert "đã cắt bớt" in out and len(out) < A.MAX_OUTPUT_CHARS + 500


def test_list_business_apis(env, monkeypatch):
    env["granted"]["7"] = {"house", "coffee"}
    out = run(A.list_business_apis.ainvoke({}, cfg_for("staff")))
    assert "Khối house (Nyna House)" in out and "revenue_by_day: Doanh thu House theo ngày" in out
    assert "misa" not in out
    monkeypatch.delenv("TEST_MISA_URL")
    monkeypatch.setattr(A, "_dotenv", lambda: {})
    out = run(A.list_business_apis.ainvoke({}, cfg_for("boss")))
    assert "Khối misa (Kế toán MISA) - CHƯA CẤU HÌNH" in out
    assert run(A.list_business_apis.ainvoke({}, cfg_for("staff", "9"))) == \
        "Người hỏi không có quyền khối dữ liệu API nào."


# ---------------------------------------------------------------- HTTP thật với server cục bộ
async def _with_server(handler, body):
    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        return await body(f"http://127.0.0.1:{port}")
    finally:
        await runner.cleanup()


def test_http_request_against_local_server():
    seen = {}

    async def handler(request):
        seen.update(method=request.method, path=request.path, query=dict(request.query),
                    auth=request.headers.get("Authorization"),
                    json=await request.json() if request.method == "POST" else None)
        if request.path == "/redirect":
            raise web.HTTPFound("http://evil.example.com/")
        if request.path == "/err":
            return web.Response(status=500, text="boom")
        if request.path == "/html":
            return web.Response(text="<html>")
        return web.json_response({"ok": True, "rows": [1, 2]})

    async def body(base):
        out = await A._http_request("GET", base + "/a", params={"from": "2026-11-01"},
                                    headers={"Authorization": "Bearer t"}, timeout=5)
        assert out == {"ok": True, "rows": [1, 2]}
        assert seen == {"method": "GET", "path": "/a", "query": {"from": "2026-11-01"},
                        "auth": "Bearer t", "json": None}
        await A._http_request("POST", base + "/b", params={"x": "1"}, headers={}, timeout=5)
        assert seen["json"] == {"x": "1"} and seen["query"] == {}
        for path, msg in [("/redirect", "HTTP 302"), ("/err", "HTTP 500"), ("/html", "không phải JSON")]:
            with pytest.raises(A.ApiCallError, match=msg):
                await A._http_request("GET", base + path, params={}, headers={}, timeout=5)

    run(_with_server(handler, body))


def test_http_unreachable():
    with pytest.raises(A.ApiCallError, match="không kết nối được API"):
        run(A._http_request("GET", "http://127.0.0.1:9/x", params={}, headers={}, timeout=3))

