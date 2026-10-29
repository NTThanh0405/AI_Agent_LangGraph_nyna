# app/db/pool.py
# Connection pool (async) tới PostgreSQL nội bộ bằng tài khoản chỉ-đọc.
# Mỗi kết nối được ép ngay từ phía server: schema mặc định, read-only và các giới hạn
# thời gian (câu lệnh, chờ khoá, giao dịch treo), không phụ thuộc cài đặt của role.
# app/main.py gọi init_pool/close_pool; các tool và health check dùng get_pool.

from typing import Optional

from psycopg_pool import AsyncConnectionPool

from app.config import get_settings

_pool: Optional[AsyncConnectionPool] = None


async def init_pool() -> None:
    global _pool
    s = get_settings()
    options = " ".join(
        [
            f"-c search_path={s.db_schema}",
            f"-c statement_timeout={s.db_statement_timeout_ms}",
            "-c lock_timeout=5000",
            "-c idle_in_transaction_session_timeout=30000",
            "-c default_transaction_read_only=on",
        ]
    )
    _pool = AsyncConnectionPool(
        conninfo=s.database_url,
        min_size=s.db_pool_min,
        max_size=s.db_pool_max,
        timeout=10,
        max_lifetime=1800,
        check=AsyncConnectionPool.check_connection,  # cần psycopg_pool >= 3.2
        kwargs={"options": options},
        open=False,
    )
    # wait=True: báo lỗi ngay khi khởi động nếu không kết nối được DB
    await _pool.open(wait=True)


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def get_pool() -> AsyncConnectionPool:
    if _pool is None:
        raise RuntimeError("Connection pool chưa được khởi tạo (init_pool chưa chạy)")
    return _pool