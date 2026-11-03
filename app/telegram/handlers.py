# app/telegram/handlers.py
# Các handler của bot Telegram:
#   /start /help /new /tuan /thang /dashboard /myid, tin nhắn thường, phản hồi tin lạ,
#   và lệnh quản trị người dùng (/adduser /addadmin /removeadmin /removeuser /users).
# Mọi tin nhắn đi qua AllowlistMiddleware trước: chỉ chat riêng, chỉ người có trong bảng
# agent_state.users (PostgreSQL), bỏ qua tin quá cũ. Người lạ bị im lặng bỏ qua (chỉ ghi log),
# trừ lệnh /myid để họ lấy được Telegram ID gửi cho quản trị viên.
# Lệnh quản trị là code cố định, không đi qua LLM.

import logging
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import Message

from app.agent.permissions import ROLE_TABLES
from app.agent.runner import MSG_BUSY, run_turn
from app.agent.sessions import get_lock, is_busy, reset, thread_for
from app.telegram.auth import (
    ADMIN,
    OWNER,
    STAFF,
    AllowedUser,
    check_manage,
    demote_admin,
    get_allowed_user,
    list_users,
    log_change,
    remove_user,
    upsert_user,
    valid_role,
)
from app.telegram.formatter import send_text
from app.telegram.progress import progress
from app.telegram.ratelimit import get_limiter

logger = logging.getLogger(__name__)

# Tin nhắn gửi lúc bot đang tắt: quá cũ thì bỏ qua thay vì trả lời muộn hàng giờ.
STALE_AFTER_SECONDS = 600

HELP_TEXT = (
    "Mình là trợ lý phân tích dữ liệu kinh doanh. Bạn cứ hỏi tự nhiên, ví dụ:\n"
    "• Doanh thu tuần này thế nào?\n"
    "• So sánh doanh thu tháng này với tháng trước\n"
    "• Sản phẩm nào đang kéo doanh thu xuống?\n"
    "• Cho tôi dashboard doanh thu tuần này\n\n"
    "Lệnh nhanh:\n"
    "/tuan - tình hình kinh doanh tuần này\n"
    "/thang - tình hình kinh doanh tháng này\n"
    "/dashboard - dashboard doanh thu tuần này\n"
    "/new - bắt đầu cuộc trò chuyện mới\n"
    "/help - xem hướng dẫn\n\n"
    "Lưu ý: mình chỉ nhớ ngữ cảnh cuộc trò chuyện trong 3 ngày."
)

ADMIN_HELP_TEXT = (
    "\n\nLệnh quản trị:\n"
    "/users - danh sách người dùng\n"
    "/adduser <id> <role> [tên] - thêm nhân viên hoặc đổi role\n"
    "/addadmin <id> <role> [tên] - thêm hoặc nâng lên admin\n"
    "/removeadmin <id> - gỡ quyền admin\n"
    "/removeuser <id> - xóa người dùng"
)

PROMPT_WEEK = (
    "Tình hình kinh doanh tuần này thế nào? Cho tôi các số liệu chính, so với tuần trước, "
    "và đề xuất."
)
PROMPT_MONTH = (
    "Tình hình kinh doanh tháng này thế nào? Cho tôi các số liệu chính, so với tháng trước, "
    "và đề xuất."
)
PROMPT_DASHBOARD = "Tạo dashboard doanh thu tuần này."

_TAG = {OWNER: " (chủ)", ADMIN: " (admin)", STAFF: ""}


def _is_myid(event: Message) -> bool:
    text = (event.text or "").strip().lower()
    return text == "/myid" or text.startswith("/myid ") or text.startswith("/myid@")


