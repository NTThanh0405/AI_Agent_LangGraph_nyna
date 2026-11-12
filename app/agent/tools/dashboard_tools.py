# app/agent/tools/dashboard_tools.py
# Tool tạo dashboard HTML theo yêu cầu người dùng. Hai cách gọi:
#   1. TRANG CÓ SẴN (ưu tiên): pages=["tong_quan", ...] + period_start/period_end. Code tự dựng SQL
#      (dashboard_presets.build_dataset_sql, gồm kỳ trước), bố cục và màu theo mẫu; LLM không viết SQL.
#   2. TỰ KHAI BÁO (yêu cầu không có trang sẵn): dashboards=[...] với preset "default", mỗi tab một câu
#      SELECT chi tiết + bộ lọc, KPI, biểu đồ.
# Code tự chạy SQL (qua đúng check_select + phân quyền theo vai trò), nhúng dataset vào
# template cố định; việc lọc và cộng gộp diễn ra ngay trên trình duyệt của người xem.
# Lưu file với tên ngẫu nhiên rồi trả MỘT link duy nhất. LLM không tự viết HTML.
# File được ghi vào DASHBOARD_DIR (volume dùng chung); container dashboards-web (nginx)
# đọc thư mục đó để phục vụ link. Số file và dung lượng thư mục có giới hạn;
# cleanup_loop xoá file quá DASHBOARD_TTL_HOURS.

import asyncio
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
from pydantic import BaseModel, Field, ValidationError

from app.agent.permissions import PermissionDenied, ensure_access_user
from app.agent.tools import dashboard_presets as P
from app.agent.tools.dashboard_presets import (
    describe_presets,
    get_preset,
    get_theme,
    preset_names,
)
from app.agent.tools.dashboard_template import logo_data_uri, render_html
from app.agent.tools.sql_tools import _caller
from app.config import get_settings
from app.db.guard import UnsafeSQLError, check_select
from app.db.pool import get_pool

logger = logging.getLogger(__name__)

VN_TZ = timezone(timedelta(hours=7))
MAX_FILES = 500
MAX_TOTAL_BYTES = 200 * 1024 * 1024
CLEANUP_INTERVAL_SECONDS = 300
MAX_DASHBOARDS = 6          # số tab tối đa trong một link
MAX_DATASET_ROWS = 10_000   # số dòng dataset tối đa của MỘT tab (dữ liệu nhúng vào file)
MAX_TOTAL_ROWS = 30_000     # tổng số dòng dataset của toàn bộ link


class FilterSpec(BaseModel):
    """Một ô bộ lọc trên đầu dashboard."""

    label: str = Field(max_length=30, description="Nhãn hiển thị, ví dụ 'Cửa hàng'")
    column: str = Field(max_length=64, description="Cột của dataset dùng để lọc")
    type: Literal["select", "date"] = Field(
        default="select",
        description="select: danh sách chọn; date: khoảng ngày (cột phải là kiểu ngày)",
    )


class KpiSpec(BaseModel):
    """Một thẻ số liệu lớn, tự so sánh với kỳ trước nếu dataset có cột ky."""

    label: str = Field(max_length=40)
    column: str = Field(max_length=64, description="Cột số của dataset")
    agg: Literal["sum", "avg", "ratio"] = Field(
        default="sum", description="ratio = tổng(column) / tổng(denom), ví dụ AOV"
    )
    denom: Optional[str] = Field(default=None, max_length=64, description="Cột mẫu số khi agg=ratio")
    format: Literal["number", "compact", "decimal"] = Field(
        default="compact", description="number: 1.234; compact: 24.74M; decimal: 2.10"
    )
    good_when: Literal["up", "down"] = Field(
        default="up", description="Tăng là tốt (xanh) hay giảm là tốt (ví dụ tỷ lệ huỷ)"
    )


