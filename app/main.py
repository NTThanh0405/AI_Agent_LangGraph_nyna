# app/main.py
# Điểm khởi động của dịch vụ. Khi khởi động: cấu hình log, mở pool PostgreSQL chỉ-đọc (dữ liệu
# kinh doanh) và pool ghi cho database agent_app (người dùng, hội thoại, audit), tạo bảng
# người dùng và nạp owner đầu tiên nếu bảng còn trống, dựng checkpointer PostgresSaver,
# compile graph một lần và chạy các tác vụ nền (bot Telegram, dọn phiên hết hạn, dọn dashboard
# hết hạn, dọn hội thoại/audit cũ). FastAPI chỉ còn phục vụ health check cho Docker HEALTHCHECK.
# Khi tắt thì dừng các tác vụ nền và đóng pool.

import asyncio
import logging
import os
import signal
from contextlib import AsyncExitStack, asynccontextmanager

from fastapi import FastAPI
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from app.agent.graph import build_graph
from app.agent.tools import ALL_TOOLS
from app.agent.tools.dashboard_tools import cleanup_loop as dashboard_cleanup_loop
from app.api.v1.router import api_router
from app.config import get_settings
from app.db.pool import close_pool, init_pool
from app.telegram.auth import init_users
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
    # Danh sách người dùng nằm trong database agent_app nên CHECKPOINT_DATABASE_URL là bắt buộc.
    if not settings.checkpoint_database_url:
        raise RuntimeError(
            "Thiếu CHECKPOINT_DATABASE_URL: cần database agent_app để lưu người dùng và hội thoại"
        )

    await init_pool()  # pool chỉ-đọc tới database kinh doanh (dùng cho tool SQL)
    try:
        async with AsyncExitStack() as stack:
            # Pool ghi tới agent_app (role agent_checkpoint). Các tham số này do
            # AsyncPostgresSaver yêu cầu; dict_row để auth.py/retention.py đọc row["cột"].
            cp_pool = AsyncConnectionPool(
                conninfo=settings.checkpoint_database_url.get_secret_value(),
                min_size=1,
                max_size=3,
                kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
                open=False,
            )
            await cp_pool.open(wait=True)  # báo lỗi ngay nếu không kết nối được
            stack.push_async_callback(cp_pool.close)

            checkpointer = AsyncPostgresSaver(cp_pool)
            await checkpointer.setup()  # tạo bảng checkpoint nếu chưa có
            await init_users(cp_pool)  # tạo bảng users; nạp users_seed.yaml nếu còn trống
            logger.info("Hội thoại và người dùng được lưu trong PostgreSQL (agent_app)")

            app.state.graph = build_graph(ALL_TOOLS, checkpointer)
            app.state.cp_pool = cp_pool

            bot_task = asyncio.create_task(
                run_bot(app.state.graph, cp_pool), name="telegram-bot"
            )
            bot_task.add_done_callback(_on_bot_done)
            tasks = [
                bot_task,
                asyncio.create_task(sweep_loop(app.state.graph)),
                asyncio.create_task(dashboard_cleanup_loop()),
                asyncio.create_task(purge_loop(cp_pool, checkpointer)),
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