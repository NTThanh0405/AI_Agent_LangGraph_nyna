# app/agent/grants.py
# Khối dữ liệu (coffee / house / misa..., xem config/scopes.yaml) cấp riêng cho từng nhân viên (role staff),
# lưu trong agent_app, bảng agent_state.scope_grants. Boss/manager cấp/thu hồi bằng /capquyen, /thuhoi
# (app/telegram/handlers.py); permissions.py và api_tools.py đọc từ đây, có hiệu lực ngay, không cần restart.
# Lỗi database khi ĐỌC quyền thì trả về tập rỗng (không có quyền) để luôn an toàn.
#
# Bảng cũ agent_state.data_grants (cấp theo từng bảng) không còn được đọc; có thể xoá tay khi không cần.

import logging
from typing import Optional

from psycopg_pool import AsyncConnectionPool

logger = logging.getLogger(__name__)
_pool: Optional[AsyncConnectionPool] = None

_ACTIVE = "(expires_at IS NULL OR expires_at > now())"


async def init_grants(pool: AsyncConnectionPool) -> None:
    """Tạo bảng nếu chưa có và lưu pool (cp_pool) để các hàm bên dưới dùng."""
    global _pool
    _pool = pool
    async with pool.connection() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_state.scope_grants (
                id              bigserial PRIMARY KEY,
                grantee_user_id text NOT NULL,
                scope           text NOT NULL,
                granted_by      text NOT NULL,
                created_at      timestamptz NOT NULL DEFAULT now(),
                expires_at      timestamptz,
                UNIQUE (grantee_user_id, scope)
            )
            """
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_scope_grants_user "
            "ON agent_state.scope_grants (grantee_user_id)"
        )


async def get_grants(user_id: str) -> set[str]:
    """Các khối còn hiệu lực được cấp cho user_id. Lỗi thì trả về tập rỗng."""
    if _pool is None:
        return set()
    try:
        async with _pool.connection() as conn:
            cur = await conn.execute(
                f"SELECT scope FROM agent_state.scope_grants WHERE grantee_user_id = %s AND {_ACTIVE}",
                (str(user_id),),
            )
            return {r["scope"].lower() for r in await cur.fetchall()}
    except Exception:
        logger.exception("Không đọc được quyền được cấp (user_id=%s)", user_id)
        return set()


async def list_grants(user_id: str) -> list[dict]:
    """[{scope, granted_by, created_at, expires_at}] còn hiệu lực, sắp theo tên khối."""
    async with _pool.connection() as conn:
        cur = await conn.execute(
            f"SELECT scope, granted_by, created_at, expires_at FROM agent_state.scope_grants "
            f"WHERE grantee_user_id = %s AND {_ACTIVE} ORDER BY scope",
            (str(user_id),),
        )
        return await cur.fetchall()


async def add_grant(user_id: str, scope: str, by: str, hours: int | None = None) -> None:
    """Cấp khối (hoặc cập nhật hạn nếu đã có). hours=None là không hết hạn."""
    async with _pool.connection() as conn:
        await conn.execute(
            "INSERT INTO agent_state.scope_grants (grantee_user_id, scope, granted_by, expires_at) "
            "VALUES (%s, %s, %s, now() + %s::int * interval '1 hour') "
            "ON CONFLICT (grantee_user_id, scope) DO UPDATE SET "
            "granted_by = EXCLUDED.granted_by, expires_at = EXCLUDED.expires_at, created_at = now()",
            (str(user_id), scope.lower(), str(by), hours),
        )


async def revoke_grant(user_id: str, scope: str | None = None) -> int:
    """Thu hồi một khối, hoặc tất cả nếu scope=None. Trả về số quyền đã xoá."""
    sql = "DELETE FROM agent_state.scope_grants WHERE grantee_user_id = %s"
    params: list = [str(user_id)]
    if scope:
        sql += " AND scope = %s"
        params.append(scope.lower())
    async with _pool.connection() as conn:
        cur = await conn.execute(sql, params)
        return cur.rowcount


async def purge_expired() -> int:
    """Xoá quyền đã hết hạn. Gọi trong purge_loop của retention.py."""
    async with _pool.connection() as conn:
        cur = await conn.execute(
            "DELETE FROM agent_state.scope_grants WHERE expires_at IS NOT NULL AND expires_at <= now()"
        )
        return cur.rowcount
