# app/agent/tools/sql_tools.py
# Ba tool để agent làm việc với PostgreSQL nội bộ: liệt kê bảng, xem cột của bảng,
# chạy câu SELECT. Mọi tool đều lọc theo quyền của người hỏi (user_role, lấy từ config
# do app/api/v1/agent_route.py truyền vào) và trả lỗi dạng chữ để LLM tự sửa câu SQL.

import logging
from typing import Optional

import psycopg
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

from app.agent.permissions import PermissionDenied, can_access, ensure_access, filter_tables
from app.config import get_settings
from app.db.guard import UnsafeSQLError, check_select
from app.db.pool import get_pool

logger = logging.getLogger(__name__)

_MAX_CELL_CHARS = 200  # cắt ô dữ liệu quá dài để không tràn ngữ cảnh của LLM


def _caller(config: RunnableConfig) -> tuple[Optional[str], Optional[str]]:
    """Lấy (user_id, user_role) của người đang hỏi từ config của lượt chạy."""
    configurable = config.get("configurable") or {}
    return configurable.get("user_id"), configurable.get("user_role")


def _cell(value) -> str:
    if value is None:
        return "NULL"
    text = str(value).replace("\n", " ")
    return text if len(text) <= _MAX_CELL_CHARS else text[:_MAX_CELL_CHARS] + "..."


@tool
async def list_tables(config: RunnableConfig) -> str:
    """Liệt kê các bảng/view mà người dùng hiện tại được phép xem, dạng schema.table."""
    _, role = _caller(config)
    query = (
        "SELECT table_schema, table_name FROM information_schema.tables "
        "WHERE table_schema NOT IN ('pg_catalog', 'information_schema') "
        "AND table_type IN ('BASE TABLE', 'VIEW') ORDER BY 1, 2"
    )
    try:
        async with get_pool().connection() as conn:
            rows = await (await conn.execute(query)).fetchall()
    except psycopg.Error as e:
        logger.warning("list_tables lỗi: %s", e)
        return "Không đọc được danh sách bảng từ database."

    names = filter_tables(role, (f"{schema}.{table}" for schema, table in rows))
    return "\n".join(names) if names else "Không có bảng nào bạn được phép xem."


@tool
async def describe_table(table_name: str, config: RunnableConfig) -> str:
    """Xem danh sách cột (tên, kiểu dữ liệu, có cho NULL không) của một bảng.

    table_name dạng "schema.table" hoặc "table" (mặc định schema public).
    """
    _, role = _caller(config)
    schema, _, table = table_name.strip().lower().rpartition(".")
    schema = schema or "public"
    not_found = f"Không tìm thấy bảng '{table_name}' hoặc bạn không có quyền xem."

    if not can_access(role, f"{schema}.{table}"):
        return not_found

    query = (
        "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
        "WHERE table_schema = %s AND table_name = %s ORDER BY ordinal_position"
    )
    try:
        async with get_pool().connection() as conn:
            rows = await (await conn.execute(query, (schema, table))).fetchall()
    except psycopg.Error as e:
        logger.warning("describe_table lỗi: %s", e)
        return "Không đọc được cấu trúc bảng từ database."

    if not rows:
        return not_found
    lines = [f"{name} ({dtype}, {'null' if nullable == 'YES' else 'not null'})"
             for name, dtype, nullable in rows]
    return f"Bảng {schema}.{table}:\n" + "\n".join(lines)


@tool
async def run_select_query(query: str, config: RunnableConfig) -> str:
    """Chạy MỘT câu SELECT (PostgreSQL) và trả kết quả dạng bảng chữ.

    Chỉ được đọc dữ liệu, chỉ dùng các bảng người dùng được phép xem. Nên dùng
    describe_table trước để biết đúng tên cột. Kết quả bị giới hạn số dòng.
    """
    user_id, role = _caller(config)
    max_rows = get_settings().db_max_rows

    try:
        checked = check_select(query, max_rows)
        ensure_access(role, checked.tables)
    except (UnsafeSQLError, PermissionDenied) as e:
        return f"Câu SQL bị từ chối: {e}"

    logger.info("SQL (user_id=%s, role=%s): %s", user_id, role, checked.sql)
    try:
        async with get_pool().connection() as conn:
            cursor = await conn.execute(checked.sql)
            rows = await cursor.fetchall()
            columns = [c.name for c in cursor.description]
    except psycopg.errors.QueryCanceled:
        return "Truy vấn chạy quá lâu nên bị huỷ. Hãy thu hẹp điều kiện lọc hoặc khoảng thời gian."
    except psycopg.Error as e:
        logger.warning("run_select_query lỗi: %s", e)
        return f"Lỗi khi chạy truy vấn: {str(e).splitlines()[0]}"

    truncated = len(rows) > max_rows
    rows = rows[:max_rows]
    if not rows:
        return "Truy vấn không trả về dòng nào."

    lines = [" | ".join(columns)]
    lines += [" | ".join(_cell(v) for v in row) for row in rows]
    if truncated:
        lines.append(f"(đã cắt, chỉ hiển thị {max_rows} dòng đầu)")
    return "\n".join(lines)