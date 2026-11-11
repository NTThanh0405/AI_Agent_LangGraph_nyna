# tests/test_permissions.py
# Kiểm tra phân quyền xem bảng (app/agent/permissions.py) và quyền theo KHỐI dữ liệu cấp riêng cho staff
# (app/agent/grants.py + app/agent/scopes.py, config/scopes.yaml: coffee -> dwh.*, house/misa -> API).
#
# Chạy:  pytest tests/test_permissions.py
#
# Hai nhóm test:
#   1) Không cần database: giả lập grants.get_grants (trả tên khối), chạy nhanh, luôn chạy được.
#   2) Cần PostgreSQL thật (kiểm tra hết hạn, thu hồi, dọn dẹp bằng SQL thật). Chỉ chạy khi đặt
#      biến môi trường TEST_CHECKPOINT_DATABASE_URL trỏ tới database agent_app (hoặc một database
#      thử nghiệm). Test chỉ đụng tới dòng có grantee_user_id bắt đầu bằng "test-" và tự xoá sau khi chạy.
#
# Không cần pytest-asyncio: mỗi test tự gọi asyncio.run.

import asyncio
import os

import pytest

from app.agent import grants, scopes
from app.agent import permissions as perm
from app.agent.permissions import PermissionDenied

TABLE = "dwh.test_orders"
OTHER_TABLE = "dwh.test_customers"
OUTSIDE = "acc.salary"


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def fake_grants(monkeypatch):
    """Giả lập bảng scope_grants trong bộ nhớ: store[user_id] = tập khối được cấp (coffee, house...)."""
    store: dict[str, set[str]] = {}

    async def fake_get_grants(user_id):
        return set(store.get(str(user_id), set()))

    monkeypatch.setattr(grants, "get_grants", fake_get_grants)
    return store


# ================================================================ 1) theo role (không cần DB)

def test_boss_and_manager_see_whole_dwh_schema():
    for role in ("boss", "manager"):
        assert perm.can_access(role, "dwh.fact_sales")
        assert perm.can_access(role, "dwh.dim_store")


def test_boss_and_manager_cannot_see_other_schemas():
    for role in ("boss", "manager"):
        assert not perm.can_access(role, "acc.salary")
        assert not perm.can_access(role, "public.users")


def test_role_and_table_are_case_insensitive():
    assert perm.can_access("MANAGER", "DWH.Fact_Sales")


def test_unknown_or_empty_role_is_denied():
    for role in (None, "", "hacker", "admin"):
        assert not perm.can_access(role, "dwh.fact_sales")
        assert not perm.is_known_role(role)
        with pytest.raises(PermissionDenied):
            perm.ensure_access(role, [])  # kể cả câu SELECT không đụng bảng nào


def test_staff_is_known_role_but_sync_api_blocks_everything():
    # API đồng bộ chỉ theo role: staff luôn bị chặn (an toàn nếu ai đó quên dùng bản _user)
    assert perm.is_known_role("staff")
    assert not perm.can_access("staff", "dwh.fact_sales")
    with pytest.raises(PermissionDenied):
        perm.ensure_access("staff", ["dwh.fact_sales"])


def test_filter_tables_by_role():
    tables = ["dwh.fact_sales", "acc.salary", "dwh.dim_store"]
    assert perm.filter_tables("manager", tables) == ["dwh.fact_sales", "dwh.dim_store"]
    assert perm.filter_tables("staff", tables) == []


# ================================================================ 2) staff + khối được cấp (không cần DB)

def test_staff_without_scope_is_denied(fake_grants):
    assert not run(perm.can_access_user("staff", "u1", TABLE))
    with pytest.raises(PermissionDenied):
        run(perm.ensure_access_user("staff", "u1", [TABLE]))


def test_staff_with_coffee_sees_dwh_but_not_other_schemas(fake_grants):
    fake_grants["u1"] = {"coffee"}
    assert run(perm.can_access_user("staff", "u1", TABLE))
    assert run(perm.can_access_user("staff", "u1", OTHER_TABLE))
    assert not run(perm.can_access_user("staff", "u1", OUTSIDE))
    run(perm.ensure_access_user("staff", "u1", [TABLE, OTHER_TABLE]))  # không raise
    with pytest.raises(PermissionDenied) as e:
        run(perm.ensure_access_user("staff", "u1", [TABLE, OUTSIDE]))
    assert OUTSIDE in str(e.value) and TABLE not in str(e.value)


