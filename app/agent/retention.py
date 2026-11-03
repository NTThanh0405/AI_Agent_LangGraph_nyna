# app/agent/retention.py
import asyncio
import logging

from psycopg_pool import AsyncConnectionPool

logger = logging.getLogger(__name__)

AUDIT_RETENTION_DAYS = 90
PURGE_INTERVAL_SECONDS = 3600


async def purge_audit(pool: AsyncConnectionPool) -> int:
    async with pool.connection() as conn:
        cur = await conn.execute(
            "DELETE FROM agent_state.audit_log "
            "WHERE created_at < now() - make_interval(days => %s)",
            (AUDIT_RETENTION_DAYS,),
        )
        return cur.rowcount


async def purge_loop(pool: AsyncConnectionPool) -> None:
    while True:
        try:
            n = await purge_audit(pool)
            if n:
                logger.info("Đã xóa %d dòng audit quá %d ngày", n, AUDIT_RETENTION_DAYS)
        except Exception:
            logger.exception("Dọn audit_log thất bại")
        await asyncio.sleep(PURGE_INTERVAL_SECONDS)