class AllowlistMiddleware(BaseMiddleware):
    """Chỉ cho qua tin nhắn chat riêng, còn mới, của người có trong bảng users.

    Ngoại lệ: /myid cho mọi người (kể cả người lạ) để lấy Telegram ID.
    """

    async def __call__(
        self,
        handler: Callable[[Message, dict[str, Any]], Awaitable[Any]],
        event: Message,
        data: dict[str, Any],
    ) -> Any:
        if event.chat.type != ChatType.PRIVATE or event.from_user is None:
            return None  # bỏ qua group/channel

        age = (datetime.now(timezone.utc) - event.date).total_seconds()
        if age > STALE_AFTER_SECONDS:
            logger.info("Bỏ qua tin nhắn cũ %.0fs (telegram_id=%s)", age, event.from_user.id)
            return None

        if _is_myid(event):
            data["user"] = None
            return await handler(event, data)

        pool = data["pool"]  # dp["pool"] = cp_pool, đặt ở app/main.py
        try:
            user = await get_allowed_user(pool, event.from_user.id)
        except Exception:
            logger.exception("Không tra được người dùng trong database")
            return None

        if user is None:
            logger.info("Bỏ qua người dùng ngoài danh sách (telegram_id=%s)", event.from_user.id)
            return None

        data["user"] = user
        return await handler(event, data)


# ---------------------------------------------------------------- hỏi đáp với agent

async def _ask(message: Message, user: AllowedUser, graph, text: str) -> None:
    """Gửi một câu hỏi cho agent và trả lời người dùng."""
    uid = str(user.telegram_id)

    if is_busy(thread_for(uid)):
        await message.answer(MSG_BUSY)
        return

    blocked = get_limiter().hit(uid)
    if blocked:
        await message.answer(blocked)
        return

    async with progress(message):
        result = await run_turn(graph, user_id=uid, role=user.role, message=text)

    if result.session_reset:
        await message.answer("Cuộc trò chuyện trước đã hết hạn nên mình bắt đầu cuộc mới.")
    await send_text(message, result.answer)


async def cmd_myid(message: Message) -> None:
    await message.answer(f"Telegram ID của bạn: {message.from_user.id}")


async def cmd_start(message: Message, user: AllowedUser) -> None:
    text = f"Xin chào {user.name or ''}!\n\n{HELP_TEXT}".replace("Xin chào !", "Xin chào!")
    if user.is_admin:
        text += ADMIN_HELP_TEXT
    await message.answer(text)


async def cmd_help(message: Message, user: AllowedUser) -> None:
    text = HELP_TEXT + (ADMIN_HELP_TEXT if user.is_admin else "")
    await message.answer(text)


async def cmd_new(message: Message, user: AllowedUser, graph) -> None:
    thread = thread_for(user.telegram_id)
    if is_busy(thread):
        await message.answer("Mình đang xử lý câu trước, đợi xong rồi gõ /new nhé.")
        return
    try:
        async with get_lock(thread):
            await reset(graph, thread)
    except Exception:
        logger.exception("Không xoá được hội thoại (thread=%s)", thread)
        await message.answer("Chưa xoá được hội thoại, bạn thử lại sau nhé.")
        return
    await message.answer("Đã bắt đầu cuộc trò chuyện mới. Bạn muốn xem gì?")


async def cmd_week(message: Message, user: AllowedUser, graph) -> None:
    await _ask(message, user, graph, PROMPT_WEEK)


async def cmd_month(message: Message, user: AllowedUser, graph) -> None:
    await _ask(message, user, graph, PROMPT_MONTH)


async def cmd_dashboard(message: Message, user: AllowedUser, graph) -> None:
    await _ask(message, user, graph, PROMPT_DASHBOARD)


# ---------------------------------------------------------------- lệnh quản trị
# Chỉ admin (cấp 1) và owner (cấp 2) dùng được. Người thường gõ thì im lặng.

async def _add(message: Message, command: CommandObject, user: AllowedUser, pool,
               level: int | None, usage: str) -> None:
    if not user.is_admin:
        return
    parts = (command.args or "").split(maxsplit=2)
    if len(parts) < 2 or not parts[0].isdigit():
        await message.answer(usage)
        return
    tid, role = int(parts[0]), parts[1].lower()
    name = parts[2] if len(parts) > 2 else None
    if not valid_role(role):
        await message.answer("Role không hợp lệ. Các role: " + ", ".join(sorted(ROLE_TABLES)))
        return
    err = await check_manage(pool, user, tid, role, level)
    if err:
        await message.answer(err)
        return
    await upsert_user(pool, tid, role, name, by=user.telegram_id, level=level)
    await log_change(pool, user.telegram_id, f"set {tid} role={role} level={level}")
    await message.answer(f"Đã lưu {tid}: role {role}" + (", cấp admin." if level == ADMIN else "."))


