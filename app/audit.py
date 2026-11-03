# app/audit.py
# Nhật ký truy vết trong agent_state.audit_log: ai hỏi gì, kết quả ra sao, và các thay đổi quyền
# (auth.log_change ghi status='perm_change'). init_audit tạo bảng khi khởi động (app/main.py);
# log_turn ghi mỗi lượt hỏi và KHÔNG BAO GIỜ raise, để lỗi ghi nhật ký không làm hỏng câu trả lời.
# retention.py tự xoá dòng cũ hơn AUDIT_RETENTION_DAYS ngày.

import logging

from psycopg_pool import AsyncConnectionPool

logger = logging.getLogger(__name__)

_MAX_QUESTION_CHARS = 2000

# Các cột user_id, question, status khớp với câu INSERT trong app/telegram/auth.py (log_change).
_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS agent_state.audit_log (
    id          bigserial PRIMARY KEY,
    created_at  timestamptz NOT NULL DEFAULT now(),
    user_id     text,
    role        text,
    question    text,
    status      text NOT NULL,
    duration_ms integer
);
CREATE INDEX IF NOT EXISTS audit_log_created_at_idx
    ON agent_state.audit_log (created_at);
"""


async def init_audit(pool: AsyncConnectionPool) -> None:
    """Tạo bảng audit_log nếu chưa có. Gọi một lần khi khởi động, sau init_users."""
    async with pool.connection() as conn:
        await conn.execute(_CREATE_SQL)


async def log_turn(
    pool: AsyncConnectionPool,
    *,
    user_id: str,
    role: str | None,
    question: str,
    status: str,
    duration_ms: int | None = None,
) -> None:
    """Ghi một lượt hỏi. status ví dụ: 'ok', 'error', 'busy'."""
    try:
        async with pool.connection() as conn:
            await conn.execute(
                "INSERT INTO agent_state.audit_log "
                "(user_id, role, question, status, duration_ms) VALUES (%s, %s, %s, %s, %s)",
                (user_id, role, question[:_MAX_QUESTION_CHARS], status, duration_ms),
            )
    except Exception:
        logger.exception("Không ghi được audit_log (user_id=%s)", user_id)