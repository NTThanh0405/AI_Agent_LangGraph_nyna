# app/agent/tools/explore_tools.py
# Ba tool để agent khám phá database (schema mặc định db_schema = dwh) trước khi viết SQL: liệt kê bảng, xem cột của bảng,
# xem vài dòng dữ liệu mẫu. Mọi tool đều lọc theo quyền của người hỏi (user_role, lấy từ
# config do app/api/v1/agent_route.py truyền vào), không đụng tới schema hệ thống, và trả
# lỗi dạng chữ để LLM tự xử lý. Chạy câu SELECT thật nằm ở sql_tools.py.

import logging
from typing import Optional

import psycopg
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from psycopg import sql

from app.agent.permissions import can_access, filter_tables
from app.agent.tools.sql_tools import _caller, _cell
from app.config import get_settings
from app.db.pool import get_pool

logger = logging.getLogger(__name__)

SAMPLE_DEFAULT_ROWS = 5
SAMPLE_MAX_ROWS = 10  # trần cứng, dù LLM xin bao nhiêu

_SYSTEM_SCHEMAS = {"pg_catalog", "information_schema"}

# Cột có tên chứa một trong các cụm này sẽ bị che thành *** trong dữ liệu mẫu.
# SỬA theo database thật của bạn (tên cột tiếng Việt không dấu cũng nên thêm vào).
SENSITIVE_COLUMN_PARTS = (
    "password", "passwd", "secret", "token", "api_key", "credential",
    "phone", "mobile", "sdt", "dien_thoai",
    "email", "address", "dia_chi",
    "cccd", "cmnd", "passport", "tax_code", "ma_so_thue",
    "bank", "card", "iban",
)


def _is_sensitive(column: str) -> bool:
    name = column.lower()
    return any(part in name for part in SENSITIVE_COLUMN_PARTS)


def _resolve(table_name: str, role: Optional[str]) -> Optional[tuple[str, str]]:
    """Tách "schema.table" (mặc định schema db_schema, tức dwh) và kiểm tra quyền.

    Trả về (schema, table), hoặc None nếu tên rỗng, thuộc schema hệ thống hoặc
    vai trò hiện tại không được xem bảng này.
    """
    schema, _, table = table_name.strip().lower().rpartition(".")
    schema = schema or get_settings().db_schema
    if not table or schema in _SYSTEM_SCHEMAS or schema.startswith("pg_") or table.startswith("pg_"):
        return None
    if not can_access(role, f"{schema}.{table}"):
        return None
    return schema, table


async def _fetch_columns(conn, schema: str, table: str) -> list[tuple[str, str, str]]:
    """Lấy (tên cột, kiểu dữ liệu, is_nullable) của bảng.

    information_schema chỉ liệt kê cột mà tài khoản agent_readonly có quyền đọc,
    nên nếu bạn cấp SELECT theo cột thì cột bị che sẽ không xuất hiện ở đây.
    """
    query = (
        "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
        "WHERE table_schema = %s AND table_name = %s ORDER BY ordinal_position"
    )
    return await (await conn.execute(query, (schema, table))).fetchall()


@tool
async def list_tables(config: RunnableConfig) -> str:
    """Liệt kê các bảng/view mà người dùng hiện tại được phép xem, dạng schema.table."""
    _, role = _caller(config)
    schema = get_settings().db_schema
    query = (
        "SELECT table_schema, table_name FROM information_schema.tables "
        "WHERE table_schema = %s AND table_type IN ('BASE TABLE', 'VIEW') ORDER BY 1, 2"
    )
    try:
        async with get_pool().connection() as conn:
            rows = await (await conn.execute(query, (schema,))).fetchall()
    except psycopg.Error as e:
        logger.warning("list_tables lỗi: %s", e)
        return "Không đọc được danh sách bảng từ database."

    names = filter_tables(role, (f"{s}.{t}" for s, t in rows))
    return "\n".join(names) if names else "Không có bảng nào bạn được phép xem."


