# app/reports/builder.py
# Dựng nội dung báo cáo sáng cho một manager: chạy các file SQL viết sẵn trong
# app/agent/queries/ (KHÔNG để LLM sinh SQL lúc 7h sáng), so với kỳ trước, rồi ghép thành tin
# nhắn chữ thuần. Cùng nguồn "hôm qua" với agent (app/agent/periods.py) nên số liệu khớp nhau.
#
# QUY ƯỚC CHO FILE SQL trong queries/ (đổi _params() nếu file của bạn dùng kiểu khác):
#   dùng tham số tên của psycopg: %(start)s (ngày đầu, gồm), %(end)s (ngày cuối, gồm),
#   %(end_exclusive)s (ngày sau ngày cuối, dùng cho  col >= %(start)s AND col < %(end_exclusive)s).
# Bảng mà mỗi mục dùng được đọc tự động từ chính câu SQL để kiểm tra quyền (permissions.py).

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Optional

from app.agent.periods import Period, comparison_periods, resolve_period, today_local
from app.agent.permissions import can_access
from app.db.guard import UnsafeSQLError, check_select

logger = logging.getLogger(__name__)

QUERY_DIR = Path(__file__).resolve().parents[1] / "agent" / "queries"
MAX_FETCH_ROWS = 50
TELEGRAM_LIMIT = 4000  # giới hạn thật là 4096, chừa chỗ an toàn


class ReportError(Exception):
    """Không dựng được báo cáo (không mục nào chạy được)."""


@dataclass(frozen=True)
class Section:
    title: str
    sql_file: str
    top_n: int = 10  # kết quả nhiều dòng thì chỉ hiện từng này dòng đầu


# Khoá = tên dùng trong reports.yaml
SECTIONS: dict[str, Section] = {
    "kpi_summary": Section("Chỉ số chính", "kpi_summary.sql"),
    "revenue_by_day": Section("Doanh thu theo ngày", "revenue_by_day.sql"),
    "revenue_by_channel": Section("Doanh thu theo kênh", "revenue_by_channel.sql"),
    "top_products": Section("Sản phẩm bán chạy", "top_products.sql", top_n=5),
    "customers_new_vs_returning": Section("Khách mới và khách quay lại", "customers_new_vs_returning.sql"),
    "target_vs_actual": Section("Thực tế so với mục tiêu", "target_vs_actual.sql"),
}

# Tên cột -> nhãn tiếng Việt. Cột không có ở đây thì dùng tên cột (bỏ dấu gạch dưới).
COLUMN_LABELS = {
    "revenue": "Doanh thu", "total_revenue": "Doanh thu", "gmv": "GMV",
    "orders": "Số đơn", "order_count": "Số đơn", "total_orders": "Số đơn",
    "customers": "Số khách", "new_customers": "Khách mới", "returning_customers": "Khách quay lại",
    "aov": "Giá trị đơn trung bình", "quantity": "Số lượng", "units": "Số lượng",
    "product": "Sản phẩm", "product_name": "Sản phẩm", "channel": "Kênh",
    "target": "Mục tiêu", "actual": "Thực tế", "achievement_pct": "Tỷ lệ đạt",
    "day": "Ngày", "date": "Ngày",
}
# Đoán cách hiển thị theo tên cột (đoán sai thì thêm/bớt từ khoá ở đây)
_MONEY_HINTS = ("revenue", "doanh_thu", "amount", "gmv", "sales", "profit", "loi_nhuan",
                "aov", "price", "cost", "chi_phi")
_PCT_HINTS = ("pct", "percent", "ty_le", "rate")


# ---------------------------------------------------------------- định dạng số

def _is_number(v) -> bool:
    return isinstance(v, (int, float, Decimal)) and not isinstance(v, bool)


def _vn(n: float, decimals: int = 0) -> str:
    """1234567.8 -> '1.234.568' (dấu chấm hàng nghìn, dấu phẩy thập phân kiểu Việt Nam)."""
    s = f"{n:,.{decimals}f}"
    return s.replace(",", "\0").replace(".", ",").replace("\0", ".")


