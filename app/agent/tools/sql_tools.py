# app/agent/tools/sql_tools.py
# Tool chạy câu SELECT cho agent (run_select_query) và các hàm dùng chung cho tool khác
# (_caller, _cell, _first_line). Tool lọc theo quyền của người hỏi (user_role, lấy từ config
# do app/api/v1/agent_route.py truyền vào) và trả lỗi dạng chữ để LLM tự sửa câu SQL.
# Khám phá database (liệt kê bảng, xem cột, dữ liệu mẫu) nằm ở explore_tools.py.

import logging
from typing import Optional

import psycopg
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

from app.agent.permissions import PermissionDenied, ensure_access
from app.config import get_settings
from app.db.guard import UnsafeSQLError, check_select
from app.db.pool import get_pool

logger = logging.getLogger(__name__)

_MAX_CELL_CHARS = 200      # cắt ô dữ liệu quá dài
_MAX_OUTPUT_CHARS = 20000  # cắt tổng kết quả để không tràn ngữ cảnh của LLM


def _caller(config: RunnableConfig) -> tuple[Optional[str], Optional[str]]:
    """Lấy (user_id, user_role) của người đang hỏi từ config của lượt chạy."""
    configurable = config.get("configurable") or {}
    return configurable.get("user_id"), configurable.get("user_role")


def _cell(value) -> str:
    if value is None:
        return "NULL"
    text = str(value).replace("\n", " ")
    return text if len(text) <= _MAX_CELL_CHARS else text[:_MAX_CELL_CHARS] + "..."


def _first_line(e: Exception) -> str:
    return (str(e).splitlines() or [""])[0]


@tool
async def run_select_query(query: str, config: RunnableConfig) -> str:
    """Chạy MỘT câu SELECT (PostgreSQL) và trả kết quả dạng bảng chữ.

    Chỉ được đọc dữ liệu, chỉ dùng các bảng người dùng được phép xem, ghi tên bảng dạng
    dwh.<bảng>. Không dùng SELECT *, luôn liệt kê cột cụ thể (xem describe_table trước).
    Kết quả bị giới hạn số dòng.
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
    except psycopg.errors.InsufficientPrivilege:
        return (
            "Không có quyền đọc một số cột trong truy vấn. Đừng dùng SELECT *, "
            "hãy chỉ chọn các cột có trong describe_table."
        )
    except psycopg.Error as e:
        logger.warning("run_select_query lỗi: %s", e)
        return f"Lỗi khi chạy truy vấn: {_first_line(e)}"

    truncated = len(rows) > max_rows
    rows = rows[:max_rows]
    if not rows:
        return "Truy vấn không trả về dòng nào."

    lines = [" | ".join(columns)]
    lines += [" | ".join(_cell(v) for v in row) for row in rows]
    if truncated:
        lines.append(f"(đã cắt, chỉ hiển thị {max_rows} dòng đầu)")
    text = "\n".join(lines)
    if len(text) > _MAX_OUTPUT_CHARS:
        text = text[:_MAX_OUTPUT_CHARS] + "\n(đã cắt bớt vì kết quả quá dài, hãy thu hẹp truy vấn)"
    return text