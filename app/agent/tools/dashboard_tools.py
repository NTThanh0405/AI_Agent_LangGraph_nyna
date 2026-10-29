# app/agent/tools/dashboard_tools.py
# Tool tạo dashboard HTML theo yêu cầu người dùng.
# LLM chỉ mô tả dashboard (tiêu đề + danh sách biểu đồ, mỗi biểu đồ một câu SELECT);
# code tự chạy SQL (qua đúng check_select + phân quyền theo vai trò), đổ dữ liệu vào
# template cố định, lưu file với tên ngẫu nhiên rồi trả link. LLM không tự viết HTML.
# File được ghi vào DASHBOARD_DIR (volume dùng chung); container dashboards-web (nginx)
# đọc thư mục đó để phục vụ link. Số file và dung lượng thư mục có giới hạn;
# cleanup_loop xoá file quá DASHBOARD_TTL_HOURS.

import asyncio
import json
import logging
import math
import os
import secrets
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Literal, Optional

import psycopg
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from pydantic import BaseModel, Field

from app.agent.permissions import PermissionDenied, ensure_access
from app.agent.tools.dashboard_template import TEMPLATE
from app.agent.tools.sql_tools import _caller
from app.config import get_settings
from app.db.guard import UnsafeSQLError, check_select
from app.db.pool import get_pool

logger = logging.getLogger(__name__)

VN_TZ = timezone(timedelta(hours=7))
MAX_CHART_POINTS = 60
MAX_FILES = 500
MAX_TOTAL_BYTES = 200 * 1024 * 1024
CLEANUP_INTERVAL_SECONDS = 300


class ChartSpec(BaseModel):
    """Mô tả một biểu đồ trong dashboard."""

    title: str = Field(max_length=120, description="Tiêu đề biểu đồ, ngắn gọn")
    type: Literal["bar", "line", "pie", "table"] = Field(
        description="bar: cột; line: đường (theo thời gian); pie: tròn (tỷ trọng); table: bảng số liệu"
    )
    sql: str = Field(
        max_length=4000,
        description="MỘT câu SELECT PostgreSQL nhẹ, đã GROUP BY sẵn, đặt alias cột rõ ràng. "
        "Luôn ghi tên bảng dạng dwh.<bảng>. Biểu đồ line: luôn ORDER BY cột x tăng dần.",
    )
    x: Optional[str] = Field(
        default=None,
        max_length=64,
        description="Tên cột làm nhãn (trục X của bar/line, tên lát cắt của pie). Không cần với table.",
    )
    y: list[str] = Field(
        default_factory=list,
        max_length=6,
        description="Tên các cột SỐ làm giá trị. pie chỉ dùng cột đầu. Không cần với table.",
    )


class _ChartError(Exception):
    """Lỗi do mô tả biểu đồ sai; trả lại cho LLM dạng chữ để tự sửa."""


def _first_line(e: Exception) -> str:
    return (str(e).splitlines() or [""])[0]


def _json_value(v):
    if v is None or isinstance(v, (bool, int, str)):
        return v if not isinstance(v, str) else v[:200]
    if isinstance(v, (float, Decimal)):
        f = float(v)
        return f if math.isfinite(f) else None
    if isinstance(v, datetime):
        if v.tzinfo is not None:
            v = v.astimezone(VN_TZ)
        return v.strftime("%Y-%m-%d %H:%M")
    if isinstance(v, date):
        return v.isoformat()
    return str(v)[:200]


def _number(v, title: str, column: str):
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise _ChartError(f"biểu đồ '{title}': cột '{column}' không phải kiểu số")
    return f if math.isfinite(f) else None


async def _build_chart(conn, spec: ChartSpec, role: Optional[str], max_rows: int) -> dict:
    """Chạy câu SELECT của một biểu đồ và đóng gói dữ liệu cho template."""
    try:
        checked = check_select(spec.sql, max_rows)
        ensure_access(role, checked.tables)
    except (UnsafeSQLError, PermissionDenied) as e:
        raise _ChartError(f"biểu đồ '{spec.title}': câu SQL bị từ chối: {e}")

    cursor = await conn.execute(checked.sql)
    fetched = await cursor.fetchall()
    truncated = len(fetched) > max_rows
    rows = fetched[:max_rows]
    columns = [c.name for c in cursor.description]

    if spec.type == "table":
        title = f"{spec.title} (chỉ hiển thị {max_rows} dòng đầu)" if truncated else spec.title
        return {
            "title": title, "type": "table", "columns": columns,
            "rows": [[_json_value(v) for v in r] for r in rows],
        }

    if not spec.x or spec.x not in columns:
        raise _ChartError(f"biểu đồ '{spec.title}': x phải là một trong các cột {columns}")
    value_cols = spec.y[:1] if spec.type == "pie" else spec.y
    if not value_cols or any(y not in columns for y in value_cols):
        raise _ChartError(f"biểu đồ '{spec.title}': y phải là các cột số trong {columns}")
    if truncated or len(rows) > MAX_CHART_POINTS:
        raise _ChartError(
            f"biểu đồ '{spec.title}': có hơn {MAX_CHART_POINTS} điểm dữ liệu, "
            "hãy gom nhóm lại hoặc dùng ORDER BY ... LIMIT"
        )

    ix = columns.index(spec.x)
    labels = ["(trống)" if r[ix] is None else str(_json_value(r[ix])) for r in rows]
    series = []
    for y in value_cols:
        iy = columns.index(y)
        series.append({"name": y, "values": [_number(r[iy], spec.title, y) for r in rows]})
    return {
        "title": spec.title, "type": spec.type, "x_name": spec.x,
        "labels": labels, "series": series,
    }


