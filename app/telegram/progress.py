# app/telegram/progress.py
# Báo tiến độ trong lúc agent phân tích (mất 20-60 giây): gửi tin "Đang phân tích...",
# giữ trạng thái "đang gõ..." cho đến khi xong, rồi xoá tin tiến độ để cuộc trò chuyện gọn.
# Lỗi khi gửi/xoá tin tiến độ chỉ bị bỏ qua, không làm hỏng câu trả lời.

import logging
from contextlib import asynccontextmanager

from aiogram.exceptions import TelegramAPIError
from aiogram.types import Message
from aiogram.utils.chat_action import ChatActionSender

logger = logging.getLogger(__name__)

DEFAULT_TEXT = "⏳ Đang phân tích, khoảng 1 phút..."


@asynccontextmanager
async def progress(message: Message, text: str = DEFAULT_TEXT):
    """Dùng: `async with progress(message): ...chạy agent...`"""
    status = None
    try:
        status = await message.answer(text)
    except TelegramAPIError:
        logger.warning("Không gửi được tin tiến độ", exc_info=True)

    try:
        async with ChatActionSender.typing(
            bot=message.bot,
            chat_id=message.chat.id,
            message_thread_id=message.message_thread_id,
        ):
            yield
    finally:
        if status is not None:
            try:
                await status.delete()
            except TelegramAPIError:
                logger.warning("Không xoá được tin tiến độ", exc_info=True)