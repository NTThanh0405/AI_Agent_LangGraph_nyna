# app/main.py
# Điểm khởi động của dịch vụ. Khi khởi động: cấu hình log, nạp allowlist Telegram (sai thì
# báo lỗi ngay), mở connection pool PostgreSQL, chọn nơi lưu hội thoại (PostgresSaver hoặc
# RAM), compile graph một lần và chạy ba tác vụ nền (bot Telegram, dọn phiên hết hạn, dọn
# dashboard hết hạn). FastAPI chỉ còn phục vụ health check cho Docker HEALTHCHECK.
# Khi tắt thì dừng các tác vụ nền và đóng pool.

import asyncio
import logging
import os
import signal
from contextlib import AsyncExitStack, asynccontextmanager

from fastapi import FastAPI

from app.agent.graph import build_graph
from app.agent.sessions import sweep_loop
from app.agent.tools import ALL_TOOLS
from app.agent.tools.dashboard_tools import cleanup_loop as dashboard_cleanup_loop
from app.api.v1.router import api_router
from app.config import get_settings
from app.db.pool import close_pool, init_pool
from app.telegram.auth import load_allowlist
from app.telegram.bot import run_bot

settings = get_settings()
logging.basicConfig(
    level=settings.log_level.upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def _on_bot_done(task: asyncio.Task) -> None:
    """Bot dừng vì lỗi (ví dụ token sai) thì tắt cả dịch vụ để Docker khởi động lại,
    thay vì để container vẫn "healthy" trong khi bot đã chết."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.critical(
            "Bot Telegram dừng do lỗi, tắt dịch vụ để Docker khởi động lại", exc_info=exc
        )
        os.kill(os.getpid(), signal.SIGTERM)


@asynccontextmanager
async def lifespan(app: FastAPI):
    count = load_allowlist()  # file allowlist sai thì dừng ngay, trước khi mở kết nối
    logger.info("Allowlist có %d người dùng", count)

    await init_pool()
    try:
        async with AsyncExitStack() as stack:
            checkpointer = None  # None -> graph dùng MemorySaver (RAM)
            if settings.checkpoint_database_url:
                from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

                checkpointer = await stack.enter_async_context(
                    AsyncPostgresSaver.from_conn_string(
                        settings.checkpoint_database_url.get_secret_value()
                    )
                )
                await checkpointer.setup()
                logger.info("Hội thoại được lưu trong PostgreSQL (PostgresSaver)")
            else:
                logger.warning(
                    "Chưa cấu hình CHECKPOINT_DATABASE_URL: hội thoại lưu trong RAM, "
                    "sẽ mất khi restart"
                )

            app.state.graph = build_graph(ALL_TOOLS, checkpointer)

            bot_task = asyncio.create_task(run_bot(app.state.graph), name="telegram-bot")
            bot_task.add_done_callback(_on_bot_done)
            tasks = [
                bot_task,
                asyncio.create_task(sweep_loop(app.state.graph)),
                asyncio.create_task(dashboard_cleanup_loop()),
            ]
            try:
                yield
            finally:
                for t in tasks:
                    t.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        await close_pool()


is_prod = settings.app_env == "production"

app = FastAPI(
    title=settings.app_name,
    lifespan=lifespan,
    docs_url=None if is_prod else "/docs",
    redoc_url=None,
    openapi_url=None if is_prod else "/openapi.json",
)
app.include_router(api_router)