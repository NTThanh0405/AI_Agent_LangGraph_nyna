# app/audit.py
# Nhật ký truy vết trong agent_state.audit_log: ai hỏi gì, ở phiên nào, kết quả ra sao, và các
# thay đổi quyền (auth.log_change ghi status='perm_change'). init_audit tạo/bổ sung bảng khi
# khởi động (app/main.py); log_turn ghi mỗi lượt hỏi và KHÔNG BAO GIỜ raise, để lỗi ghi nhật ký
# không làm hỏng câu trả lời. retention.py tự xoá dòng cũ hơn AUDIT_RETENTION_DAYS ngày.
# Cột khớp với sql/create_list_user.sql; ALTER bên dưới bổ sung cho bảng tạo từ bản cũ.

import logging

from psycopg_pool import AsyncConnectionPool

logger = logging.getLogger(__name__)

_MAX_QUESTION_CHARS = 2000

# Mỗi phần tử là MỘT câu lệnh: pool dùng prepare_threshold=0 nên không chạy được nhiều lệnh
# trong một lần execute.
_CREATE_SQL = (
    """
    CREATE TABLE IF NOT EXISTS agent_state.audit_log (
        id            bigserial PRIMARY KEY,
        created_at    timestamptz NOT NULL DEFAULT now(),
        user_id       text,
        user_role     text,
        thread_id     text,
        question      text,
        sql_executed  text[],
        row_count     int,
        status        text,
        input_tokens  int,
        output_tokens int,
        duration_ms   int
    )
    """,
    "ALTER TABLE agent_state.audit_log ADD COLUMN IF NOT EXISTS user_role text",
    "ALTER TABLE agent_state.audit_log ADD COLUMN IF NOT EXISTS thread_id text",
    "CREATE INDEX IF NOT EXISTS idx_audit_created ON agent_state.audit_log (created_at)",
    "CREATE INDEX IF NOT EXISTS idx_audit_user ON agent_state.audit_log (user_id, created_at)",
)


async def init_audit(pool: AsyncConnectionPool) -> None:
    """Tạo bảng audit_log nếu chưa có và bổ sung cột thiếu. Gọi một lần khi khởi động."""
    async with pool.connection() as conn:
        for stmt in _CREATE_SQL:
            await conn.execute(stmt)


async def log_turn(
    pool: AsyncConnectionPool,
    *,
    user_id: str,
    role: str | None,
    question: str,
    status: str,
    duration_ms: int | None = None,
    thread_id: str | None = None,
) -> None:
    """Ghi một lượt hỏi. status: 'ok', 'error', 'busy'."""
    try:
        async with pool.connection() as conn:
            await conn.execute(
                "INSERT INTO agent_state.audit_log "
                "(user_id, user_role, thread_id, question, status, duration_ms) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (user_id, role, thread_id, question[:_MAX_QUESTION_CHARS], status, duration_ms),
            )
    except Exception:
        logger.exception("Không ghi được audit_log (user_id=%s)", user_id)