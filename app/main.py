# app/main.py
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.agent.graph import build_graph
from app.api.v1.router import api_router
from app.config import get_settings

settings = get_settings()
logging.basicConfig(
    level=settings.log_level.upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.graph = build_graph()  # compile một lần khi khởi động
    yield


is_prod = settings.app_env == "production"

app = FastAPI(
    title=settings.app_name,
    lifespan=lifespan,
    docs_url=None if is_prod else "/docs",
    redoc_url=None,
    openapi_url=None if is_prod else "/openapi.json",
)
app.include_router(api_router)