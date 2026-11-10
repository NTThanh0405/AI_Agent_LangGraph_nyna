# app/main.py
# Điểm khởi động của dịch vụ. Khi khởi động: cấu hình log, mở pool PostgreSQL chỉ-đọc (dữ liệu
# kinh doanh) và pool ghi cho database agent_app (người dùng, phiên, hội thoại, audit), tạo bảng
# người dùng/phiên/audit, nạp cấu hình agent (config/agents.yaml) và owner đầu tiên nếu bảng còn trống, dựng checkpointer
# PostgresSaver, compile graph một lần và chạy các tác vụ nền (bot Telegram, dọn dashboard hết
# hạn, dọn phiên/audit cũ, gửi báo cáo sáng cho manager). FastAPI chỉ còn phục vụ health check cho Docker HEALTHCHECK.
# Khi tắt thì dừng các tác vụ nền và đóng pool.

import asyncio
import logging
import os
import signal
from contextlib import AsyncExitStack, asynccontextmanager

from aiogram import Bot
from fastapi import FastAPI
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from app.agent.grants import init_grants
from app.agent.graph import build_graph
from app.agent.models import load_agents_config, recursion_limit
from app.agent.retention import purge_loop
from app.agent.tools import ALL_TOOLS
from app.agent.tools.dashboard_tools import cleanup_loop as dashboard_cleanup_loop
from app.api.v1.router import api_router
from app.audit import init_audit
from app.config import get_settings
from app.db.pool import close_pool, get_data_pool, init_pool
from app.reports.scheduler import report_loop
from app.reports.subscriptions import ensure_schema as ensure_report_schema
from app.reports.subscriptions import get_store
from app.telegram.auth import init_users
from app.telegram.bot import run_bot

settings = get_settings()
logging.basicConfig(
    level=settings.log_level.upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def _bot_token() -> str:
    """Token bot Telegram, dùng cho Bot gửi báo cáo sáng (settings.telegram_bot_token là bắt buộc)."""
    return settings.telegram_bot_token.get_secret_value()


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

    # Cấu hình từng agent (config/agents.yaml): sai thì dừng ngay lúc khởi động, không đợi có người hỏi.
    agents_cfg = load_agents_config()
    logger.info(
        "Agent: %s | max_handoffs=%d | recursion_limit=%d",
        ", ".join(f"{n}={a.model}" + ("" if a.enabled else " (tắt)") for n, a in agents_cfg.agents.items()),
        agents_cfg.max_handoffs, recursion_limit(agents_cfg),
    )

    await init_pool()  # pool chỉ-đọc tới database kinh doanh (dùng cho tool SQL)
    try:
        async with AsyncExitStack() as stack:
            # Pool ghi tới agent_app (role agent_checkpoint). Các tham số này do
            # AsyncPostgresSaver yêu cầu; dict_row để auth.py/sessions.py/retention.py đọc row["cột"].
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
            await init_users(cp_pool)  # tạo bảng users + chat_sessions; nạp users_seed.yaml nếu trống
            await init_grants(cp_pool)  # tạo bảng data_grants (quyền xem bảng cấp riêng cho staff)
            await init_audit(cp_pool)  # tạo/bổ sung cột bảng audit_log
            # Bảng report_state + cột chat_sessions.kind (phải chạy SAU init_users vì cần bảng chat_sessions)
            await ensure_report_schema(cp_pool)
            logger.info("Hội thoại và người dùng được lưu trong PostgreSQL (agent_app)")

            app.state.graph = build_graph(ALL_TOOLS, checkpointer)
            app.state.cp_pool = cp_pool

            # Bot riêng chỉ để GỬI báo cáo sáng (bot nhận tin trong run_bot dùng Bot khác, cùng token
            # nên không xung đột). Báo cáo chạy các câu SQL viết sẵn qua pool chỉ-đọc, không qua LLM.
            report_bot = Bot(token=_bot_token())
            stack.push_async_callback(report_bot.session.close)

            bot_task = asyncio.create_task(
                run_bot(app.state.graph, cp_pool), name="telegram-bot"
            )
            bot_task.add_done_callback(_on_bot_done)
            tasks = [
                bot_task,
                asyncio.create_task(dashboard_cleanup_loop()),
                # Dọn phiên hết hạn + checkpoint mồ côi + audit quá 30 ngày, chạy mỗi giờ
                asyncio.create_task(purge_loop(cp_pool, app.state.graph)),
                # Gửi báo cáo sáng cho manager theo config/reports.yaml (kiểm tra mỗi 60 giây)
                asyncio.create_task(
                    report_loop(report_bot, app.state.graph, cp_pool, get_data_pool, get_store()),
                    name="report-loop",
                ),
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