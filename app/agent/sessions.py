# app/agent/sessions.py
# Quản lý "phiên" hội thoại: nhớ trong phiên, quá SESSION_TTL_SECONDS không hoạt động thì xoá.
# Mỗi thread có một khoá asyncio để hai request cùng thread không chạy chồng lên nhau.
# Chỉ đúng khi chạy MỘT process (Dockerfile dùng 1 worker uvicorn, đừng thêm --workers).

import asyncio
import logging
import time

logger = logging.getLogger(__name__)

SESSION_TTL_SECONDS = 60 * 60
SWEEP_INTERVAL_SECONDS = 300

_last_seen: dict[str, float] = {}
_locks: dict[str, asyncio.Lock] = {}


def get_lock(thread: str) -> asyncio.Lock:
    """Khoá của thread; tạo mới nếu chưa có."""
    lock = _locks.get(thread)
    if lock is None:
        lock = _locks[thread] = asyncio.Lock()
    return lock


def _forget(thread: str) -> None:
    """Bỏ ghi nhận thread; giữ lại khoá nếu đang có request chạy."""
    _last_seen.pop(thread, None)
    lock = _locks.get(thread)
    if lock is not None and not lock.locked():
        _locks.pop(thread, None)


def _busy(thread: str) -> bool:
    lock = _locks.get(thread)
    return lock is not None and lock.locked()


async def touch(graph, thread: str) -> None:
    """Gọi đầu mỗi request (bên trong khoá): phiên cũ đã quá hạn thì xoá trước khi chạy tiếp."""
    now = time.monotonic()
    last = _last_seen.get(thread)
    if last is not None and now - last > SESSION_TTL_SECONDS:
        await graph.checkpointer.adelete_thread(thread)
        logger.info("Phiên %s đã hết hạn, bắt đầu phiên mới", thread)
    _last_seen[thread] = now


async def reset(graph, thread: str) -> None:
    """Kết thúc phiên ngay (người dùng gõ /new, hoặc lượt chạy bị lỗi)."""
    await graph.checkpointer.adelete_thread(thread)
    _forget(thread)


async def sweep_loop(graph) -> None:
    """Chạy nền: xoá phiên hết hạn (kể cả khi người đó không nhắn lại) và khoá mồ côi."""
    while True:
        await asyncio.sleep(SWEEP_INTERVAL_SECONDS)
        now = time.monotonic()
        expired = [
            t for t, ts in list(_last_seen.items())
            if now - ts > SESSION_TTL_SECONDS and not _busy(t)
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