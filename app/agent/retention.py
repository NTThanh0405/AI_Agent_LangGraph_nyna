# app/agent/retention.py
# Dọn dữ liệu cũ mỗi giờ: phiên hết hạn (+ checkpoint mồ côi), quyền xem bảng đã hết hạn
# của staff (scope_grants) và audit_log quá hạn.
# Số ngày giữ nhật ký lấy từ app/config.py (audit_retention_days).

import asyncio
import logging

from app.agent import grants  # grants.purge_expired khác sessions.purge_expired nên gọi qua module
from app.agent.sessions import purge_expired
from app.config import get_settings

logger = logging.getLogger(__name__)

PURGE_INTERVAL_SECONDS = 3600


async def purge_audit(pool) -> int:
    async with pool.connection() as conn:
        cur = await conn.execute(
            "DELETE FROM agent_state.audit_log "
            "WHERE created_at < now() - make_interval(days => %s)",
            (get_settings().audit_retention_days,),
        )
        return cur.rowcount


async def purge_loop(pool, graph) -> None:
    while True:
        try:
            n = await purge_expired(graph, pool)
            if n:
                logger.info("Đã xoá %d hội thoại hết hạn/mồ côi", n)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Dọn hội thoại cũ thất bại")
        try:
            n = await purge_audit(pool)
            if n:
                logger.info("Đã xoá %d dòng audit quá hạn", n)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Dọn audit_log thất bại")
        # Quyền hết hạn đã không còn tác dụng (get_grants lọc theo expires_at); bước này chỉ dọn bảng
        try:
            n = await grants.purge_expired()
            if n:
                logger.info("Đã xoá %d quyền khối dữ liệu hết hạn", n)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Dọn quyền hết hạn thất bại")
        await asyncio.sleep(PURGE_INTERVAL_SECONDS)