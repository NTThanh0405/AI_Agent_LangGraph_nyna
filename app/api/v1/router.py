# app/api/v1/router.py
# Gom các route của API v1 dưới tiền tố /api/v1. Chỉ còn health check (không cần khoá):
# bot Telegram gọi graph trực tiếp qua app/agent/runner.py nên không còn endpoint /agent.

from fastapi import APIRouter

from app.api.v1 import health

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(health.router)