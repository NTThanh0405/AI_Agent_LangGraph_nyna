# tests/test_permissions.py
# Kiểm tra phân quyền theo vai trò (boss / manager) và việc nối với guard.check_select.
# Không kết nối database.

import pytest

from app.agent import permissions
from app.agent.permissions import (
    ROLE_TABLES,
    PermissionDenied,
    can_access,
    ensure_access,
    filter_tables,
    is_known_role,
)
from app.db.guard import check_select


# ---------------------------------------------------------------- vai trò

def test_known_roles():
    assert is_known_role("boss")
    assert is_known_role("manager")


def test_role_is_case_insensitive():
    assert is_known_role("MANAGER")
    assert can_access("Manager", "DWH.FACT_SALES")


@pytest.mark.parametrize("role", [None, "", "hacker", "admin", " boss"])
def test_unknown_role_rejected(role):
    assert not is_known_role(role)
    assert not can_access(role, "dwh.fact_sales")


# ------------------------------------------------------------ boss / manager

def test_boss_can_read_everything_in_dwh():
    assert can_access("boss", "dwh.fact_sales")
    assert can_access("boss", "dwh.dim_customer")
    assert can_access("boss", "dwh.fact_member_vouchers")
    assert can_access("boss", "dwh.bang_moi_tao_sau_nay")


def test_boss_cannot_read_other_schemas():
    assert not can_access("boss", "public.users")
    assert not can_access("boss", "raw_ipos.events")


def test_manager_same_as_boss():
    assert ROLE_TABLES["manager"] == ROLE_TABLES["boss"]


@pytest.mark.parametrize("table", [
    "dwh.fact_sales", "dwh.fact_member_vouchers", "dwh.fact_hub_bills",
    "dwh.fact_bookings", "dwh.dim_voucher_campaign", "dwh.bang_moi_tao_sau_nay",
])
def test_manager_can_read_all_dwh(table):
    assert can_access("manager", table)


def test_manager_cannot_read_other_schemas():
    assert not can_access("manager", "public.users")
    assert not can_access("manager", "dwh_evil.fact_sales")


# ----------------------------------------------------- tên bảng giả mạo / lạ

def test_table_without_schema_denied():
    assert not can_access("boss", "fact_sales")
    assert not can_access("manager", "fact_sales")


def test_similar_schema_name_not_matched_by_wildcard():
    assert not can_access("boss", "dwh_evil.fact_sales")


def test_star_wildcard_allows_everything(monkeypatch):
    monkeypatch.setitem(ROLE_TABLES, "root", {"*"})
    assert can_access("root", "anything.at_all")


# ------------------------------------------------------------ filter_tables

def test_filter_tables_keeps_only_allowed():
    tables = ["dwh.fact_sales", "dwh.fact_member_vouchers", "dwh.dim_store"]
    assert filter_tables("manager", tables) == ["dwh.fact_sales", "dwh.dim_store"]


def test_filter_tables_unknown_role_returns_nothing():
    assert filter_tables("hacker", ["dwh.fact_sales"]) == []


# ------------------------------------------------------------ ensure_access

def test_ensure_access_ok():
    ensure_access("manager", ["dwh.fact_sales", "dwh.dim_store"])


def test_ensure_access_boss_with_no_tables_ok():
    ensure_access("boss", [])


@pytest.mark.parametrize("role", [None, "", "hacker"])
def test_ensure_access_unknown_role_denied_even_without_tables(role):
    with pytest.raises(PermissionDenied):
        ensure_access(role, [])


def test_ensure_access_lists_denied_tables_sorted():
    with pytest.raises(PermissionDenied) as exc:
        ensure_access(
            "manager",
            ["dwh.fact_sales", "dwh.fact_member_vouchers", "dwh.fact_hub_bills"],
        )
    msg = str(exc.value)
    assert "dwh.fact_hub_bills" in msg
    assert "dwh.fact_member_vouchers" in msg
    assert "dwh.fact_sales" not in msg
    assert msg.index("fact_hub_bills") < msg.index("fact_member_vouchers")


# ------------------------------------------- nối guard + permissions (end-to-end)

def test_manager_blocked_from_voucher_table_via_sql():
    checked = check_select("SELECT * FROM fact_member_vouchers", 100)
    with pytest.raises(PermissionDenied):
        ensure_access("manager", checked.tables)


def test_manager_allowed_sales_query_via_sql():
    checked = check_select(
        "SELECT s.tran_ts FROM fact_sales s JOIN dim_store d ON s.store_key = d.store_key",
        100,
    )
    ensure_access("manager", checked.tables)


def test_cte_trick_cannot_bypass_permissions():
    # Đặt tên CTE trùng tên bảng thật để lách kiểm tra quyền.
    checked = check_select(
        "WITH fact_member_vouchers AS (SELECT * FROM fact_member_vouchers) "
        "SELECT * FROM fact_member_vouchers",
        100,
    )
    with pytest.raises(PermissionDenied):
        ensure_access("manager", checked.tables)


def test_unqualified_table_resolved_to_dwh_for_permission_check():
    checked = check_select("SELECT * FROM dim_customer", 100)
    assert checked.tables == frozenset({"dwh.dim_customer"})
    ensure_access("manager", checked.tables)