class ChartSpec(BaseModel):
    """Mô tả một biểu đồ; dữ liệu được cộng gộp từ dataset theo bộ lọc."""

    title: str = Field(max_length=120, description="Tiêu đề biểu đồ, ngắn gọn")
    type: Literal["bar", "line", "pie", "table"] = Field(
        description="bar: cột; line: đường (theo thời gian); pie: tròn (tỷ trọng); table: bảng số liệu"
    )
    x: Optional[str] = Field(
        default=None, max_length=64,
        description="Cột nhãn (trục X; tên lát cắt của pie). table: cột nhóm, bỏ trống để xem dòng thô.",
    )
    x_order: Optional[str] = Field(
        default=None, max_length=64,
        description="Cột số quyết định thứ tự của x (ví dụ thu_stt cho T2..CN). Bỏ trống = sắp theo chữ.",
    )
    y: list[str] = Field(default_factory=list, max_length=6, description="Các cột SỐ làm giá trị. pie chỉ dùng cột đầu.")
    agg: Literal["sum", "avg", "ratio"] = "sum"
    denom: Optional[str] = Field(default=None, max_length=64, description="Cột mẫu số khi agg=ratio")
    compare: bool = Field(default=False, description="Vẽ thêm series kỳ trước (cần cột ky trong dataset)")
    color_by: Optional[str] = Field(
        default=None, max_length=64,
        description="Tách series và tô màu theo cột này (ví dụ ca sáng/ngày/đêm). Không dùng chung với compare.",
    )
    sort: Literal["none", "asc", "desc"] = Field(default="none", description="Sắp xếp theo giá trị của y đầu tiên")
    span: int = Field(default=6, ge=1, le=12, description="Chiếm bao nhiêu trong 12 cột (12 = cả hàng)")


class DashboardSpec(BaseModel):
    """Một dashboard con, hiển thị thành một tab trong trang."""

    name: str = Field(max_length=40, description="Tên tab, ví dụ 'Tổng quan', 'Kho'")
    preset: str = Field(
        default="default",
        description="Bố cục mẫu. Nếu dùng preset khác 'default' thì filters/kpis/charts có thể để trống.",
    )
    theme: Optional[str] = Field(default=None, description="Bảng màu; bỏ trống = theo preset")
    dataset_sql: str = Field(
        max_length=6000,
        description="MỘT câu SELECT PostgreSQL ở mức chi tiết (đã GROUP BY ngày/giờ/cửa hàng/...), "
        "alias cột rõ ràng, tên bảng dạng dwh.<bảng>. Có cột ky ('nay'/'truoc') nếu cần so sánh kỳ trước.",
    )
    filters: list[FilterSpec] = Field(default_factory=list, max_length=6)
    kpis: list[KpiSpec] = Field(default_factory=list, max_length=6)
    charts: list[ChartSpec] = Field(default_factory=list)


class _ChartError(Exception):
    """Lỗi do mô tả dashboard sai; trả lại cho LLM dạng chữ để tự sửa."""


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


def _resolve(d: DashboardSpec) -> dict:
    """Ghép preset với phần LLM tự khai báo (LLM khai báo thì ưu tiên hơn preset)."""
    if d.preset in P.PAGE_NAMES:
        raise _ChartError(
            f"dashboard '{d.name}': '{d.preset}' là trang có sẵn. Gọi lại create_dashboard với "
            f"pages=['{d.preset}'] và period_start/period_end, không truyền dashboards/dataset_sql."
        )
    try:
        p = get_preset(d.preset)
    except KeyError:
        raise _ChartError(
            f"dashboard '{d.name}': preset '{d.preset}' không tồn tại, các preset hiện có: "
            + ", ".join(preset_names())
        )
    try:
        filters = d.filters or [FilterSpec(**f) for f in p["filters"]]
        kpis = d.kpis or [KpiSpec(**k) for k in p["kpis"]]
        charts = d.charts or [ChartSpec(**c) for c in p["charts"]]
    except ValidationError as e:
        raise _ChartError(f"dashboard '{d.name}': khai báo preset lỗi: {_first_line(e)}")
    if not charts:
        raise _ChartError(f"dashboard '{d.name}': chưa có biểu đồ nào (hãy chọn preset hoặc khai báo charts)")
    try:
        theme = get_theme(d.theme or p["theme"])
    except KeyError:
        raise _ChartError(f"dashboard '{d.name}': theme '{d.theme}' không tồn tại")
    return {"filters": filters, "kpis": kpis, "charts": charts, "theme": theme}


