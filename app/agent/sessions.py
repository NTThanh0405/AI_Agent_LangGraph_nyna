# app/agent/sessions.py
# Quản lý phiên chat: mỗi người tối đa max_sessions_per_user phiên, sổ cái là bảng
# agent_state.chat_sessions, phiên đang dùng nằm ở agent_state.users.active_thread.
# Xoá phiên = adelete_thread (checkpoints, blobs, writes) rồi xoá dòng sổ cái.
# Các tham số (số phiên tối đa, số ngày hết hạn...) nằm trong app/config.py (biến môi trường).
# Phiên bị xoá khi session_idle_days ngày không dùng hoặc quá session_max_days ngày kể từ lúc tạo.
# Thread báo cáo sáng (kind = 'report', xem app/reports/) nằm chung bảng chat_sessions nhưng KHÔNG tính vào
# hạn mức phiên, không hiện trong /sessions, và hết hạn sau REPORT_RETENTION_DAYS ngày.
# Khoá theo thread giữ trong RAM nên service chỉ được chạy MỘT worker.
# `pool` ở mọi hàm là pool tới database agent_app (row_factory=dict_row, xem app/main.py).

import asyncio
import logging
import uuid
import weakref
from datetime import datetime, timedelta

from app.config import get_settings

logger = logging.getLogger(__name__)

REPORT_RETENTION_DAYS = 3  # thread báo cáo giữ 3 ngày kể từ lúc tạo

_locks: "weakref.WeakValueDictionary[str, asyncio.Lock]" = weakref.WeakValueDictionary()


def get_lock(thread: str) -> asyncio.Lock:
    lock = _locks.get(thread)
    if lock is None:
        lock = asyncio.Lock()
        _locks[thread] = lock
    return lock


def is_busy(thread: str) -> bool:
    lock = _locks.get(thread)
    return lock is not None and lock.locked()


def session_expires_at(row: dict) -> datetime:
    """Thời điểm phiên sẽ bị xoá: sớm hơn của (lần dùng cuối + 7 ngày, lúc tạo + 14 ngày)."""
    s = get_settings()
    return min(
        row["last_active"] + timedelta(days=s.session_idle_days),
        row["created_at"] + timedelta(days=s.session_max_days),
    )


async def drop_session(graph, pool, thread: str) -> None:
    """Xoá checkpoint trước, dòng sổ cái sau: lỗi giữa chừng thì lần dọn sau thử lại."""
    await graph.checkpointer.adelete_thread(thread)
    async with pool.connection() as conn:
        await conn.execute("DELETE FROM agent_state.chat_sessions WHERE thread_id = %s", (thread,))


async def create_session(graph, pool, user_id) -> tuple[str, list[str]]:
    """Tạo phiên mới và đặt làm phiên hiện tại. Vượt hạn mức thì xoá phiên ít dùng nhất.
    Trả về (thread_id mới, danh sách thread đã bị xoá)."""
    uid = str(user_id)
    thread = f"tg:{uid}:{uuid.uuid4().hex[:8]}"
    async with pool.connection() as conn:
        async with conn.transaction():
            # Khoá theo user để hai lệnh /new cùng lúc không làm vượt hạn mức
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s::text))", (uid,))
            cur = await conn.execute(
                "SELECT thread_id FROM agent_state.chat_sessions "
                "WHERE user_id = %s AND kind = 'chat' ORDER BY last_active DESC OFFSET %s",
                (uid, get_settings().max_sessions_per_user - 1),  # chừa chỗ cho phiên mới
            )
            surplus = [r["thread_id"] for r in await cur.fetchall()]
            await conn.execute(
                "INSERT INTO agent_state.chat_sessions (thread_id, user_id) VALUES (%s, %s)",
                (thread, uid),
            )
            await conn.execute(
                "UPDATE agent_state.users SET active_thread = %s WHERE telegram_id = %s",
                (thread, int(uid)),
            )
    evicted = []
    for old in surplus:
        if is_busy(old):
            continue  # đang chạy, purge_expired sẽ dọn sau
        await drop_session(graph, pool, old)
        evicted.append(old)
    return thread, evicted


