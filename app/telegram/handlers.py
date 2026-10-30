# app/telegram/handlers.py
# Các handler của bot Telegram:
#   /start /help /new /tuan /thang /dashboard, tin nhắn thường, và phản hồi cho tin lạ.
# Mọi tin nhắn đi qua AllowlistMiddleware trước: chỉ chat riêng, chỉ người trong allowlist
# (config/allowlist.yaml), bỏ qua tin quá cũ. Người lạ bị im lặng bỏ qua (chỉ ghi log).
# Các lệnh /tuan /thang /dashboard chỉ là câu hỏi viết sẵn gửi cho agent.

import logging
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command, CommandStart
from aiogram.types import Message

from app.agent.runner import MSG_BUSY, run_turn
from app.agent.sessions import get_lock, is_busy, reset, thread_for
from app.telegram.auth import AllowedUser, get_user
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
    "/help - xem hướng dẫn"
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


class AllowlistMiddleware(BaseMiddleware):
    """Chỉ cho qua tin nhắn chat riêng, còn mới, của người trong allowlist."""

    async def __call__(
        self,
        handler: Callable[[Message, dict[str, Any]], Awaitable[Any]],
        event: Message,
        data: dict[str, Any],
    ) -> Any:
        if event.chat.type != ChatType.PRIVATE or event.from_user is None:
            return None  # bỏ qua group/channel

        user = get_user(event.from_user.id)
        if user is None:
            logger.info("Bỏ qua người dùng ngoài allowlist (telegram_id=%s)", event.from_user.id)
            return None

        age = (datetime.now(timezone.utc) - event.date).total_seconds()
        if age > STALE_AFTER_SECONDS:
            logger.info("Bỏ qua tin nhắn cũ %.0fs (telegram_id=%s)", age, user.telegram_id)
            return None

        data["user"] = user
        return await handler(event, data)


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


async def cmd_start(message: Message, user: AllowedUser) -> None:
    await message.answer(f"Xin chào {user.name}!\n\n{HELP_TEXT}")


async def cmd_help(message: Message, user: AllowedUser) -> None:
    await message.answer(HELP_TEXT)


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
    router.message.register(cmd_start, CommandStart())
    router.message.register(cmd_help, Command("help"))
    router.message.register(cmd_new, Command("new"))
    router.message.register(cmd_week, Command("tuan"))
    router.message.register(cmd_month, Command("thang"))
    router.message.register(cmd_dashboard, Command("dashboard"))
    router.message.register(on_unknown_command, F.text.startswith("/"))
    router.message.register(on_text, F.text)
    router.message.register(on_other)
    return router