def _dir_full(directory: Path) -> bool:
    count = total = 0
    for p in directory.glob("*.html"):
        try:
            total += p.stat().st_size
            count += 1
        except FileNotFoundError:  # vừa bị cleanup xoá
            pass
    return count >= MAX_FILES or total >= MAX_TOTAL_BYTES


def _write_atomic(tmp: Path, final: Path, html: str) -> None:
    tmp.write_text(html, encoding="utf-8")
    os.replace(tmp, final)  # nginx chỉ phục vụ *.html nên không bao giờ thấy file dở dang


@tool
async def create_dashboard(title: str, charts: list[ChartSpec], config: RunnableConfig) -> str:
    """Tạo dashboard HTML trực quan (biểu đồ cột/đường/tròn và bảng) rồi trả về LINK để xem.

    Chỉ dùng khi người dùng yêu cầu dashboard, biểu đồ hoặc báo cáo trực quan.
    Mỗi biểu đồ gồm một câu SELECT nhẹ, đã gom nhóm sẵn. Chỉ dùng bảng người dùng được phép xem.
    Dashboard là ảnh chụp số liệu lúc tạo và tự hết hạn sau một thời gian.
    """
    s = get_settings()
    if not s.dashboard_base_url:
        return "Chưa cấu hình DASHBOARD_BASE_URL nên chưa tạo được dashboard."
    if not charts:
        return "Cần ít nhất một biểu đồ."
    if len(charts) > s.dashboard_max_charts:
        return f"Mỗi dashboard tối đa {s.dashboard_max_charts} biểu đồ, hãy giảm bớt."

    user_id, role = _caller(config)
    built = []
    try:
        async with get_pool().connection() as conn:
            for spec in charts:
                built.append(await _build_chart(conn, spec, role, s.db_max_rows))
    except _ChartError as e:
        return f"Không tạo được dashboard: {e}"
    except psycopg.errors.QueryCanceled:
        return "Một truy vấn chạy quá lâu nên bị huỷ. Hãy thu hẹp điều kiện lọc hoặc khoảng thời gian."
    except psycopg.errors.InsufficientPrivilege:
        return (
            "Không có quyền đọc một số cột trong truy vấn. Đừng dùng SELECT *, "
            "hãy chỉ chọn các cột có trong describe_table."
        )
    except psycopg.Error as e:
        logger.warning("create_dashboard lỗi: %s", e)
        return f"Lỗi khi chạy truy vấn: {_first_line(e)}"

    now = datetime.now(VN_TZ)
    payload = {
        "title": title.strip()[:120] or "Dashboard",
        "generated": now.strftime("%d/%m/%Y %H:%M"),
        "expires": (now + timedelta(hours=s.dashboard_ttl_hours)).strftime("%d/%m/%Y %H:%M"),
        "charts": built,
    }
    # Thoát <, >, & để dữ liệu không thể đóng thẻ <script> hay chèn HTML
    blob = (
        json.dumps(payload, ensure_ascii=False)
        .replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
    )
    html = TEMPLATE.replace("__PAYLOAD__", blob)

    directory = Path(s.dashboard_dir)
    name = secrets.token_hex(24)  # 192 bit ngẫu nhiên, không đoán được
    try:
        directory.mkdir(parents=True, exist_ok=True)
        if await asyncio.to_thread(_dir_full, directory):
            return "Kho dashboard đang đầy, vui lòng thử lại sau ít phút."
        await asyncio.to_thread(
            _write_atomic, directory / f"{name}.tmp", directory / f"{name}.html", html
        )
    except OSError:
        logger.exception("Không ghi được file dashboard vào %s", directory)
        return "Không lưu được dashboard (lỗi ghi file). Hãy báo người quản trị."

    logger.info(
        "Dashboard %s... (user_id=%s, role=%s, %d biểu đồ)", name[:8], user_id, role, len(built)
    )
    url = f"{s.dashboard_base_url.rstrip('/')}/{name}.html"
    return (
        f"Đã tạo dashboard '{payload['title']}'. "
        f"Link xem (hết hạn sau {s.dashboard_ttl_hours} giờ): {url}\n"
        "Hãy gửi nguyên văn link này cho người dùng."
    )


def _cleanup_once(directory: Path, max_age_seconds: float) -> int:
    if not directory.is_dir():
        return 0
    removed = 0
    cutoff = time.time() - max_age_seconds
    for pattern in ("*.html", "*.tmp"):
        for p in directory.glob(pattern):
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink()
                    removed += 1
            except OSError:
                logger.warning("Không xoá được %s", p.name)
    return removed


async def cleanup_loop() -> None:
    """Chạy nền: xoá dashboard quá hạn. Gọi bằng asyncio.create_task trong lifespan."""
    s = get_settings()
    while True:
        try:
            removed = await asyncio.to_thread(
                _cleanup_once, Path(s.dashboard_dir), s.dashboard_ttl_hours * 3600
            )
            if removed:
                logger.info("Đã xoá %d dashboard hết hạn", removed)
        except Exception:
            logger.exception("Dọn dashboard lỗi")
        await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)