def _validate(name: str, columns: list[str], rows: list, filters, kpis, charts) -> None:
    """Mọi cột được nhắc tới phải có trong dataset; lỗi thì báo kèm danh sách cột."""
    cols = set(columns)

    def need(where: str, *names):
        miss = [n for n in names if n and n not in cols]
        if miss:
            raise _ChartError(
                f"dashboard '{name}', {where}: dataset thiếu cột {miss}. "
                f"Các cột dataset_sql đang trả về: {columns}. "
                "Hãy sửa dataset_sql (xem list_dashboard_presets) hoặc sửa tên cột."
            )

    for f in filters:
        need(f"bộ lọc '{f.label}'", f.column)
    for k in kpis:
        if k.agg == "ratio" and not k.denom:
            raise _ChartError(f"dashboard '{name}', KPI '{k.label}': agg=ratio cần denom")
        need(f"KPI '{k.label}'", k.column, k.denom)
    for c in charts:
        where = f"biểu đồ '{c.title}'"
        if c.type == "table":
            need(where, c.x, c.x_order, c.denom, *c.y)
            continue
        if not c.x or not c.y:
            raise _ChartError(f"dashboard '{name}', {where}: cần cả x và y")
        if c.agg == "ratio" and not c.denom:
            raise _ChartError(f"dashboard '{name}', {where}: agg=ratio cần denom")
        need(where, c.x, c.x_order, c.color_by, c.denom, *c.y)
        if c.compare and "ky" not in cols:
            raise _ChartError(f"dashboard '{name}', {where}: compare cần cột ky ('nay'/'truoc') trong dataset")

    if "ky" in cols:
        iky = columns.index("ky")
        bad = {r[iky] for r in rows} - {"nay", "truoc"}
        if bad:
            raise _ChartError(f"dashboard '{name}': cột ky chỉ được nhận 'nay' hoặc 'truoc', đang có {sorted(map(str, bad))}")


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
def list_dashboard_presets() -> str:
    """Liệt kê các preset dashboard mẫu, cột dataset_sql bắt buộc và SQL mẫu.

    Gọi tool này TRƯỚC create_dashboard khi người dùng muốn một dashboard giống một mẫu có sẵn
    (ví dụ 'tổng quan hiệu suất'), để viết dataset_sql trả đúng tên cột.
    """
    return describe_presets()


VN_TODAY = lambda: datetime.now(VN_TZ).date()  # noqa: E731  (test thay bằng ngày cố định)


def _vn(d: date) -> str:
    return d.strftime("%d/%m/%Y")


def _logo() -> Optional[str]:
    path = (get_settings().dashboard_logo or "").strip()
    if not path:
        return None
    try:
        return logo_data_uri(path)
    except (OSError, ValueError):
        logger.warning("Không đọc được logo dashboard %s, bỏ qua logo", path)
        return None


async def _fetch(conn, label: str, sql: str, max_rows: int, user_id: Optional[str], role: Optional[str]) -> tuple[list, list]:
    """Chạy một câu SELECT qua đúng guard + kiểm tra quyền như mọi truy vấn khác của agent."""
    try:
        checked = check_select(sql, max_rows)
        await ensure_access_user(role, user_id, checked.tables)  # staff: tính cả khối được cấp riêng
    except (UnsafeSQLError, PermissionDenied) as e:
        raise _ChartError(f"{label}: câu SQL bị từ chối: {e}")
    cursor = await conn.execute(checked.sql)
    fetched = await cursor.fetchall()
    if len(fetched) > max_rows:
        raise _ChartError(f"{label}: dữ liệu quá {max_rows} dòng, hãy thu hẹp khoảng thời gian")
    columns = [c.name for c in cursor.description]
    return columns, [[_json_value(v) for v in r] for r in fetched]


