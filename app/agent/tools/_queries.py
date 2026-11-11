# app/agent/tools/_queries.py
# Phần dùng chung của snapshot_tools / analysis_tools / context_tools:
#   - chạy các file SQL viết sẵn trong app/agent/queries/ (qua guard.check_template + kiểm tra quyền),
#   - gom KPI của một kỳ,
#   - định dạng số kiểu Việt Nam, tính % thay đổi.
# Không import app/reports (hướng phụ thuộc: reports import từ agent, không ngược lại).
# Tên file bắt đầu bằng "_" nên không phải tool; không đăng ký vào TOOLS_BY_AGENT.

import logging
from dataclasses import dataclass, field
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

import psycopg

from app.agent.periods import Period
from app.agent.permissions import ensure_access_user
from app.db.guard import CheckedQuery, check_template
from app.db.pool import get_pool

logger = logging.getLogger(__name__)

QUERY_DIR = Path(__file__).resolve().parents[1] / "queries"
PARAM_NAMES = frozenset({"start", "end", "end_exclusive"})
MAX_FETCH_ROWS = 200


class QueryFailed(Exception):
    """Một file SQL chạy lỗi (không phải lỗi quyền). Thông điệp an toàn để đưa cho LLM."""


# ----------------------------------------------------------------------------- chạy SQL

@lru_cache(maxsize=32)
def _prepare(file: str) -> CheckedQuery:
    raw = (QUERY_DIR / file).read_text(encoding="utf-8-sig").strip().rstrip(";")
    return check_template(raw, allowed_params=PARAM_NAMES)


def _clean(v: Any) -> Any:
    return float(v) if isinstance(v, Decimal) else v


async def run_query_file(file: str, period: Period, *, role: Optional[str], user_id: Optional[str]) -> list[dict]:
    """Chạy một file trong queries/ cho `period`; trả list dict (cột -> giá trị, Decimal đổi sang float).

    Raise PermissionDenied nếu người hỏi không được xem bảng mà file dùng (để tool báo rõ).
    Raise QueryFailed với các lỗi còn lại.
    """
    try:
        checked = _prepare(file)
    except Exception as e:  # file thiếu / SQL không qua guard
        logger.warning("không nạp được %s: %s", file, e)
        raise QueryFailed(f"không nạp được truy vấn {file}") from e

    await ensure_access_user(role, user_id, checked.tables)

    params = {"start": period.start, "end": period.end, "end_exclusive": period.end_exclusive}
    try:
        async with get_pool().connection() as conn:
            cur = await conn.execute(checked.sql, params)
            rows = await cur.fetchmany(MAX_FETCH_ROWS)
            columns = [c.name for c in cur.description]
    except psycopg.errors.QueryCanceled as e:
        raise QueryFailed(f"{file} chạy quá lâu nên bị huỷ") from e
    except psycopg.Error as e:
        logger.warning("%s lỗi: %s", file, e)
        raise QueryFailed(f"{file} lỗi khi chạy: {(str(e).splitlines() or [''])[0]}") from e

    out = []
    for r in rows:
        values = list(r.values()) if isinstance(r, dict) else list(r)
        out.append({c: _clean(v) for c, v in zip(columns, values)})
    return out


# ----------------------------------------------------------------------------- KPI một kỳ

KPI_METRICS = ("revenue", "orders", "aov", "customers", "new_customers", "returning_customers")

METRIC_LABELS = {
    "revenue": "Doanh thu", "orders": "Số đơn", "aov": "Giá trị đơn trung bình",
    "customers": "Số khách", "new_customers": "Khách mới", "returning_customers": "Khách quay lại",
}
_MONEY = {"revenue", "aov"}


@dataclass
class Kpis:
    period: Period
    values: dict[str, Optional[float]] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def get(self, metric: str) -> Optional[float]:
        return self.values.get(metric)


async def collect_kpis(period: Period, *, role: Optional[str], user_id: Optional[str]) -> Kpis:
    """KPI của một kỳ từ kpi_summary.sql + customers_new_vs_returning.sql.
    Một file lỗi thì chỉ thiếu các chỉ số của file đó (ghi vào .errors). PermissionDenied được ném ra ngoài."""
    kpis = Kpis(period)
    for file, cols in (
        ("kpi_summary.sql", ("revenue", "orders", "aov", "customers")),
        ("customers_new_vs_returning.sql", ("new_customers", "returning_customers")),
    ):
        try:
            rows = await run_query_file(file, period, role=role, user_id=user_id)
        except QueryFailed as e:
            kpis.errors.append(str(e))
            continue
        row = rows[0] if rows else {}
        for c in cols:
            kpis.values[c] = row.get(c)
    return kpis


# ----------------------------------------------------------------------------- định dạng

def vn_num(n: float, decimals: int = 0) -> str:
    """1234567.8 -> '1.234.568' (chấm hàng nghìn, phẩy thập phân)."""
    s = f"{n:,.{decimals}f}"
    return s.replace(",", "\0").replace(".", ",").replace("\0", ".")


def fmt_metric(metric: str, value: Optional[float]) -> str:
    if value is None:
        return "không có số liệu"
    if metric in _MONEY:
        return f"{vn_num(value)} ₫"
    return vn_num(value) if float(value).is_integer() else vn_num(value, 1)


def pct_change(current: Optional[float], previous: Optional[float]) -> Optional[float]:
    """% thay đổi; None nếu thiếu số hoặc kỳ trước bằng 0 (không chia cho 0)."""
    if current is None or previous is None or previous == 0:
        return None
    return (current - previous) / abs(previous) * 100


def fmt_delta(pct: Optional[float]) -> str:
    if pct is None:
        return "không tính được % thay đổi"
    if round(pct, 1) == 0:
        return "≈ 0%"
    return f"{'▲' if pct > 0 else '▼'} {vn_num(abs(pct), 1)}%"


def partial_note(p: Period) -> Optional[str]:
    return "Kỳ này gồm cả hôm nay nên số liệu CHƯA ĐỦ NGÀY." if p.partial else None