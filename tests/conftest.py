# tests/conftest.py
# Cấu hình dùng chung cho mọi test. Không kết nối DB, không đọc .env.

from types import SimpleNamespace

import pytest


@pytest.fixture(autouse=True)
def fake_settings(monkeypatch):
    """guard.check_select cần db_schema; thay get_settings bằng giá trị giả."""
    monkeypatch.setattr(
        "app.db.guard.get_settings",
        lambda: SimpleNamespace(db_schema="dwh"),
    )

# Cấu hình khối "tương lai" (coffee + house + misa qua API) để test phần quyền/tool API, vì
# config/scopes.yaml thật hiện chỉ bật coffee. Dùng: def test_x(api_scopes_config): ...
API_SCOPES_DATA = {
    "scopes": {
        "coffee": {"title": "Nyna Coffee (iPOS)", "source": "postgres", "tables": ["dwh.*"]},
        "house": {"title": "Nyna House", "source": "api", "connector": "house"},
        "misa": {"title": "Kế toán MISA", "source": "api", "connector": "misa"},
    },
    "role_scopes": {"boss": ["*"], "manager": ["*"], "staff": []},
    "connectors": {
        "house": {
            "base_url_env": "TEST_HOUSE_URL",
            "auth": {"type": "header", "header": "Authorization", "value_env": "TEST_HOUSE_TOKEN",
                     "prefix": "Bearer "},
            "max_days": 31,
            "endpoints": {
                "revenue_by_day": {"description": "Doanh thu House theo ngày", "path": "/rev",
                                   "params": {"from": "{start}", "to": "{end}"}, "result_path": "data"},
            },
        },
        "misa": {
            "base_url_env": "TEST_MISA_URL",
            "auth": {"type": "query", "param": "token", "value_env": "TEST_MISA_TOKEN"},
            "endpoints": {
                "revenue_summary": {"description": "Doanh thu kế toán", "method": "POST", "path": "/sum",
                                    "params": {"from_date": "{start_dmy}", "to_date": "{end_dmy}"}},
            },
        },
    },
}


@pytest.fixture
def api_scopes_config(monkeypatch):
    """Thay cấu hình khối bằng bản có house/misa (API) ở mọi nơi đã import load_scopes_config."""
    import app.agent.graph as graph_mod
    from app.agent import scopes

    cfg = scopes.parse_scopes_config(API_SCOPES_DATA)
    monkeypatch.setattr(scopes, "load_scopes_config", lambda *a, **k: cfg)
    monkeypatch.setattr(graph_mod, "load_scopes_config", lambda *a, **k: cfg)
    try:
        from app.telegram import handlers
        monkeypatch.setattr(handlers, "load_scopes_config", lambda *a, **k: cfg)
    except Exception:  # handlers cần Settings; test không dùng handlers thì bỏ qua
        pass
    return cfg
