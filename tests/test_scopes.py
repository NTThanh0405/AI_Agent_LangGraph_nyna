# tests/test_scopes.py
# Kiểm tra config/scopes.yaml (app/agent/scopes.py): file thật hợp lệ, cấu hình sai bị báo lỗi ngay,
# khối theo role, và dòng "Khối dữ liệu được xem" trong system prompt. Không cần database.

import asyncio
import copy

import pytest

from app.agent import grants, scopes
from app.agent.scopes import ScopeConfigError, parse_scopes_config
from tests.conftest import API_SCOPES_DATA


def run(coro):
    return asyncio.run(coro)


def test_real_config_has_only_coffee_today():
    scopes.load_scopes_config.cache_clear()
    cfg = scopes.load_scopes_config()
    assert cfg.names() == ["coffee"]
    assert cfg.db_scopes() == ["coffee"] and cfg.api_scopes() == []
    assert cfg.tables_for({"coffee"}) == {"dwh.*"}
    assert cfg.for_role("boss") == cfg.for_role("manager") == {"coffee"}
    assert cfg.for_role("staff") == set() and cfg.for_role("hacker") == set()


def test_future_config_parses():
    cfg = parse_scopes_config(API_SCOPES_DATA)
    assert cfg.api_scopes() == ["house", "misa"]
    assert cfg.tables_for({"house", "misa"}) == set()
    house = cfg.connectors["house"]
    assert house.auth_type == "header" and house.auth_name == "Authorization" and house.max_days == 31
    assert cfg.connectors["misa"].endpoints["revenue_summary"].method == "POST"


def _broken(mutate):
    data = copy.deepcopy(API_SCOPES_DATA)
    mutate(data)
    return data


@pytest.mark.parametrize("mutate,msg", [
    (lambda d: d.pop("scopes"), "thiếu mục 'scopes'"),
    (lambda d: d["scopes"].update({"Coffee 2": {"source": "postgres", "tables": ["dwh.*"]}}), "tên khối"),
    (lambda d: d["scopes"].update({"all": {"source": "postgres", "tables": ["dwh.*"]}}), "khác 'all'"),
    (lambda d: d["scopes"]["coffee"].update({"source": "excel"}), "source phải là"),
    (lambda d: d["scopes"]["coffee"].update({"tables": []}), "tables"),
    (lambda d: d["scopes"]["coffee"].update({"tables": ["dwh.fact_sales; drop"]}), "tables"),
    (lambda d: d["scopes"]["house"].update({"connector": "khong_co"}), "không có trong 'connectors'"),
    (lambda d: d["role_scopes"].update({"staff": ["kho"]}), "khối không tồn tại"),
    (lambda d: d["connectors"]["house"]["endpoints"]["revenue_by_day"].update({"path": "https://evil.com/x"}), "path"),
    (lambda d: d["connectors"]["house"]["endpoints"]["revenue_by_day"].update({"path": "/a/../admin"}), "path"),
    (lambda d: d["connectors"]["house"]["endpoints"]["revenue_by_day"].update({"method": "DELETE"}), "GET hoặc POST"),
    (lambda d: d["connectors"]["house"]["endpoints"]["revenue_by_day"]["params"].update({"x": "{token}"}), "biến lạ"),
    (lambda d: d["connectors"]["house"].update({"endpoints": {}}), "chưa khai báo endpoint"),
    (lambda d: d["connectors"]["house"]["auth"].update({"type": "cookie"}), "auth.type"),
    (lambda d: d["connectors"]["house"]["auth"].pop("value_env"), "value_env"),
    (lambda d: d["connectors"]["house"].update({"timeout_seconds": 0}), "timeout_seconds"),
])
def test_bad_config_is_rejected(mutate, msg):
    with pytest.raises(ScopeConfigError, match=msg.replace("(", r"\(").replace(")", r"\)")):
        parse_scopes_config(_broken(mutate))


def test_staff_scopes_combine_role_and_grants(monkeypatch, api_scopes_config):
    async def fake(uid):
        return {"misa", "khong_co"} if uid == "7" else set()
    monkeypatch.setattr(grants, "get_grants", fake)
    assert run(scopes.user_scopes("staff", "7")) == {"misa"}      # khối lạ bị bỏ
    assert run(scopes.user_scopes("staff", "8")) == set()
    assert run(scopes.user_scopes("staff", None)) == set()
    assert run(scopes.user_scopes("boss", "7")) == {"coffee", "house", "misa"}


def test_describe_scopes():
    assert scopes.describe_scopes({"coffee"}) == "coffee (Nyna Coffee (iPOS))"
    assert scopes.describe_scopes(set()) == "(chưa có)"
    assert scopes.describe_scopes({"khong_co"}) == "(chưa có)"


def test_system_prompt_lists_scopes_from_config():
    from app.agent.nodes import _common as C

    cfg = {"configurable": {"user_role": "staff", "user_scopes": ["coffee"]}}
    text = C.extract_text(C.system_message("coder", cfg).content)
    assert "Khối dữ liệu được xem: coffee (Nyna Coffee (iPOS))." in text
    text = C.extract_text(C.system_message("coder", {"configurable": {"user_role": "staff", "user_scopes": []}}).content)
    assert "Khối dữ liệu được xem: (chưa có)." in text
    # không truyền user_scopes (vd test cũ) thì không thêm dòng này
    assert "Khối dữ liệu" not in C.extract_text(C.system_message("coder", {"configurable": {}}).content).split("\n")[-1]
