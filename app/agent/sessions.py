# app/agent/sessions.py
# Quản lý "phiên" hội thoại: nhớ trong phiên, quá SESSION_TTL_SECONDS không hoạt động thì xoá.
# Mỗi thread có một khoá asyncio để hai request cùng thread không chạy chồng lên nhau.
# Mỗi người dùng Telegram có đúng một thread (xem thread_for); hết hạn hoặc /new thì
# xoá checkpoint của thread đó, nên không cần đổi thread_id.
# Chỉ đúng khi chạy MỘT process (Dockerfile dùng 1 worker uvicorn, đừng thêm --workers).

import asyncio
import logging
import time

from app.config import get_settings

logger = logging.getLogger(__name__)

SESSION_TTL_SECONDS = get_settings().session_ttl_minutes * 60  # đổi bằng SESSION_TTL_MINUTES trong .env
SWEEP_INTERVAL_SECONDS = 300

_last_seen: dict[str, float] = {}
_locks: dict[str, asyncio.Lock] = {}


def thread_for(user_id: int | str) -> str:
    """Thread hội thoại của một người dùng Telegram (mỗi người một thread)."""
    return f"tg:{user_id}"


def get_lock(thread: str) -> asyncio.Lock:
    """Khoá của thread; tạo mới nếu chưa có."""
    lock = _locks.get(thread)
    if lock is None:
        lock = _locks[thread] = asyncio.Lock()
    return lock


def is_busy(thread: str) -> bool:
    """Thread đang có request chạy không (để bot báo "đang xử lý câu trước" thay vì xếp hàng im lặng)."""
    lock = _locks.get(thread)
    return lock is not None and lock.locked()


def _forget(thread: str) -> None:
    """Bỏ ghi nhận thread; giữ lại khoá nếu đang có request chạy."""
    _last_seen.pop(thread, None)
    lock = _locks.get(thread)
    if lock is not None and not lock.locked():
        _locks.pop(thread, None)


async def touch(graph, thread: str) -> bool:
    """Gọi đầu mỗi request (bên trong khoá): phiên cũ đã quá hạn thì xoá trước khi chạy tiếp.

    Trả về True nếu vừa xoá một phiên hết hạn, để bot báo cho người dùng biết
    cuộc trò chuyện mới đã bắt đầu.
    """
    now = time.monotonic()
    last = _last_seen.get(thread)
    cleared = False
    if last is not None and now - last > SESSION_TTL_SECONDS:
        try:
            await graph.checkpointer.adelete_thread(thread)
            cleared = True
            logger.info("Phiên %s đã hết hạn, bắt đầu phiên mới", thread)
        except Exception:
            # Không để lỗi dọn dẹp làm hỏng câu hỏi đang chờ trả lời.
            logger.exception("Không xoá được phiên hết hạn %s", thread)
    _last_seen[thread] = now
    return cleared


async def reset(graph, thread: str) -> None:
    """Kết thúc phiên ngay (người dùng gõ /new, hoặc lượt chạy bị lỗi).
    Handler nên gọi bên trong `async with get_lock(thread)` để không xoá giữa lúc đang chạy."""
    await graph.checkpointer.adelete_thread(thread)
    _forget(thread)


async def _sweep_once(graph) -> None:
    now = time.monotonic()
    expired = [
        t for t, ts in list(_last_seen.items())
        if now - ts > SESSION_TTL_SECONDS and not is_busy(t)
    ]
    for t in expired:
        try:
            await graph.checkpointer.adelete_thread(t)
        except Exception:
            logger.exception("Không xoá được phiên %s", t)
        _forget(t)
    for t, lock in list(_locks.items()):
        if t not in _last_seen and not lock.locked():
            _locks.pop(t, None)
    if expired:
        logger.info("Đã dọn %d phiên hết hạn", len(expired))


async def sweep_loop(graph) -> None:
    """Chạy nền: xoá phiên hết hạn (kể cả khi người đó không nhắn lại) và khoá mồ côi.
    Lỗi một vòng chỉ được ghi log, vòng dọn không bị chết."""
    while True:
        await asyncio.sleep(SWEEP_INTERVAL_SECONDS)
        try:
            await _sweep_once(graph)
        except Exception:
            logger.exception("Vòng dọn phiên gặp lỗi")