def _col_kind(col: str) -> str:
    c = col.lower()
    if any(h in c for h in _PCT_HINTS):
        return "pct"
    if any(h in c for h in _MONEY_HINTS):
        return "money"
    return "num"


def _label(col: str) -> str:
    return COLUMN_LABELS.get(col.lower()) or col.replace("_", " ").capitalize()


def fmt_value(v, col: str = "") -> str:
    if v is None:
        return "—"
    if isinstance(v, datetime):
        return v.strftime("%d/%m/%Y %H:%M")
    if isinstance(v, date):
        return v.strftime("%d/%m/%Y")
    if _is_number(v):
        kind = _col_kind(col)
        if kind == "pct":
            return f"{_vn(float(v), 1)}%"
        if kind == "money":
            return f"{_vn(float(v))} ₫"
        if isinstance(v, int) or float(v).is_integer():
            return _vn(float(v))
        return _vn(float(v), 1)
    text = str(v).replace("\n", " ")
    return text if len(text) <= 60 else text[:60] + "..."


def delta_text(cur, prev) -> Optional[str]:
    """'▲ 8,2%' / '▼ 3,1%'. Kỳ so sánh bằng 0 hoặc không có thì trả None (không chia cho 0)."""
    if not (_is_number(cur) and _is_number(prev)) or float(prev) == 0:
        return None
    pct = (float(cur) - float(prev)) / abs(float(prev)) * 100
    if round(pct, 1) == 0:
        return "≈ 0%"
    return f"{'▲' if pct > 0 else '▼'} {_vn(abs(pct), 1)}%"


# ---------------------------------------------------------------- chạy SQL

@dataclass(frozen=True)
class Result:
    columns: list
    rows: list  # list các tuple


_PARAM_RE = re.compile(r"%\((\w+)\)s")


@lru_cache(maxsize=64)
def _load_sql(file: str) -> str:
    return (QUERY_DIR / file).read_text(encoding="utf-8-sig").strip().rstrip(";")


@lru_cache(maxsize=64)
def _tables_of(sql: str) -> frozenset:
    """Các bảng mà câu SQL dùng, để kiểm tra quyền. Thay tham số bằng ngày giả cho parser đọc được;
    check_select cũng đảm bảo file SQL chỉ là SELECT."""
    return check_select(_PARAM_RE.sub("'2000-01-01'", sql), 1).tables


def _params(p: Period) -> dict:
    return {"start": p.start, "end": p.end, "end_exclusive": p.end_exclusive}


async def _fetch(pool, sql: str, period: Period) -> Result:
    async with pool.connection() as conn:
        cur = await conn.execute(sql, _params(period))
        rows = await cur.fetchall()
        columns = [c.name for c in cur.description]
    tuples = [tuple(r.values()) if isinstance(r, dict) else tuple(r) for r in rows[:MAX_FETCH_ROWS]]
    return Result(columns, tuples)


# ---------------------------------------------------------------- dựng chữ cho từng mục

def _render_single(res: Result, comparisons: list) -> list[str]:
    """Kết quả 1 dòng (chỉ số tổng hợp): mỗi cột một dòng, kèm % thay đổi so với các kỳ so sánh."""
    row = res.rows[0]
    lines = []
    for i, col in enumerate(res.columns):
        val = row[i]
        line = f"• {_label(col)}: {fmt_value(val, col)}"
        if _is_number(val) and _col_kind(col) != "pct":
            parts = []
            for name, cres in comparisons:
                if cres is None or not cres.rows or col not in cres.columns:
                    continue
                d = delta_text(val, cres.rows[0][cres.columns.index(col)])
                if d:
                    parts.append(f"{d} so với {name}")
            if parts:
                line += " (" + "; ".join(parts) + ")"
        lines.append(line)
    return lines


def _render_multi(res: Result, top_n: int) -> list[str]:
    """Kết quả nhiều dòng (theo kênh, sản phẩm...): cột chữ đầu tiên làm tên dòng."""
    label_idx = next(
        (i for i in range(len(res.columns)) if not _is_number(res.rows[0][i])), None
    )
    lines = []
    for n, row in enumerate(res.rows[:top_n], 1):
        name = fmt_value(row[label_idx]) if label_idx is not None else f"Dòng {n}"
        if label_idx is not None and row[label_idx] is None:
            name = "(không rõ)"
        rest = ", ".join(
            f"{_label(c)}: {fmt_value(v, c)}"
            for i, (c, v) in enumerate(zip(res.columns, row))
            if i != label_idx
        )
        lines.append(f"{n}. {name}" + (f" — {rest}" if rest else ""))
    if len(res.rows) > top_n:
        lines.append(f"(còn {len(res.rows) - top_n} dòng nữa)")
    return lines


