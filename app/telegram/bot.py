# app/telegram/bot.py
# Khởi động bot Telegram bằng long polling: agent chủ động hỏi Telegram có tin mới không,
# nên không cần mở port hay đặt webhook công khai. app/main.py chạy run_bot như một tác vụ
# nền trong lifespan. Telegram tự đánh dấu update đã nhận (offset) nên không bị xử lý hai lần.

import logging

from aiogram import Bot, Dispatcher
from aiogram.types import BotCommand

from app.config import get_settings
from app.telegram.handlers import build_router

logger = logging.getLogger(__name__)

COMMANDS = [
    BotCommand(command="tuan", description="Tình hình kinh doanh tuần này"),
    BotCommand(command="thang", description="Tình hình kinh doanh tháng này"),
    BotCommand(command="dashboard", description="Dashboard doanh thu tuần này"),
    BotCommand(command="new", description="Bắt đầu cuộc trò chuyện mới"),
    BotCommand(command="help", description="Xem hướng dẫn"),
]


def build_dispatcher(graph) -> Dispatcher:
    """Dispatcher có sẵn `graph` để handler nhận qua tham số cùng tên."""
    dp = Dispatcher(graph=graph)
    dp.include_router(build_router())
    return dp


async def run_bot(graph) -> None:
    """Chạy polling cho đến khi bị cancel. Lỗi không phục hồi được (ví dụ token sai)
    sẽ raise ra ngoài để main.py tắt dịch vụ cho Docker khởi động lại."""
    bot = Bot(token=get_settings().telegram_bot_token.get_secret_value())
    dp = build_dispatcher(graph)
    try:
        me = await bot.get_me()  # token sai thì lỗi ngay tại đây
        # n8n từng đặt webhook cho bot này; còn webhook thì polling bị Telegram từ chối (409).
        await bot.delete_webhook(drop_pending_updates=False)
        await bot.set_my_commands(COMMANDS)
        logger.info("Bot Telegram sẵn sàng: @%s", me.username)
        await dp.start_polling(
            bot,
            handle_signals=False,  # uvicorn lo tín hiệu tắt, aiogram không được chen vào
            allowed_updates=dp.resolve_used_update_types(),
        )
    finally:
        await bot.session.close()