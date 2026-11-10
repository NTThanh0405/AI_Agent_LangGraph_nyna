# app/reports/builder.py
# Dựng nội dung báo cáo sáng cho một manager: chạy các file SQL viết sẵn trong
# app/agent/queries/ (KHÔNG để LLM sinh SQL lúc 7h sáng), so với kỳ trước, rồi ghép thành tin
# nhắn chữ thuần. Cùng nguồn "hôm qua" với agent (app/agent/periods.py) nên số liệu khớp nhau.
#
# AN TOÀN: mỗi file SQL đi qua guard.check_template(). Câu SQL được CHẠY là chuỗi guard trả về
# (đã gán schema cho bảng thiếu schema), không phải nội dung gốc của file, nên bảng được kiểm tra
# quyền và bảng thực sự đọc luôn là một.
#
# QUY ƯỚC CHO FILE SQL trong queries/ (đổi _params() và PARAM_NAMES nếu file của bạn dùng kiểu khác):
#   dùng tham số tên của psycopg: %(start)s (ngày đầu, gồm), %(end)s (ngày cuối, gồm),
#   %(end_exclusive)s (ngày sau ngày cuối, dùng cho  col >= %(start)s AND col < %(end_exclusive)s).
#   Ký tự % thật (ví dụ LIKE 'a%') phải viết %%. Chỉ được là một câu SELECT.
# Bảng mà mỗi mục dùng được đọc tự động từ chính câu SQL để kiểm tra quyền (permissions.py).
# File SQL được nạp một lần rồi giữ trong RAM: sửa file trong queries/ thì phải restart service.

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Awaitable, Callable, Optional

from app.agent.periods import Period, comparison_periods, resolve_period, today_local
from app.agent.permissions import can_access
from app.db.guard import CheckedQuery, UnsafeSQLError, check_template

logger = logging.getLogger(__name__)

QUERY_DIR = Path(__file__).resolve().parents[1] / "agent" / "queries"
MAX_FETCH_ROWS = 50
TELEGRAM_LIMIT = 4000  # giới hạn thật là 4096, chừa chỗ an toàn
PARAM_NAMES = frozenset({"start", "end", "end_exclusive"})  # khớp _params() bên dưới


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


@lru_cache(maxsize=64)
def _prepare(file: str) -> CheckedQuery:
    """Đọc file SQL và cho qua guard: chỉ một câu SELECT, bảng được gán schema, tham số hợp lệ.
    Kết quả (SQL an toàn + tập bảng) được giữ trong RAM. Lỗi thì raise, lru_cache không nhớ lỗi."""
    raw = (QUERY_DIR / file).read_text(encoding="utf-8-sig").strip().rstrip(";")
    return check_template(raw, allowed_params=PARAM_NAMES)


def _params(p: Period) -> dict:
    return {"start": p.start, "end": p.end, "end_exclusive": p.end_exclusive}


async def _fetch(pool, sql: str, period: Period) -> Result:
    async with pool.connection() as conn:
        cur = await conn.execute(sql, _params(period))
        rows = await cur.fetchmany(MAX_FETCH_ROWS)
        columns = [c.name for c in cur.description]
    tuples = [tuple(r.values()) if isinstance(r, dict) else tuple(r) for r in rows]
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


# ---------------------------------------------------------------- nhận xét (tuỳ chọn, v2)

_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)*%?")


def _numbers_ok(comment: str, report_text: str) -> bool:
    """Chống bịa số: mọi số trong nhận xét phải xuất hiện trong báo cáo. Số nguyên nhỏ (<= 10, không
    kèm %) được bỏ qua để câu như "top 3 sản phẩm" không bị loại oan. Đây là kiểm tra thô."""
    for tok in _NUMBER_RE.findall(comment):
        if tok.isdigit() and int(tok) <= 10:
            continue
        if tok not in report_text:
            return False
    return True


async def _commentary_block(
    fn: Callable[[str], Awaitable[str]], report_text: str, timeout: float
) -> Optional[str]:
    """Gọi hàm viết nhận xét. Lỗi, quá thời gian, rỗng hoặc có số không có trong báo cáo thì trả None
    (báo cáo vẫn được gửi, chỉ không có nhận xét)."""
    try:
        raw = await asyncio.wait_for(fn(report_text), timeout)
    except Exception:  # gồm cả hết thời gian chờ
        logger.warning("Không tạo được nhận xét, gửi báo cáo không có nhận xét", exc_info=True)
        return None
    raw = (raw or "").strip()
    if not raw:
        return None
    if not _numbers_ok(raw, report_text):
        logger.warning("Nhận xét chứa số không có trong báo cáo, bỏ qua nhận xét")
        return None
    return "💬 Nhận xét\n" + raw


# ---------------------------------------------------------------- báo cáo hoàn chỉnh

@dataclass(frozen=True)
class BuiltReport:
    text: str
    period: Period
    ok: tuple
    failed: tuple
    denied: tuple
    commentary: bool = False  # True nếu báo cáo có kèm nhận xét


async def build_report(
    pool,
    role: str,
    sections,
    today: Optional[date] = None,
    commentary: Optional[Callable[[str], Awaitable[str]]] = None,
    commentary_timeout: float = 20.0,
) -> BuiltReport:
    """Dựng báo cáo ngày hôm qua cho `role`, gồm các mục trong `sections`.

    - commentary: hàm async nhận chữ báo cáo, trả 2-3 dòng nhận xét (reports/commentary.py). None = không nhận xét.
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
            checked = _prepare(section.sql_file)
        except (OSError, UnsafeSQLError):
            logger.exception("File SQL của mục '%s' không đọc được hoặc không an toàn", key)
            failed.append(key)
            continue
        if any(not can_access(role, t) for t in checked.tables):
            logger.warning("Mục '%s' vượt quyền của role '%s', bỏ qua", key, role)
            denied.append(key)
            continue
        try:
            lines = await _run_section(pool, checked.sql, section, period)
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
    note = await _commentary_block(commentary, text, commentary_timeout) if commentary else None
    if note:
        text += "\n\n" + note
    return BuiltReport(text, period, tuple(ok), tuple(failed), tuple(denied), commentary=bool(note))


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