async def peek_active(pool, user_id) -> str | None:
    """Phiên hiện tại của người dùng (chỉ đọc, không tạo mới)."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT active_thread FROM agent_state.users WHERE telegram_id = %s",
            (int(user_id),),
        )
        row = await cur.fetchone()
    return row["active_thread"] if row else None


async def reuse_empty_active(pool, user_id) -> str | None:
    """Nếu phiên hiện tại còn trống (chưa có câu hỏi nào) thì trả về nó. Dùng cho /new để
    gõ /new nhiều lần không đẩy các phiên có nội dung ra khỏi hạn mức."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT s.thread_id FROM agent_state.users u "
            "JOIN agent_state.chat_sessions s ON s.thread_id = u.active_thread "
            "WHERE u.telegram_id = %s AND s.title IS NULL AND s.kind = 'chat'",
            (int(user_id),),
        )
        row = await cur.fetchone()
    return row["thread_id"] if row else None


def report_thread_id(user_id, day) -> str:
    """Tên thread báo cáo của một người trong một ngày, ví dụ tg:123456789:report:20261106."""
    return f"tg:{user_id}:report:{day:%Y%m%d}"


async def register_report_thread(pool, user_id, thread: str, title: str) -> None:
    """Ghi thread báo cáo vào sổ cái với kind='report' (không đổi phiên đang dùng của người dùng)."""
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO agent_state.chat_sessions (thread_id, user_id, title, kind) "
            "SELECT %s, %s, %s, 'report' "
            "WHERE NOT EXISTS (SELECT 1 FROM agent_state.chat_sessions WHERE thread_id = %s)",
            (thread, str(user_id), title, thread),
        )


async def latest_report_thread(pool, user_id) -> dict | None:
    """Thread báo cáo mới nhất còn hạn của người dùng (dùng cho lệnh /reply), hoặc None."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT thread_id, title, created_at FROM agent_state.chat_sessions "
            "WHERE user_id = %s AND kind = 'report' ORDER BY created_at DESC LIMIT 1",
            (str(user_id),),
        )
        return await cur.fetchone()


async def get_or_create_active(graph, pool, user_id) -> tuple[str, bool]:
    """Trả về (thread hiện tại, expired). expired=True nếu phiên cũ của người này đã
    bị xoá (hết hạn hoặc lỗi) nên phải mở phiên mới."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT u.active_thread, (s.thread_id IS NOT NULL) AS alive "
            "FROM agent_state.users u "
            "LEFT JOIN agent_state.chat_sessions s ON s.thread_id = u.active_thread "
            "WHERE u.telegram_id = %s",
            (int(user_id),),
        )
        row = await cur.fetchone()
    if row and row["active_thread"] and row["alive"]:
        return row["active_thread"], False
    thread, _ = await create_session(graph, pool, user_id)
    return thread, bool(row and row["active_thread"])


async def touch_session(pool, thread: str, message: str) -> None:
    """Gọi đầu mỗi lượt: cập nhật lần dùng cuối, đặt tiêu đề nếu chưa có."""
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE agent_state.chat_sessions "
            "SET last_active = now(), title = COALESCE(title, left(%s::text, %s::int)) "
            "WHERE thread_id = %s",
            (message.replace("\n", " "), get_settings().session_title_max_chars, thread),
        )


