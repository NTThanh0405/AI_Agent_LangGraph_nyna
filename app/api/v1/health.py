# app/api/v1/health.py
# Các endpoint kiểm tra sức khoẻ dịch vụ (không cần x-api-key):
#   GET /api/v1/health          -> app còn sống không (dùng cho Docker HEALTHCHECK)
#   GET /api/v1/health/db       -> app có kết nối được tới PostgreSQL không (chờ tối đa 3 giây)
#   GET /api/v1/health/reports  -> vòng gửi báo cáo tự động: lần chạy gần nhất, có đang chạy đều không
# /health/db và /health/reports chỉ trả trạng thái chung chung, không lộ chi tiết lỗi hay thông tin
# máy chủ (chi tiết lỗi chỉ nằm trong log và audit_log).

import logging
from datetime import datetime

from fastapi import APIRouter, HTTPException

from app.db.pool import get_pool

logger = logging.getLogger(__name__)
router = APIRouter(tags=["health"])

# Vòng báo cáo kiểm tra mỗi 60 giây; quá ngần này giây không thấy chạy thì coi là đã đứng
REPORT_STALE_SECONDS = 180


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


@router.get("/health/reports")
def health_reports() -> dict:
    """status: not_started (chưa có lượt nào), ok, error (lượt gần nhất lỗi), stalled (lâu không chạy).
    Luôn trả 200 để người xem tự đọc; không gắn vào Docker HEALTHCHECK để báo cáo lỗi không làm
    container bị khởi động lại."""
    from app.reports.scheduler import get_status  # import tại chỗ để health.py nhẹ khi khởi động

    st = get_status()
    last_run = st["last_run_at"]
    if last_run is None:
        status = "error" if st["last_error"] else "not_started"
    elif st["last_error"]:
        status = "error"
    else:
        age = (datetime.now(datetime.fromisoformat(last_run).tzinfo) - datetime.fromisoformat(last_run)).total_seconds()
        status = "stalled" if age > REPORT_STALE_SECONDS else "ok"
    return {"status": status, "last_run_at": last_run, "last_sent": st["last_sent"]}