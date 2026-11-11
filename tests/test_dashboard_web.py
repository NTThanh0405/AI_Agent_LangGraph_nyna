import os, sys, importlib
from pathlib import Path
from starlette.testclient import TestClient
def test_dw(tmp_path, monkeypatch):
    (tmp_path/"d").mkdir(); (tmp_path/"s").mkdir()
    monkeypatch.setenv("DASHBOARD_DIR", str(tmp_path/"d"))
    monkeypatch.setenv("DASHBOARD_STATIC_DIR", str(tmp_path/"s"))
    import app.dashboard_web as dw; importlib.reload(dw)
    name = "a"*48+".html"
    (tmp_path/"d"/name).write_text("<h1>xin chào</h1>", encoding="utf-8")
    (tmp_path/"d"/"evil.html").write_text("x")
    (tmp_path/"s"/"chart.umd.min.js").write_text("js")
    c = TestClient(dw.app)
    r = c.get("/"+name); assert r.status_code==200 and "xin chào" in r.text and r.headers["cache-control"]=="no-store" and "connect-src 'none'" in r.headers["content-security-policy"]
    assert c.head("/"+name).status_code==200
    for p in ["/evil.html","/","/"+"b"*48+".html","/..%2F.env","/a/../.env","/"+"A"*48+".html"]:
        assert c.get(p).status_code==404, p
    assert c.post("/"+name).status_code==404
    assert c.get("/chart.umd.min.js").text=="js"
