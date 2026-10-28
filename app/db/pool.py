# app/db/pool.py
# Connection pool (async) tới PostgreSQL nội bộ bằng tài khoản chỉ-đọc.
# Mỗi kết nối được ép chế độ read-only và có statement_timeout ngay từ phía server.
# app/main.py gọi init_pool/close_pool; các tool và health check dùng get_pool.

from typing import Optional

from psycopg_pool import AsyncConnectionPool

from app.config import get_settings

_pool: Optional[AsyncConnectionPool] = None


async def init_pool() -> None:
    """Mở pool kết nối. Gọi một lần khi app khởi động."""
    global _pool
    s = get_settings()
    options = (
        f"-c statement_timeout={s.db_statement_timeout_ms} "
        "-c default_transaction_read_only=on"
    )
    _pool = AsyncConnectionPool(
        conninfo=s.database_url,
        min_size=s.db_pool_min,
        max_size=s.db_pool_max,
        kwargs={"options": options},
        open=False,
    )
    # wait=True: báo lỗi ngay khi khởi động nếu không kết nối được DB
    await _pool.open(wait=True)


async def close_pool() -> None:
    """Đóng pool. Gọi khi app tắt."""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def get_pool() -> AsyncConnectionPool:
    """Lấy pool đang mở; báo lỗi rõ ràng nếu chưa khởi tạo."""
    if _pool is None:
        raise RuntimeError("Connection pool chưa được khởi tạo (init_pool chưa chạy)")
    return _pool