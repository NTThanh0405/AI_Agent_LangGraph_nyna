# app/agent/sessions.py
# Quản lý phiên chat của từng người dùng (mỗi người một thread_id cố định) và cửa sổ lưu trữ:
# CHỈ GIỮ các lượt hỏi trong SESSION_TTL_MINUTES gần nhất (mặc định 3 ngày), lượt cũ hơn bị
# xoá hẳn khỏi database dù người dùng vẫn đang chat liên tục.
#   - thread_for : đổi user id thành thread_id dùng cho LangGraph
#   - get_lock / is_busy : mỗi thread chỉ chạy một lượt tại một thời điểm
#   - touch : gọi đầu mỗi lượt; cắt các lượt quá hạn, trả True nếu cả hội thoại đã hết hạn
#   - trim_thread : cắt lượt quá hạn của một thread (retention.py gọi mỗi giờ)
#   - reset : xoá toàn bộ hội thoại của thread (/new, lượt hỏi bị lỗi)
#   - sweep_loop : vòng nền xoá phiên im lặng quá hạn đang nằm trong RAM
# Mỗi lượt hỏi được đóng dấu thời gian ở HumanMessage (additional_kwargs["ts"], xem runner.py);
# AI/tool message thuộc về lượt của HumanMessage đứng trước nó. Lượt không có dấu thời gian
# (dữ liệu cũ trước khi có cơ chế này) bị coi là quá hạn.
# Khoá và mốc hoạt động giữ trong RAM nên service chỉ được chạy MỘT worker (xem Dockerfile).

import asyncio
import logging
import time
import weakref

from langchain_core.messages import HumanMessage

from app.config import get_settings

logger = logging.getLogger(__name__)

SWEEP_INTERVAL_SECONDS = 600  # quét phiên hết hạn trong RAM mỗi 10 phút

# Khoá theo thread; khoá không còn ai giữ thì tự bị thu hồi.
_locks: "weakref.WeakValueDictionary[str, asyncio.Lock]" = weakref.WeakValueDictionary()

# Lần hoạt động gần nhất của mỗi thread (time.monotonic), chỉ cho sweep_loop.
_last_active: dict[str, float] = {}


def thread_for(user_id) -> str:
    """thread_id của một người dùng. Nhận cả số lẫn chuỗi và luôn ra cùng một kết quả."""
    return f"tg:{user_id}"


def get_lock(thread: str) -> asyncio.Lock:
    lock = _locks.get(thread)
    if lock is None:
        lock = asyncio.Lock()
        _locks[thread] = lock
    return lock


def is_busy(thread: str) -> bool:
    lock = _locks.get(thread)
    return lock is not None and lock.locked()


def _config(thread: str) -> dict:
    return {"configurable": {"thread_id": thread}}


def _ttl_seconds() -> float:
    return get_settings().session_ttl_minutes * 60


def _recent_messages(messages: list, cutoff_ts: float) -> list:
    """Giữ nguyên thứ tự, chỉ giữ các lượt hỏi có dấu thời gian >= cutoff_ts."""
    kept: list = []
    keep_turn = False
    for m in messages:
        if isinstance(m, HumanMessage):
            ts = (m.additional_kwargs or {}).get("ts")
            keep_turn = isinstance(ts, (int, float)) and ts >= cutoff_ts
        if keep_turn:
            kept.append(m)
    return kept


async def reset(graph, thread: str) -> None:
    """Xoá toàn bộ hội thoại (checkpoint, blobs, writes) của thread."""
    await graph.checkpointer.adelete_thread(thread)
    _last_active.pop(thread, None)


async def _trim(graph, thread: str, messages: list) -> tuple[bool, int]:
    """Xoá lượt quá hạn. Trả về (có thay đổi không, số tin còn lại).

    Xoá sạch mọi checkpoint cũ của thread rồi ghi lại MỘT checkpoint mới chỉ chứa phần còn lại,
    vì checkpoint cũ vẫn mang nguyên lịch sử (nếu chỉ xoá tin trong state thì tin cũ còn nằm
    trong database).
    """
    kept = _recent_messages(messages, time.time() - _ttl_seconds())
    if len(kept) == len(messages):
        return False, len(kept)
    await graph.checkpointer.adelete_thread(thread)
    if kept:
        await graph.aupdate_state(_config(thread), {"messages": kept}, as_node="chat")
    logger.info("Đã xoá %d tin quá hạn (thread=%s)", len(messages) - len(kept), thread)
    return True, len(kept)


async def trim_thread(graph, thread: str) -> bool:
    """Cắt lượt quá hạn của một thread. Người gọi phải giữ khoá của thread."""
    snapshot = await graph.aget_state(_config(thread))
    messages = (snapshot.values or {}).get("messages") or [] if snapshot else []
    if not messages:
        return False
    changed, _ = await _trim(graph, thread, messages)
    return changed


async def touch(graph, thread: str) -> bool:
    """Gọi đầu mỗi lượt (người gọi đã giữ khoá): cắt lượt quá hạn. Trả về True nếu toàn bộ
    hội thoại cũ đã quá hạn và bị xoá (lượt này bắt đầu một hội thoại mới)."""
    expired = False
    snapshot = await graph.aget_state(_config(thread))
    messages = (snapshot.values or {}).get("messages") or [] if snapshot else []
    if messages:
        changed, remaining = await _trim(graph, thread, messages)
        expired = changed and remaining == 0
    _last_active[thread] = time.monotonic()
    return expired


async def sweep_loop(graph) -> None:
    """Vòng nền: xoá các phiên im lặng quá hạn đang nằm trong RAM."""
    while True:
        await asyncio.sleep(SWEEP_INTERVAL_SECONDS)
        cutoff = time.monotonic() - _ttl_seconds()
        for thread, last in list(_last_active.items()):
            if last > cutoff or is_busy(thread):
                continue
            try:
                async with get_lock(thread):
                    if _last_active.get(thread, 0.0) > cutoff:
                        continue  # có lượt mới chen vào trong lúc chờ khoá
                    await reset(graph, thread)
                    logger.info("Đã xoá phiên hết hạn (thread=%s)", thread)
            except Exception:
                logger.exception("Không xoá được phiên hết hạn (thread=%s)", thread)