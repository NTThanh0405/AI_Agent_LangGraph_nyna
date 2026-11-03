# app/agent/retention.py
# Tự dọn dữ liệu cũ trong database agent_app, chạy nền mỗi giờ (app/main.py):
#   - hội thoại: với MỌI thread, xoá các lượt hỏi quá SESSION_TTL_MINUTES (3 ngày) khỏi
#     database, kể cả thread đang được dùng (logic cắt nằm ở sessions.trim_thread)
#   - audit_log: xoá dòng quá AUDIT_RETENTION_DAYS ngày
# purge_loop(pool, graph): pool phải dùng row_factory=dict_row (đã cấu hình ở app/main.py).

import asyncio
import logging

from psycopg_pool import AsyncConnectionPool

from app.agent.sessions import get_lock, is_busy, trim_thread

logger = logging.getLogger(__name__)

AUDIT_RETENTION_DAYS = 90
PURGE_INTERVAL_SECONDS = 3600  # quét mỗi giờ


async def purge_chats(pool: AsyncConnectionPool, graph) -> int:
    """Cắt lượt quá hạn của mọi thread. Trả về số thread đã bị thay đổi."""
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT DISTINCT thread_id FROM agent_state.checkpoints")
        threads = [r["thread_id"] for r in await cur.fetchall()]

    changed = 0
    for thread in threads:
        if is_busy(thread):
            continue  # đang có lượt chạy, lần quét sau sẽ cắt (touch cũng cắt đầu mỗi lượt)
        try:
            async with get_lock(thread):
                if await trim_thread(graph, thread):
                    changed += 1
        except Exception:
            logger.exception("Không cắt được hội thoại %s", thread)
    return changed


async def purge_audit(pool: AsyncConnectionPool) -> int:
    """Xoá nhật ký quá AUDIT_RETENTION_DAYS ngày. Trả về số dòng đã xoá."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            "DELETE FROM agent_state.audit_log "
            "WHERE created_at < now() - make_interval(days => %s)",
            (AUDIT_RETENTION_DAYS,),
        )
        return cur.rowcount


async def purge_loop(pool: AsyncConnectionPool, graph) -> None:
    """Vòng nền: định kỳ dọn dữ liệu cũ. Lỗi một việc không làm chết vòng."""
    while True:
        try:
            n = await purge_chats(pool, graph)
            if n:
                logger.info("Đã cắt tin quá hạn ở %d hội thoại", n)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Dọn hội thoại cũ thất bại")

        try:
            n = await purge_audit(pool)
            if n:
                logger.info("Đã xoá %d dòng audit quá %d ngày", n, AUDIT_RETENTION_DAYS)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Dọn audit_log thất bại")

        await asyncio.sleep(PURGE_INTERVAL_SECONDS)