async def _build_pages(pages: list[str], d_from: date, d_to: date,
                       user_id: Optional[str], role: Optional[str]) -> tuple[list[dict], int]:
    """Dựng các trang có sẵn. Trang dùng chung SQL (tong_quan, cua_hang, hanh_vi) chỉ truy vấn một lần."""
    cache: dict[str, tuple[list, list]] = {}
    tabs, total = [], 0
    async with get_pool().connection() as conn:
        for name in pages:
            sql = P.build_dataset_sql(name, d_from, d_to)
            if sql not in cache:
                cache[sql] = await _fetch(conn, f"trang '{name}'", sql, P.MAX_DATASET_ROWS, user_id, role)
                total += len(cache[sql][1])
                if total > MAX_TOTAL_ROWS * 2:
                    raise _ChartError("dữ liệu các trang quá lớn, hãy chọn ít trang hơn hoặc thu hẹp khoảng thời gian")
            columns, rows = cache[sql]
            try:
                tabs.append(P.build_dashboard(name, columns, rows))
            except ValueError as e:
                raise _ChartError(f"trang '{name}': {e}")
    return tabs, total


async def _build_custom(dashboards: list["DashboardSpec"], user_id: Optional[str], role: Optional[str],
                        max_charts: int) -> tuple[list[dict], int]:
    """Đường tự khai báo (preset 'default'): mỗi tab một câu SELECT + bộ lọc, KPI, biểu đồ."""
    if len(dashboards) > MAX_DASHBOARDS:
        raise _ChartError(f"tối đa {MAX_DASHBOARDS} dashboard trong một link, hãy giảm bớt")
    resolved = [_resolve(d) for d in dashboards]
    for r in resolved:
        if len(r["charts"]) > max_charts:
            raise _ChartError(f"mỗi dashboard tối đa {max_charts} biểu đồ, hãy giảm bớt")
    built, total = [], 0
    async with get_pool().connection() as conn:
        for d, r in zip(dashboards, resolved):
            name = d.name.strip()[:40] or f"Dashboard {len(built) + 1}"
            columns, rows = await _fetch(conn, f"dashboard '{name}'", d.dataset_sql, MAX_DATASET_ROWS, user_id, role)
            total += len(rows)
            if total > MAX_TOTAL_ROWS:
                raise _ChartError(f"tổng dataset của các tab vượt {MAX_TOTAL_ROWS} dòng, hãy giảm bớt hoặc gom nhóm thô hơn")
            _validate(name, columns, rows, r["filters"], r["kpis"], r["charts"])
            built.append({
                "name": name,
                "theme": r["theme"],
                "labels": dict(P.COLUMN_LABELS),
                "dataset": {"columns": columns, "rows": rows},
                "filters": [f.model_dump() for f in r["filters"]],
                "kpis": [k.model_dump() for k in r["kpis"]],
                "charts": [c.model_dump() for c in r["charts"]],
            })
    return built, total