async def _run_section(pool, sql: str, section: Section, period: Period) -> list[str]:
    cur = await _fetch(pool, sql, period)
    if not cur.rows:
        return ["• Không có dữ liệu trong kỳ này."]
    if len(cur.rows) > 1:
        return _render_multi(cur, section.top_n)
    comparisons = []
    for name, cp in comparison_periods(period):
        try:
            comparisons.append((name, await _fetch(pool, sql, cp)))
        except Exception:  # thiếu kỳ so sánh thì vẫn hiện số liệu chính
            logger.warning("Không lấy được kỳ so sánh '%s'", name, exc_info=True)
            comparisons.append((name, None))
    return _render_single(cur, comparisons)


# ---------------------------------------------------------------- báo cáo hoàn chỉnh

@dataclass(frozen=True)
class BuiltReport:
    text: str
    period: Period
    ok: tuple
    failed: tuple
    denied: tuple


async def build_report(
    pool, role: str, sections, today: Optional[date] = None
) -> BuiltReport:
    """Dựng báo cáo ngày hôm qua cho `role`, gồm các mục trong `sections`.

    - Mục vượt quyền của role: bỏ qua (phòng trường hợp bị hạ quyền sau khi admin đã cấu hình).
    - Mục lỗi: ghi một dòng cảnh báo trong báo cáo, các mục khác vẫn gửi.
    - Không mục nào chạy được: raise ReportError để bộ lập lịch thử lại, không gửi tin rỗng.
    """
    today = today or today_local()
    period = resolve_period("hôm qua", today)

    blocks, ok, failed, denied = [], [], [], []
    for key in sections:
        section = SECTIONS.get(key)
        if section is None:
            failed.append(key)
            continue
        try:
            sql = _load_sql(section.sql_file)
            tables = _tables_of(sql)
        except (OSError, UnsafeSQLError, ValueError):
            logger.exception("Không đọc được file SQL của mục '%s'", key)
            failed.append(key)
            continue
        if any(not can_access(role, t) for t in tables):
            logger.warning("Mục '%s' vượt quyền của role '%s', bỏ qua", key, role)
            denied.append(key)
            continue
        try:
            lines = await _run_section(pool, sql, section, period)
        except Exception:
            logger.exception("Chạy mục '%s' lỗi", key)
            failed.append(key)
            blocks.append([f"▪ {section.title}", "⚠ Tạm thời không lấy được số liệu mục này."])
            continue
        ok.append(key)
        blocks.append([f"▪ {section.title}", *lines])

    if not ok:
        raise ReportError(
            f"Không mục nào chạy được (lỗi: {failed or '-'}, vượt quyền: {denied or '-'})"
        )

    header = [f"📊 Báo cáo kinh doanh {period.label}"]
    text = "\n".join(header) + "\n\n" + "\n\n".join("\n".join(b) for b in blocks)
    return BuiltReport(text, period, tuple(ok), tuple(failed), tuple(denied))


# ---------------------------------------------------------------- cắt tin dài

def split_text(text: str, limit: int = TELEGRAM_LIMIT) -> list[str]:
    """Cắt tin dài thành nhiều tin <= limit ký tự, ưu tiên cắt ở chỗ xuống dòng."""
    chunks, current = [], ""
    for para in text.split("\n\n"):
        candidate = f"{current}\n\n{para}" if current else para
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
            current = ""
        while len(para) > limit:  # đoạn quá dài: cắt ở dòng, không thì cắt cứng
            cut = para.rfind("\n", 0, limit)
            cut = cut if cut > 0 else limit
            chunks.append(para[:cut])
            para = para[cut:].lstrip("\n")
        current = para
    if current:
        chunks.append(current)
    return chunks