@pytest.mark.parametrize("api_scopes", [{"house"}, {"misa"}, {"house", "misa"}])
def test_api_scopes_give_no_database_tables(fake_grants, api_scopes, api_scopes_config):
    fake_grants["u1"] = api_scopes
    assert not run(perm.can_access_user("staff", "u1", TABLE))
    assert run(scopes.user_scopes("staff", "u1")) == api_scopes


def test_scope_is_per_user(fake_grants):
    fake_grants["u1"] = {"coffee"}
    assert not run(perm.can_access_user("staff", "u2", TABLE))


def test_staff_without_user_id_is_denied(fake_grants):
    fake_grants[""] = {"coffee"}
    fake_grants["None"] = {"coffee"}
    assert not run(perm.can_access_user("staff", None, TABLE))
    assert not run(perm.can_access_user("staff", "", TABLE))


def test_unknown_scope_names_in_db_are_ignored(fake_grants):
    # dòng lạ (vd tên bảng kiểu cũ, khối đã xoá khỏi cấu hình) không mở thêm quyền
    fake_grants["u1"] = {"dwh.*", TABLE, "kho_cu"}
    assert not run(perm.can_access_user("staff", "u1", TABLE))
    assert run(scopes.user_scopes("staff", "u1")) == set()


def test_role_name_is_case_insensitive(fake_grants):
    fake_grants["u1"] = {"coffee"}
    assert run(perm.can_access_user("STAFF", "u1", TABLE.upper()))


def test_scopes_do_not_widen_boss_or_manager(fake_grants):
    fake_grants["m1"] = {"coffee"}
    assert not run(perm.can_access_user("manager", "m1", OUTSIDE))
    assert run(perm.can_access_user("manager", "m1", "dwh.fact_sales"))


def test_boss_and_manager_have_every_configured_scope(fake_grants):
    for role in ("boss", "manager"):
        assert run(scopes.user_scopes(role, "x")) == {"coffee"}      # cấu hình hiện tại: chỉ coffee


def test_boss_and_manager_get_new_scopes_automatically(fake_grants, api_scopes_config):
    # role_scopes "*" -> khối mới thêm vào cấu hình (house, misa) có ngay cho boss/manager
    for role in ("boss", "manager"):
        assert run(scopes.user_scopes(role, "x")) == {"coffee", "house", "misa"}


def test_scope_not_in_current_config_gives_nothing(fake_grants):
    # được cấp "misa" từ trước nhưng cấu hình hiện tại chưa có misa -> không có tác dụng
    fake_grants["u1"] = {"misa"}
    assert run(scopes.user_scopes("staff", "u1")) == set()


def test_unknown_role_denied_even_if_scopes_exist(fake_grants):
    fake_grants["u1"] = {"coffee"}
    assert not run(perm.can_access_user("hacker", "u1", TABLE))
    assert run(scopes.user_scopes("hacker", "u1")) == set()
    with pytest.raises(PermissionDenied):
        run(perm.ensure_access_user("hacker", "u1", [TABLE]))


def test_select_without_tables_is_ok_for_known_role_only(fake_grants):
    run(perm.ensure_access_user("staff", "u1", []))  # ví dụ SELECT 1
    with pytest.raises(PermissionDenied):
        run(perm.ensure_access_user(None, "u1", []))


def test_filter_tables_user_for_staff(fake_grants):
    fake_grants["u1"] = {"coffee"}
    got = run(perm.filter_tables_user("staff", "u1", [TABLE, OUTSIDE, OTHER_TABLE]))
    assert got == [TABLE, OTHER_TABLE]


def test_scope_with_explicit_table_list(fake_grants, monkeypatch):
    cfg = scopes.parse_scopes_config({
        "scopes": {"kho": {"source": "postgres", "tables": ["inv.stock", "inv.moves"]},
                   "coffee": {"source": "postgres", "tables": ["dwh.*"]}},
        "role_scopes": {"boss": ["*"]},
    })
    monkeypatch.setattr(scopes, "load_scopes_config", lambda: cfg)
    fake_grants["u1"] = {"kho"}
    assert run(perm.can_access_user("staff", "u1", "inv.stock"))
    assert not run(perm.can_access_user("staff", "u1", "inv.prices"))
    assert not run(perm.can_access_user("staff", "u1", TABLE))


# ================================================================ lỗi database -> chặn (fail-closed)

def test_get_grants_returns_empty_when_pool_not_initialized(monkeypatch):
    monkeypatch.setattr(grants, "_pool", None)
    assert run(grants.get_grants("u1")) == set()