@tool
async def create_dashboard(
    config: RunnableConfig,
    pages: Optional[list[str]] = None,
    period_start: Optional[date] = None,
    period_end: Optional[date] = None,
    title: str = "",
    dashboards: Optional[list[DashboardSpec]] = None,
) -> str:
    """Tạo trang dashboard HTML (KPI, biểu đồ, bảng, bộ lọc, so sánh kỳ trước) rồi trả về MỘT LINK để xem.

    Gọi ĐÚNG MỘT LẦN cho mỗi yêu cầu, theo một trong hai cách:

    1. Trang có sẵn (ưu tiên): truyền `pages` và kỳ `period_start`, `period_end` (YYYY-MM-DD, gồm hai đầu,
       tối đa 92 ngày). Trang: tong_quan (tổng quan hiệu suất), cua_hang, nen_tang, san_pham,
       hanh_vi (giờ cao điểm), hoi_vien, don_online. pages rỗng = 5 trang chính. Bỏ trống kỳ = tuần trọn
       vẹn gần nhất. KHÔNG viết SQL: hệ thống tự dựng SQL, kỳ trước và bố cục.
    2. Yêu cầu không có trang sẵn: truyền `dashboards` (preset 'default'), mỗi phần tử là một tab với
       MỘT dataset_sql chi tiết + filters, kpis, charts tự khai báo.

    Dashboard là ảnh chụp số liệu lúc tạo, chỉ chứa dữ liệu người hỏi được xem, và tự hết hạn.
    """
    s = get_settings()
    if not s.dashboard_base_url:
        return "Chưa cấu hình DASHBOARD_BASE_URL nên chưa tạo được dashboard."
    user_id, role = _caller(config)
    period_note = ""
    try:
        if dashboards:
            if pages:
                raise _ChartError("chỉ dùng MỘT cách: hoặc pages (trang có sẵn), hoặc dashboards (tự khai báo)")
            built, total_rows = await _build_custom(dashboards, user_id, role, s.dashboard_max_charts)
            page_title = "Dashboard"
        else:
            names = [str(p).strip().lower() for p in (pages or []) if str(p).strip()] or list(P.REPORT_PAGES)
            names = list(dict.fromkeys(names))  # bỏ trùng, giữ thứ tự
            bad = [n for n in names if n not in P.PAGE_NAMES]
            if bad:
                raise _ChartError(f"không có trang {bad}. Các trang có sẵn: {', '.join(P.PAGE_NAMES)}")
            if len(names) > MAX_DASHBOARDS + 1:
                raise _ChartError(f"tối đa {MAX_DASHBOARDS + 1} trang trong một link")
            if (period_start is None) != (period_end is None):
                raise _ChartError("cần cả period_start và period_end, hoặc bỏ trống cả hai (tuần trọn vẹn gần nhất)")
            if period_start is None:
                period_start, period_end = P.last_full_week(VN_TODAY())
            try:
                per = P.period(period_start, period_end)
            except (TypeError, ValueError) as e:
                raise _ChartError(f"kỳ không hợp lệ: {e}")
            built, total_rows = await _build_pages(names, per["d_from"], per["d_to"], user_id, role)
            page_title = built[0]["title"] if len(built) == 1 else "Báo cáo hiệu suất NYNA"
            period_note = (
                f" Kỳ {_vn(per['d_from'])}–{_vn(per['d_to'])}, so với kỳ trước "
                f"{_vn(per['p_from'])}–{_vn(per['p_to'])}. Trang: {', '.join(t['name'] for t in built)}."
            )
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
        "title": (title or "").strip()[:120] or page_title,
        "generated": now.strftime("%d/%m/%Y %H:%M"),
        "expires": (now + timedelta(hours=s.dashboard_ttl_hours)).strftime("%d/%m/%Y %H:%M"),
        "logo": _logo(),
        "dashboards": built,
    }
    # render_html thoát < > & U+2028 U+2029 nên dữ liệu không thể đóng thẻ <script> hay chèn HTML
    html = render_html(payload)

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
        "Dashboard %s... (user_id=%s, role=%s, %d tab, %d dòng dữ liệu)",
        name[:8], user_id, role, len(built), total_rows,
    )
    url = f"{s.dashboard_base_url.rstrip('/')}/{name}.html"
    return (
        f"Đã tạo dashboard '{payload['title']}' ({len(built)} trang trong cùng một link).{period_note} "
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