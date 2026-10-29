# app/api/v1/health.py
# Các endpoint kiểm tra sức khoẻ dịch vụ (không cần x-api-key):
#   GET /api/v1/health     -> app còn sống không (dùng cho Docker HEALTHCHECK)
#   GET /api/v1/health/db  -> app có kết nối được tới PostgreSQL không (chờ tối đa 3 giây)
# /health/db chỉ trả ok/lỗi chung chung, không lộ chi tiết lỗi hay thông tin máy chủ DB.

import logging

from fastapi import APIRouter, HTTPException

from app.db.pool import get_pool

logger = logging.getLogger(__name__)
router = APIRouter(tags=["health"])


@router.get("/health")
def health() -> dict:
    return {"status": "ok"}


@router.get("/health/db")
async def health_db() -> dict:
    try:
        async with get_pool().connection(timeout=3) as conn:
            await conn.execute("SELECT 1")
    except Exception:
        logger.exception("Kiểm tra kết nối database thất bại")
        raise HTTPException(status_code=503, detail="Database không sẵn sàng")
    return {"status": "ok"}