def test_database_error_denies_staff(monkeypatch):
    class BrokenPool:
        def connection(self):
            raise RuntimeError("database down")

    monkeypatch.setattr(grants, "_pool", BrokenPool())
    assert run(grants.get_grants("u1")) == set()
    assert not run(perm.can_access_user("staff", "u1", TABLE))
    # boss/manager không phụ thuộc bảng quyền cấp riêng nên không bị ảnh hưởng
    assert run(perm.can_access_user("manager", "m1", "dwh.fact_sales"))


# ================================================================ 3) cần PostgreSQL thật

DB_URL = os.getenv("TEST_CHECKPOINT_DATABASE_URL")
needs_db = pytest.mark.skipif(
    not DB_URL, reason="Đặt TEST_CHECKPOINT_DATABASE_URL để chạy test với PostgreSQL thật"
)


async def _with_pool(body):
    """Mở pool giống app/main.py, init_grants, chạy body(pool), rồi dọn dòng test-*."""
    from psycopg.rows import dict_row
    from psycopg_pool import AsyncConnectionPool

    pool = AsyncConnectionPool(
        conninfo=DB_URL,
        min_size=1,
        max_size=2,
        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
        open=False,
    )
    await pool.open(wait=True)
    try:
        async with pool.connection() as conn:
            await conn.execute("CREATE SCHEMA IF NOT EXISTS agent_state")
        await grants.init_grants(pool)
        await body(pool)
    finally:
        async with pool.connection() as conn:
            await conn.execute("DELETE FROM agent_state.scope_grants WHERE grantee_user_id LIKE 'test-%'")
        await pool.close()


async def _expire(pool, user_id, scope):
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE agent_state.scope_grants SET expires_at = now() - interval '1 hour' "
            "WHERE grantee_user_id = %s AND scope = %s",
            (user_id, scope),
        )


@needs_db
def test_db_grant_without_expiry():
    async def body(pool):
        await grants.add_grant("test-1", "coffee", by="api:test")
        assert await grants.get_grants("test-1") == {"coffee"}
        rows = await grants.list_grants("test-1")
        assert len(rows) == 1 and rows[0]["expires_at"] is None and rows[0]["granted_by"] == "api:test"

    run(_with_pool(body))


@needs_db
def test_db_regrant_updates_expiry():
    async def body(pool):
        await grants.add_grant("test-1", "house", by="api:test", hours=1)
        assert (await grants.list_grants("test-1"))[0]["expires_at"] is not None
        await grants.add_grant("test-1", "house", by="api:test", hours=None)
        rows = await grants.list_grants("test-1")
        assert len(rows) == 1 and rows[0]["expires_at"] is None

    run(_with_pool(body))


@needs_db
def test_db_expired_grant_is_ignored_and_purged():
    async def body(pool):
        await grants.add_grant("test-1", "coffee", by="api:test", hours=1)
        await _expire(pool, "test-1", "coffee")
        assert await grants.get_grants("test-1") == set()
        assert await grants.list_grants("test-1") == []
        assert await grants.purge_expired() >= 1
        assert await grants.purge_expired() == 0

    run(_with_pool(body))


@needs_db
def test_db_revoke_one_and_all():
    async def body(pool):
        for s in ("coffee", "house", "misa"):
            await grants.add_grant("test-1", s, by="api:test")
        assert await grants.revoke_grant("test-1", "house") == 1
        assert await grants.get_grants("test-1") == {"coffee", "misa"}
        assert await grants.revoke_grant("test-1", None) == 2
        assert await grants.revoke_grant("test-1", None) == 0

    run(_with_pool(body))


@needs_db
def test_db_end_to_end_staff_permission_lifecycle():
    async def body(pool):
        assert not await perm.can_access_user("staff", "test-1", TABLE)
        await grants.add_grant("test-1", "house", by="api:test")      # khối API: không mở bảng nào
        assert not await perm.can_access_user("staff", "test-1", TABLE)
        await grants.add_grant("test-1", "coffee", by="api:test", hours=1)
        assert await perm.can_access_user("staff", "test-1", TABLE)
        await _expire(pool, "test-1", "coffee")
        assert not await perm.can_access_user("staff", "test-1", TABLE)
        await grants.add_grant("test-1", "coffee", by="api:test")
        await grants.revoke_grant("test-1", "coffee")
        assert not await perm.can_access_user("staff", "test-1", TABLE)

    run(_with_pool(body))
