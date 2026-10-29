# app/main.py
# Điểm khởi động của dịch vụ FastAPI.
# Việc chính: cấu hình log, khi khởi động thì mở connection pool PostgreSQL +
# nơi lưu hội thoại (checkpointer) rồi compile graph LangGraph một lần,
# khi tắt thì đóng các kết nối lại.

import logging
from contextlib import AsyncExitStack, asynccontextmanager

from fastapi import FastAPI

from app.agent.graph import build_graph
from app.agent.tools import ALL_TOOLS  # các tool truy vấn DB của agent
from app.api.v1.router import api_router
from app.config import get_settings
from app.db.pool import close_pool, init_pool  # connection pool tới PostgreSQL
import asyncio 
from app.agent.sessions import sweep_loop

settings = get_settings()
logging.basicConfig(
    level=settings.log_level.upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Chạy khi app khởi động (trước yield) và khi app tắt (sau yield)."""
    # 1) Mở pool kết nối tới PostgreSQL (tài khoản chỉ-đọc dùng cho tool SQL).
    await init_pool()

    # 2) Chọn nơi lưu hội thoại theo thread_id.
    #    AsyncExitStack giúp tự đóng kết nối checkpointer khi app tắt.
    async with AsyncExitStack() as stack:
        checkpointer = None  # None -> graph dùng MemorySaver (RAM)
        if settings.checkpoint_database_url:
            # Import tại chỗ: chỉ cần thư viện này khi bật lưu hội thoại vào Postgres.
            from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

            checkpointer = await stack.enter_async_context(
                AsyncPostgresSaver.from_conn_string(
                    settings.checkpoint_database_url.get_secret_value()
                )
            )
            await checkpointer.setup()  # tạo bảng checkpoint nếu chưa có
            logger.info("Hội thoại được lưu trong PostgreSQL (PostgresSaver)")
        else:
            logger.warning(
                "Chưa cấu hình CHECKPOINT_DATABASE_URL: hội thoại lưu trong RAM, "
                "sẽ mất khi restart"
            )

        # 3) Compile graph một lần duy nhất và dùng chung cho mọi request.
        app.state.graph = build_graph(ALL_TOOLS, checkpointer)
        app.state.graph = build_graph(ALL_TOOLS, checkpointer)
        sweeper = asyncio.create_task(sweep_loop(app.state.graph))

        yield  # ---- app chạy ở đây ----

        sweeper.cancel()

        yield  # ---- app chạy ở đây ----

    # 4) App tắt: đóng pool kết nối DB.
    await close_pool()


# Production thì ẩn trang tài liệu API (/docs, /openapi.json) cho an toàn.
is_prod = settings.app_env == "production"

app = FastAPI(
    title=settings.app_name,
    lifespan=lifespan,
    docs_url=None if is_prod else "/docs",
    redoc_url=None,
    openapi_url=None if is_prod else "/openapi.json",
)
app.include_router(api_router)