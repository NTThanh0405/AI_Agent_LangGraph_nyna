# app/telegram/auth.py
# Người dùng Telegram trong PostgreSQL: tra role, phân cấp quản trị, thêm/xóa người.

import logging
from dataclasses import dataclass
from pathlib import Path

import yaml
from psycopg_pool import AsyncConnectionPool

from app.agent.permissions import ROLE_TABLES

logger = logging.getLogger(__name__)

STAFF, ADMIN, OWNER = 0, 1, 2
MAX_ADMINS = 3  # số admin (cấp 1) tối đa, không tính owner
_LEVELS = {"staff": STAFF, "admin": ADMIN, "owner": OWNER}

_CREATE_SQL = (
    """
    CREATE TABLE IF NOT EXISTS agent_state.users (
        telegram_id bigint PRIMARY KEY,
        name        text,
        role        text NOT NULL,
        admin_level smallint NOT NULL DEFAULT 0 CHECK (admin_level IN (0, 1, 2)),
        created_at  timestamptz NOT NULL DEFAULT now(),
        created_by  bigint
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS one_owner
        ON agent_state.users (admin_level) WHERE admin_level = 2
    """,
)


@dataclass(frozen=True)
class AllowedUser:
    telegram_id: int
    role: str
    admin_level: int
    name: str | None

    @property
    def is_admin(self) -> bool:
        return self.admin_level >= ADMIN


def valid_role(role: str) -> bool:
    return role.lower() in ROLE_TABLES


async def init_users(pool: AsyncConnectionPool, seed_file: str = "config/users_seed.yaml") -> None:
    """Tạo bảng; nếu còn trống thì nạp từ yaml (mỗi người có level: owner/admin/staff)."""
    async with pool.connection() as conn:
        for stmt in _CREATE_SQL:
            await conn.execute(stmt)
        cur = await conn.execute("SELECT count(*) AS n FROM agent_state.users")
        if (await cur.fetchone())["n"] > 0:
            return
        path = Path(seed_file)
        if not path.exists():
            logger.warning("Bảng users trống và không có %s: chưa ai dùng được bot", seed_file)
            return
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for u in data.get("users", []):
            level = _LEVELS.get(str(u.get("level", "staff")).lower(), STAFF)
            await conn.execute(
                "INSERT INTO agent_state.users (telegram_id, name, role, admin_level) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
                (u["telegram_id"], u.get("name"), str(u["role"]).lower(), level),
            )
        logger.info("Đã nạp người dùng ban đầu từ %s", seed_file)


async def get_user(pool, telegram_id: int) -> dict | None:
    """Trả về {role, admin_level, name} hoặc None nếu người lạ. Tra DB mỗi lần nên có hiệu lực ngay."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT role, admin_level, name FROM agent_state.users WHERE telegram_id = %s",
            (telegram_id,),
        )
        return await cur.fetchone()


async def get_allowed_user(pool, telegram_id: int) -> AllowedUser | None:
    """Tra database; trả về AllowedUser hoặc None nếu là người lạ."""
    row = await get_user(pool, telegram_id)
    if row is None:
        return None
    return AllowedUser(telegram_id, row["role"], row["admin_level"], row["name"])


def _role_allowed(actor_role: str, target_role: str) -> bool:
    """Không cho cấp role có quyền xem rộng hơn role của chính người cấp (chống leo thang)."""
    actor = ROLE_TABLES.get(actor_role.lower(), set())
    target = ROLE_TABLES.get(target_role.lower(), set())
    return "*" in actor or target <= actor


async def _admin_count(pool) -> int:
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT count(*) AS n FROM agent_state.users WHERE admin_level = %s", (ADMIN,)
        )
        return (await cur.fetchone())["n"]


async def check_manage(pool, actor: AllowedUser, target_id: int,
                       new_role: str | None, new_level: int | None) -> str | None:
    """Kiểm tra actor có được tác động lên target không. Trả về chuỗi lỗi, hoặc None nếu được phép."""
    if not actor.is_admin:
        return "Bạn không có quyền quản lý người dùng."
    if target_id == actor.telegram_id:
        return "Không tự sửa hoặc tự xóa chính mình qua bot."
    target = await get_user(pool, target_id)
    if target and target["admin_level"] >= OWNER:
        return "Không thể tác động lên chủ chatbot."
    if new_role and not _role_allowed(actor.role, new_role):
        return "Bạn không được cấp role có quyền xem rộng hơn quyền của bạn."
    if new_level == ADMIN and not (target and target["admin_level"] == ADMIN):
        if await _admin_count(pool) >= MAX_ADMINS:
            return f"Đã đủ {MAX_ADMINS} admin. Hãy gỡ bớt trước khi thêm."
    return None


async def upsert_user(pool, telegram_id: int, role: str, name: str | None,
                      by: int, level: int | None = None) -> None:
    """Thêm mới hoặc đổi role/tên. level=None thì giữ nguyên cấp quản trị hiện có."""
    async with pool.connection() as conn:
        await conn.execute(
            """
            INSERT INTO agent_state.users (telegram_id, name, role, admin_level, created_by)
            VALUES (%s, %s, %s, COALESCE(%s, 0), %s)
            ON CONFLICT (telegram_id) DO UPDATE SET
                role = EXCLUDED.role,
                name = COALESCE(EXCLUDED.name, agent_state.users.name),
                admin_level = COALESCE(%s, agent_state.users.admin_level)
            """,
            (telegram_id, name, role.lower(), level, by, level),
        )


async def demote_admin(pool, telegram_id: int) -> bool:
    """Gỡ quyền admin, người đó vẫn là nhân viên. Không đụng tới owner."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            "UPDATE agent_state.users SET admin_level = 0 WHERE telegram_id = %s AND admin_level = %s",
            (telegram_id, ADMIN),
        )
        return cur.rowcount > 0


async def remove_user(pool, telegram_id: int) -> bool:
    """Xóa hẳn người dùng (nhân viên hoặc admin). Owner thì không xóa được."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            "DELETE FROM agent_state.users WHERE telegram_id = %s AND admin_level < %s",
            (telegram_id, OWNER),
        )
        return cur.rowcount > 0


async def list_users(pool) -> list[dict]:
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT telegram_id, name, role, admin_level FROM agent_state.users "
            "ORDER BY admin_level DESC, created_at"
        )
        return await cur.fetchall()


async def log_change(pool, actor_id: int, text: str) -> None:
    """Ghi thay đổi quyền vào audit_log (không cần bảng riêng)."""
    logger.info("Thay đổi quyền bởi %s: %s", actor_id, text)
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO agent_state.audit_log (user_id, question, status) "
            "VALUES (%s, %s, 'perm_change')",
            (str(actor_id), text),
        )