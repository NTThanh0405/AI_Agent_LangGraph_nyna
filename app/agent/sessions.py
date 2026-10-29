# app/agent/sessions.py
# Quản lý "phiên" hội thoại: nhớ trong phiên, quá SESSION_TTL_SECONDS không hoạt động thì xoá.
# Chỉ đúng khi chạy MỘT process (Dockerfile hiện tại dùng 1 worker uvicorn, đừng thêm --workers).

import asyncio
import logging
import time

logger = logging.getLogger(__name__)

SESSION_TTL_SECONDS = 60 * 60   # im lặng 30 phút thì hết phiên
SWEEP_INTERVAL_SECONDS = 300    # 5 phút quét dọn một lần

_last_seen: dict[str, float] = {}  # thread nội bộ -> lần hoạt động gần nhất


async def touch(graph, thread: str) -> None:
    """Gọi đầu mỗi request: nếu phiên cũ đã quá hạn thì xoá trước khi chạy tiếp."""
    now = time.monotonic()
    last = _last_seen.get(thread)
    if last is not None and now - last > SESSION_TTL_SECONDS:
        await graph.checkpointer.adelete_thread(thread)
        logger.info("Phiên %s đã hết hạn, bắt đầu phiên mới", thread)
    _last_seen[thread] = now


async def reset(graph, thread: str) -> None:
    """Kết thúc phiên ngay (người dùng gõ /new)."""
    await graph.checkpointer.adelete_thread(thread)
    _last_seen.pop(thread, None)


async def sweep_loop(graph) -> None:
    """Chạy nền: xoá các phiên đã hết hạn để giải phóng RAM,
    kể cả khi người đó không nhắn lại nữa."""
    while True:
        await asyncio.sleep(SWEEP_INTERVAL_SECONDS)
        now = time.monotonic()
        expired = [t for t, ts in _last_seen.items() if now - ts > SESSION_TTL_SECONDS]
        for t in expired:
            try:
                await graph.checkpointer.adelete_thread(t)
            except Exception:
                logger.exception("Không xoá được phiên %s", t)
            _last_seen.pop(t, None)
        if expired:
            logger.info("Đã dọn %d phiên hết hạn", len(expired))