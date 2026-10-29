# app/main.py
# Điểm khởi động của dịch vụ FastAPI. Khi khởi động: cấu hình log, mở connection pool
# PostgreSQL, chọn nơi lưu hội thoại (PostgresSaver hoặc RAM), compile graph một lần và
# chạy hai tác vụ nền (dọn phiên hết hạn, dọn dashboard hết hạn). Khi tắt thì dừng các
# tác vụ nền và đóng pool.

import asyncio
import logging
from contextlib import AsyncExitStack, asynccontextmanager

from fastapi import FastAPI

from app.agent.graph import build_graph
from app.agent.sessions import sweep_loop
from app.agent.tools import ALL_TOOLS
from app.agent.tools.dashboard_tools import cleanup_loop as dashboard_cleanup_loop
from app.api.v1.router import api_router
from app.config import get_settings
from app.db.pool import close_pool, init_pool

settings = get_settings()
logging.basicConfig(
    level=settings.log_level.upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
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
            tasks = [
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