async def cmd_adduser(message: Message, command: CommandObject, user: AllowedUser, pool) -> None:
    await _add(message, command, user, pool, None, "Cú pháp: /adduser <telegram_id> <role> [tên]")


async def cmd_addadmin(message: Message, command: CommandObject, user: AllowedUser, pool) -> None:
    await _add(message, command, user, pool, ADMIN, "Cú pháp: /addadmin <telegram_id> <role> [tên]")


async def cmd_removeadmin(message: Message, command: CommandObject, user: AllowedUser, pool) -> None:
    if not user.is_admin:
        return
    arg = (command.args or "").strip()
    if not arg.isdigit():
        await message.answer("Cú pháp: /removeadmin <telegram_id>")
        return
    tid = int(arg)
    err = await check_manage(pool, user, tid, None, None)
    if err:
        await message.answer(err)
        return
    ok = await demote_admin(pool, tid)
    if ok:
        await log_change(pool, user.telegram_id, f"demote {tid}")
    await message.answer("Đã gỡ quyền admin." if ok else "Người này không phải admin.")


async def cmd_removeuser(message: Message, command: CommandObject, user: AllowedUser, pool) -> None:
    if not user.is_admin:
        return
    arg = (command.args or "").strip()
    if not arg.isdigit():
        await message.answer("Cú pháp: /removeuser <telegram_id>")
        return
    tid = int(arg)
    err = await check_manage(pool, user, tid, None, None)
    if err:
        await message.answer(err)
        return
    ok = await remove_user(pool, tid)
    if ok:
        await log_change(pool, user.telegram_id, f"remove {tid}")
    await message.answer("Đã xóa." if ok else "Không có người dùng này.")


async def cmd_users(message: Message, user: AllowedUser, pool) -> None:
    if not user.is_admin:
        return
    rows = await list_users(pool)
    lines = [
        f"{r['telegram_id']} | {r['role']}{_TAG.get(r['admin_level'], '')} | {r['name'] or ''}"
        for r in rows
    ]
    await message.answer("\n".join(lines) or "Chưa có người dùng nào.")


# ---------------------------------------------------------------- tin còn lại

async def on_unknown_command(message: Message, user: AllowedUser) -> None:
    await message.answer("Mình chưa có lệnh này. Gõ /help để xem hướng dẫn.")


async def on_text(message: Message, user: AllowedUser, graph) -> None:
    await _ask(message, user, graph, message.text)


async def on_other(message: Message, user: AllowedUser) -> None:
    await message.answer("Mình chỉ đọc tin nhắn dạng chữ thôi nhé.")


def build_router() -> Router:
    """Tạo router mới (thứ tự đăng ký quan trọng: lệnh trước, tin thường sau)."""
    router = Router(name="telegram-main")
    router.message.outer_middleware(AllowlistMiddleware())
    router.message.register(cmd_myid, Command("myid"))
    router.message.register(cmd_start, CommandStart())
    router.message.register(cmd_help, Command("help"))
    router.message.register(cmd_new, Command("new"))
    router.message.register(cmd_week, Command("tuan"))
    router.message.register(cmd_month, Command("thang"))
    router.message.register(cmd_dashboard, Command("dashboard"))
    # Lệnh quản trị (người thường gõ thì không có phản hồi)
    router.message.register(cmd_adduser, Command("adduser"))
    router.message.register(cmd_addadmin, Command("addadmin"))
    router.message.register(cmd_removeadmin, Command("removeadmin"))
    router.message.register(cmd_removeuser, Command("removeuser"))
    router.message.register(cmd_users, Command("users"))
    router.message.register(on_unknown_command, F.text.startswith("/"))
    router.message.register(on_text, F.text)
    router.message.register(on_other)
    return router