async def list_sessions(pool, user_id) -> list[dict]:
    """Các phiên của người dùng, sắp theo lúc tạo (thứ tự ổn định), kèm cờ is_active."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT s.thread_id, s.title, s.created_at, s.last_active, "
            "       (s.thread_id = u.active_thread) AS is_active "
            "FROM agent_state.chat_sessions s "
            "LEFT JOIN agent_state.users u ON u.telegram_id = %s "
            "WHERE s.user_id = %s AND s.kind = 'chat' ORDER BY s.created_at",
            (int(user_id), str(user_id)),
        )
        return await cur.fetchall()


async def switch_session(pool, user_id, thread: str) -> bool:
    """Đặt phiên hiện tại. False nếu phiên không tồn tại hoặc không thuộc người này.
    Cập nhật luôn last_active để phiên vừa chọn không bị coi là "ít dùng nhất"."""
    async with pool.connection() as conn:
        async with conn.transaction():
            cur = await conn.execute(
                "UPDATE agent_state.chat_sessions SET last_active = now() "
                "WHERE thread_id = %s AND user_id = %s",
                (thread, str(user_id)),
            )
            if cur.rowcount != 1:
                return False
            await conn.execute(
                "UPDATE agent_state.users SET active_thread = %s WHERE telegram_id = %s",
                (thread, int(user_id)),
            )
            return True


async def delete_own_session(graph, pool, user_id, thread: str) -> str:
    """Xoá phiên của chính người dùng. Trả về 'ok' | 'busy' | 'not_found'."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT 1 FROM agent_state.chat_sessions WHERE thread_id = %s AND user_id = %s",
            (thread, str(user_id)),
        )
        if not await cur.fetchone():
            return "not_found"
    if is_busy(thread):
        return "busy"
    async with get_lock(thread):
        await drop_session(graph, pool, thread)
    async with pool.connection() as conn:
        # Xoá đúng phiên đang dùng thì bỏ trỏ, lượt sau tự mở phiên mới (không báo "hết hạn")
        await conn.execute(
            "UPDATE agent_state.users SET active_thread = NULL "
            "WHERE telegram_id = %s AND active_thread = %s",
            (int(user_id), thread),
        )
    return "ok"


async def purge_user_sessions(graph, pool, user_id) -> int:
    """Xoá toàn bộ phiên của một người (dùng khi xoá người dùng). Trả về số phiên đã xoá."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT thread_id FROM agent_state.chat_sessions WHERE user_id = %s",
            (str(user_id),),
        )
        threads = [r["thread_id"] for r in await cur.fetchall()]
    for thread in threads:
        async with get_lock(thread):
            await drop_session(graph, pool, thread)
    return len(threads)


async def reset(graph, pool, thread: str) -> None:
    """Xoá hẳn một phiên (dùng khi lượt hỏi lỗi, tránh để lại hội thoại dở dang)."""
    await drop_session(graph, pool, thread)


async def purge_expired(graph, pool) -> int:
    """Xoá phiên hết hạn và checkpoint mồ côi. Trả về số thread đã xoá."""
    s = get_settings()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT thread_id FROM agent_state.chat_sessions "
            "WHERE (kind = 'chat' AND (last_active < now() - make_interval(days => %s) "
            "                          OR created_at  < now() - make_interval(days => %s))) "
            "   OR (kind = 'report' AND created_at < now() - make_interval(days => %s))",
            (s.session_idle_days, s.session_max_days, REPORT_RETENTION_DAYS),
        )
        expired = [r["thread_id"] for r in await cur.fetchall()]
        cur = await conn.execute(
            "SELECT DISTINCT c.thread_id FROM agent_state.checkpoints c "
            "WHERE NOT EXISTS (SELECT 1 FROM agent_state.chat_sessions s "
            "                  WHERE s.thread_id = c.thread_id)"
        )
        orphans = [r["thread_id"] for r in await cur.fetchall()]

    count = 0
    for thread in expired:
        if is_busy(thread):
            continue
        async with get_lock(thread):
            await drop_session(graph, pool, thread)
        count += 1
    for thread in orphans:
        if is_busy(thread):
            continue
        async with get_lock(thread):
            await graph.checkpointer.adelete_thread(thread)
        count += 1
    return count