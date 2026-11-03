# app/agent/retention.py
# Tự dọn dữ liệu cũ trong agent_app:
#   - hội thoại không hoạt động quá CHAT_RETENTION_DAYS ngày (dựa vào thời điểm ghi
#     checkpoint cuối cùng, nên vẫn đúng sau khi restart)
#   - nhật ký audit_log quá AUDIT_RETENTION_DAYS ngày
# sessions.py vẫn lo phiên hết hạn trong lúc chạy; file này lo phần còn sót trong database.

import asyncio
import logging

from langgraph.checkpoint.base import BaseCheckpointSaver
from psycopg_pool import AsyncConnectionPool

logger = logging.getLogger(__name__)

CHAT_RETENTION_DAYS = 3
AUDIT_RETENTION_DAYS = 90
PURGE_INTERVAL_SECONDS = 3600  # quét mỗi giờ


async def purge_chats(pool: AsyncConnectionPool, checkpointer: BaseCheckpointSaver) -> int:
    """Xóa thread có checkpoint mới nhất cũ hơn CHAT_RETENTION_DAYS ngày."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            """
            SELECT thread_id
            FROM agent_state.checkpoints
            GROUP BY thread_id
            HAVING max((checkpoint->>'ts')::timestamptz)
                   < now() - make_interval(days => %s)
            """,
            (CHAT_RETENTION_DAYS,),
        )
        expired = [r["thread_id"] for r in await cur.fetchall()]

    for thread_id in expired:
        await checkpointer.adelete_thread(thread_id)  # xóa checkpoint, blobs, writes
    return len(expired)


async def purge_audit(pool: AsyncConnectionPool) -> int:
    """Xóa nhật ký quá AUDIT_RETENTION_DAYS ngày."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            "DELETE FROM agent_state.audit_log "
            "WHERE created_at < now() - make_interval(days => %s)",
            (AUDIT_RETENTION_DAYS,),
        )
        return cur.rowcount


async def purge_loop(pool: AsyncConnectionPool, checkpointer: BaseCheckpointSaver) -> None:
    """Vòng nền: định kỳ dọn dữ liệu cũ. Lỗi một việc không làm chết vòng."""
    while True:
        try:
            n = await purge_chats(pool, checkpointer)
            if n:
                logger.info("Đã xóa %d hội thoại quá %d ngày", n, CHAT_RETENTION_DAYS)
        except Exception:
            logger.exception("Dọn hội thoại cũ thất bại")

        try:
            n = await purge_audit(pool)
            if n:
                logger.info("Đã xóa %d dòng audit quá %d ngày", n, AUDIT_RETENTION_DAYS)
        except Exception:
            logger.exception("Dọn audit_log thất bại")

        await asyncio.sleep(PURGE_INTERVAL_SECONDS)