@tool
async def describe_table(table_name: str, config: RunnableConfig) -> str:
    """Xem danh sách cột (tên, kiểu dữ liệu, có cho NULL không) của một bảng.

    table_name dạng "dwh.table" hoặc "table" (mặc định schema dwh). Chỉ hiện các cột
    được phép đọc; thông tin cá nhân của khách không nằm trong danh sách.
    """
    _, role = _caller(config)
    not_found = f"Không tìm thấy bảng '{table_name}' hoặc bạn không có quyền xem."

    target = _resolve(table_name, role)
    if target is None:
        return not_found
    schema, table = target

    try:
        async with get_pool().connection() as conn:
            rows = await _fetch_columns(conn, schema, table)
    except psycopg.Error as e:
        logger.warning("describe_table lỗi: %s", e)
        return "Không đọc được cấu trúc bảng từ database."

    if not rows:
        return not_found
    lines = [
        f"{name} ({dtype}, {'null' if nullable == 'YES' else 'not null'})"
        for name, dtype, nullable in rows
    ]
    return f"Bảng {schema}.{table}:\n" + "\n".join(lines)


@tool
async def get_sample_rows(
    table_name: str, config: RunnableConfig, limit: int = SAMPLE_DEFAULT_ROWS
) -> str:
    """Xem vài dòng dữ liệu mẫu của một bảng để biết giá trị thật trong từng cột.

    Dùng để hiểu cách dữ liệu được lưu (ví dụ cột status có giá trị gì, ngày lưu dạng nào,
    tiền tính theo đơn vị nào) trước khi viết câu SELECT. Đây chỉ là vài dòng bất kỳ,
    KHÔNG dùng làm số liệu trả lời; muốn số liệu hãy chạy câu SELECT tổng hợp.
    table_name dạng "dwh.table" hoặc "table" (mặc định schema dwh).
    """
    user_id, role = _caller(config)
    not_found = f"Không tìm thấy bảng '{table_name}' hoặc bạn không có quyền xem."

    target = _resolve(table_name, role)
    if target is None:
        return not_found
    schema, table = target
    limit = max(1, min(int(limit), SAMPLE_MAX_ROWS))

    try:
        async with get_pool().connection() as conn:
            columns = [name for name, _, _ in await _fetch_columns(conn, schema, table)]
            if not columns:
                return not_found
            # Chỉ chọn đúng các cột đọc được (không dùng SELECT *), tên được quote an toàn.
            query = sql.SQL("SELECT {cols} FROM {tbl} LIMIT {n}").format(
                cols=sql.SQL(", ").join(sql.Identifier(c) for c in columns),
                tbl=sql.Identifier(schema, table),
                n=sql.Literal(limit),
            )
            rows = await (await conn.execute(query)).fetchall()
    except psycopg.errors.QueryCanceled:
        return "Truy vấn lấy dữ liệu mẫu chạy quá lâu nên bị huỷ."
    except psycopg.errors.InsufficientPrivilege:
        return not_found
    except psycopg.Error as e:
        logger.warning("get_sample_rows lỗi: %s", e)
        return "Không đọc được dữ liệu mẫu từ database."

    logger.info("Sample rows (user_id=%s, role=%s): %s.%s", user_id, role, schema, table)
    if not rows:
        return f"Bảng {schema}.{table} hiện chưa có dữ liệu."

    masked = [i for i, c in enumerate(columns) if _is_sensitive(c)]
    lines = [" | ".join(columns)]
    for row in rows:
        cells = [
            "***" if i in masked and value is not None else _cell(value)
            for i, value in enumerate(row)
        ]
        lines.append(" | ".join(cells))

    header = f"Dữ liệu mẫu {schema}.{table} ({len(rows)} dòng, không theo thứ tự cụ thể):"
    if masked:
        header += "\n(Đã che các cột nhạy cảm: " + ", ".join(columns[i] for i in masked) + ")"
    return header + "\n" + "\n".join(lines)