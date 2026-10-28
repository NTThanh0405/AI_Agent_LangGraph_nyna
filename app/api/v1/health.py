# app/api/v1/health.py
# Các endpoint kiểm tra sức khoẻ dịch vụ (không cần x-api-key):
#   GET /api/v1/health     -> app còn sống không (dùng cho Docker HEALTHCHECK)
#   GET /api/v1/health/db  -> app có kết nối được tới PostgreSQL không

import logging

from fastapi import APIRouter, HTTPException

from app.db.pool import get_pool  # lấy connection pool đã mở ở app/main.py

logger = logging.getLogger(__name__)
router = APIRouter(tags=["health"])


@router.get("/health")
def health() -> dict:
    """Kiểm tra nhanh app còn chạy. Không đụng tới DB nên luôn nhẹ và nhanh."""
    return {"status": "ok"}


@router.get("/health/db")
async def health_db() -> dict:
    """Kiểm tra kết nối PostgreSQL bằng một câu SELECT 1.

    Chỉ trả ok/lỗi chung chung, không lộ chi tiết lỗi hay thông tin máy chủ DB
    vì endpoint này không yêu cầu khoá API.
    """
    try:
        async with get_pool().connection() as conn:
            await conn.execute("SELECT 1")
    except Exception:
        # Ghi log đầy đủ để người vận hành xem, trả về cho client thông báo chung.
        logger.exception("Kiểm tra kết nối database thất bại")
        raise HTTPException(status_code=503, detail="Database không sẵn sàng")
    return {"status": "ok"}