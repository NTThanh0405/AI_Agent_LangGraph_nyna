# app/agent/tools/analysis_tools.py
# Ba tool phân tích dựa trên các file SQL cố định trong app/agent/queries/ (cùng định nghĩa số liệu với
# báo cáo sáng, không để LLM tự viết SQL):
#   compare_periods : so KPI của hai kỳ bất kỳ
#   analyze_trend   : xu hướng doanh thu theo ngày trong một kỳ
#   explain_change  : vì sao một kỳ tăng/giảm so với kỳ trước (phân rã theo số đơn/giá trị đơn, kênh, sản phẩm, sự kiện)
# Tool chỉ đưa DỮ KIỆN. Nguyên nhân chỉ là giả thuyết nếu dữ liệu chưa chứng minh (quy tắc trong business_context.md).

import logging
from datetime import date, timedelta
from typing import Optional

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

from app.agent import knowledge
from app.agent.periods import Period, PeriodError, comparison_periods, resolve_period, today_local
from app.agent.permissions import PermissionDenied
from app.agent.tools._queries import (
    KPI_METRICS,
    METRIC_LABELS,
    Kpis,
    QueryFailed,
    collect_kpis,
    fmt_delta,
    fmt_metric,
    partial_note,
    pct_change,
    run_query_file,
    vn_num,
)
from app.agent.tools.sql_tools import _caller

logger = logging.getLogger(__name__)

TREND_BAND_PCT = 5.0   # nửa sau so với nửa đầu lệch quá ±5% mới gọi là tăng/giảm, còn lại là đi ngang
MIN_TREND_DAYS = 4
TOP_CONTRIBUTORS = 5
_CUMULATIVE = ("revenue", "orders")  # chỉ số tổng, cần chia theo ngày khi hai kỳ dài khác nhau


def _signed_money(n: float) -> str:
    return f"{'+' if n >= 0 else '-'}{vn_num(abs(n))} ₫"


# ============================================================================= so sánh hai kỳ

def build_comparison(a: Kpis, b: Kpis) -> str:
    pa, pb = a.period, b.period
    lines = [f"Kỳ A: {pa.label}", f"Kỳ B: {pb.label}"]
    for p, name in ((pa, "A"), (pb, "B")):
        if (n := partial_note(p)):
            lines.append(f"Kỳ {name}: {n}")
    if pa.days != pb.days:
        lines.append(f"Lưu ý: hai kỳ dài khác nhau ({pa.days} ngày so với {pb.days} ngày), nên tổng không so sánh "
                     "công bằng; xem dòng 'trung bình mỗi ngày' bên dưới.")
    for metric in KPI_METRICS:
        va, vb = a.get(metric), b.get(metric)
        lines.append(f"• {METRIC_LABELS[metric]}: A = {fmt_metric(metric, va)} | B = {fmt_metric(metric, vb)} | "
                     f"A so với B: {fmt_delta(pct_change(va, vb))}")
    if pa.days != pb.days:
        for metric in _CUMULATIVE:
            va, vb = a.get(metric), b.get(metric)
            if va is not None and vb is not None:
                da, db = va / pa.days, vb / pb.days
                lines.append(f"• {METRIC_LABELS[metric]} trung bình mỗi ngày: A = {fmt_metric(metric, da)} | "
                             f"B = {fmt_metric(metric, db)} | A so với B: {fmt_delta(pct_change(da, db))}")
    errors = list(dict.fromkeys(a.errors + b.errors))
    if errors:
        lines.append("Lỗi một phần: " + "; ".join(errors) + ". Các chỉ số tương ứng bị thiếu.")
    return "\n".join(lines)


@tool
async def compare_periods(period_a: str, period_b: str, config: RunnableConfig) -> str:
    """So sánh các chỉ số chính (doanh thu, số đơn, giá trị đơn TB, số khách, khách mới, khách quay lại)
    giữa HAI kỳ bất kỳ: kết quả là kỳ A so với kỳ B.

    Dùng khi người dùng tự chọn hai kỳ ("tháng 10 so với tháng 9", "tuần này so với cùng kỳ..."). Nếu chỉ hỏi
    "so với kỳ trước" thì dùng business_snapshot hoặc explain_change. Truyền cụm thời gian nguyên văn, không tự tính ngày.
    """
    user_id, role = _caller(config)
    try:
        pa, pb = resolve_period(period_a), resolve_period(period_b)
    except PeriodError as e:
        return f"Không xác định được khoảng thời gian: {e}"
    try:
        a = await collect_kpis(pa, role=role, user_id=user_id)
        b = await collect_kpis(pb, role=role, user_id=user_id)
    except PermissionDenied as e:
        return f"Không có quyền xem số liệu này: {e}"
    if not any(v is not None for v in (*a.values.values(), *b.values.values())):
        return "Không lấy được số liệu cho hai kỳ này: " + ("; ".join(a.errors + b.errors) or "không có dữ liệu") + "."
    return build_comparison(a, b)


# ============================================================================= xu hướng theo ngày

def summarize_trend(period: Period, rows: list[dict], today: Optional[date] = None) -> str:
    """Tóm tắt xu hướng doanh thu theo ngày. Hàm thuần để test được.

    Ngày không có dòng nào trong dữ liệu tính là doanh thu 0 (và được nêu rõ). Kỳ chưa kết thúc thì bỏ
    ngày hôm nay (chưa đủ ngày) để không làm lệch xu hướng.
    """
    today = today or today_local()
    last = period.end
    notes = []
    if period.partial and last >= today:
        last = today - timedelta(days=1)
        notes.append(f"Đã bỏ ngày hôm nay ({today.strftime('%d/%m/%Y')}) vì chưa đủ ngày.")
    if last < period.start:
        return f"Kỳ {period.label} chưa có ngày nào đủ số liệu để phân tích xu hướng."

    by_day = {r["day"]: (float(r.get("revenue") or 0), float(r.get("orders") or 0))
              for r in rows if r.get("day") is not None}
    days = [period.start + timedelta(days=i) for i in range((last - period.start).days + 1)]
    revenue = [by_day.get(d, (0.0, 0.0))[0] for d in days]
    orders = [by_day.get(d, (0.0, 0.0))[1] for d in days]
    missing = [d for d in days if d not in by_day]
    n = len(days)

    lines = [f"Xu hướng doanh thu theo ngày: {period.label} ({n} ngày phân tích)"]
    lines += notes
    total = sum(revenue)
    lines.append(f"• Tổng doanh thu: {vn_num(total)} ₫ | trung bình mỗi ngày: {vn_num(total / n)} ₫ | "
                 f"tổng số đơn: {vn_num(sum(orders))}")
    hi = max(range(n), key=lambda i: revenue[i])
    lo = min(range(n), key=lambda i: revenue[i])
    lines.append(f"• Ngày cao nhất: {days[hi].strftime('%d/%m/%Y')} ({vn_num(revenue[hi])} ₫)")
    lines.append(f"• Ngày thấp nhất: {days[lo].strftime('%d/%m/%Y')} ({vn_num(revenue[lo])} ₫)")
    if missing:
        shown = ", ".join(d.strftime("%d/%m") for d in missing[:5]) + ("..." if len(missing) > 5 else "")
        lines.append(f"• {len(missing)} ngày không có doanh thu trong dữ liệu ({shown}), đã tính là 0. "
                     "Có thể là ngày nghỉ hoặc dữ liệu chưa về, cần kiểm tra trước khi kết luận.")

    if n < MIN_TREND_DAYS:
        lines.append(f"• Chỉ có {n} ngày (< {MIN_TREND_DAYS}) nên chưa đủ để nói về xu hướng.")
    else:
        half = n // 2
        first, second = sum(revenue[:half]) / half, sum(revenue[n - half:]) / half
        pct = pct_change(second, first)
        if pct is None:
            label = "chưa đánh giá được (nửa đầu bằng 0)"
        elif pct > TREND_BAND_PCT:
            label = "xu hướng TĂNG"
        elif pct < -TREND_BAND_PCT:
            label = "xu hướng GIẢM"
        else:
            label = "đi ngang"
        lines.append(f"• Nửa sau so với nửa đầu kỳ (trung bình mỗi ngày): {fmt_delta(pct)} → {label}.")
    return "\n".join(lines)


@tool
async def analyze_trend(period_text: str, config: RunnableConfig) -> str:
    """Xu hướng doanh thu THEO NGÀY trong một kỳ: tổng, trung bình mỗi ngày, ngày cao/thấp nhất, và nửa sau kỳ
    tăng hay giảm so với nửa đầu. Dùng cho câu hỏi "xu hướng", "đang tăng hay giảm", "diễn biến theo ngày".

    Cần kỳ dài từ 4 ngày trở lên (ví dụ "7 ngày qua", "tháng này", "tháng 10/2026").
    """
    user_id, role = _caller(config)
    try:
        p = resolve_period(period_text)
    except PeriodError as e:
        return f"Không xác định được khoảng thời gian: {e}"
    try:
        rows = await run_query_file("revenue_by_day.sql", p, role=role, user_id=user_id)
    except PermissionDenied as e:
        return f"Không có quyền xem số liệu này: {e}"
    except QueryFailed as e:
        return f"Không lấy được số liệu theo ngày: {e}."
    if not rows:
        return f"Không có doanh thu nào trong {p.label}."
    return summarize_trend(p, rows)


# ============================================================================= giải thích biến động

def breakdown(cur_rows: list[dict], prev_rows: list[dict], key: str, value: str = "revenue") -> list[dict]:
    """Gộp hai kỳ theo `key`, tính chênh lệch; sắp theo |chênh lệch| giảm dần.
    Mỗi phần tử: name, cur, prev (None nếu không xuất hiện ở kỳ đó), delta (coi phần vắng là 0)."""
    cur = {str(r.get(key) or "(không rõ)"): float(r.get(value) or 0) for r in cur_rows}
    prev = {str(r.get(key) or "(không rõ)"): float(r.get(value) or 0) for r in prev_rows}
    out = []
    for name in cur.keys() | prev.keys():
        c, p = cur.get(name), prev.get(name)
        out.append({"name": name, "cur": c, "prev": p, "delta": (c or 0.0) - (p or 0.0)})
    out.sort(key=lambda x: (-abs(x["delta"]), x["name"]))
    return out


def decompose_revenue(cur: Kpis, prev: Kpis) -> Optional[str]:
    """Doanh thu = số đơn × giá trị đơn TB. Chênh lệch = (Δđơn × AOV kỳ trước) + (đơn kỳ này × ΔAOV): chính xác, không dư."""
    r1, r0 = cur.get("revenue"), prev.get("revenue")
    o1, o0 = cur.get("orders"), prev.get("orders")
    if None in (r1, r0, o1, o0) or not o0 or not o1:
        return None
    a1, a0 = r1 / o1, r0 / o0
    d_orders, d_aov = (o1 - o0) * a0, o1 * (a1 - a0)
    return (f"• Doanh thu {_signed_money(r1 - r0)}: do số đơn {_signed_money(d_orders)} "
            f"({vn_num(o0)} → {vn_num(o1)} đơn) và do giá trị đơn TB {_signed_money(d_aov)} "
            f"({vn_num(a0)} → {vn_num(a1)} ₫)")


def _contrib_lines(items: list[dict], total_delta: Optional[float], absent_text: str, top: int) -> list[str]:
    lines = []
    for it in items[:top]:
        c = f"{vn_num(it['cur'])} ₫" if it["cur"] is not None else absent_text
        p = f"{vn_num(it['prev'])} ₫" if it["prev"] is not None else absent_text
        share = ""
        if total_delta:
            share = f" (≈ {vn_num(it['delta'] / total_delta * 100, 0)}% tổng chênh lệch)"
        lines.append(f"  - {it['name']}: {p} → {c} | {_signed_money(it['delta'])}{share}")
    return lines


def build_explanation(
    cur: Kpis, prev: Kpis, prev_name: str,
    channels: Optional[list[dict]], products: Optional[list[dict]],
    events: Optional[str], errors: list[str],
) -> str:
    p = cur.period
    lines = [f"Phân tích biến động: {p.label} so với {prev_name} ({prev.period.label})"]
    if (n := partial_note(p)):
        lines.append(n + " Chưa nên kết luận giảm/tăng khi kỳ chưa đủ ngày.")
    lines.append("Chỉ số chính:")
    for metric in KPI_METRICS:
        v, pv = cur.get(metric), prev.get(metric)
        lines.append(f"• {METRIC_LABELS[metric]}: {fmt_metric(metric, v)} (kỳ trước {fmt_metric(metric, pv)}, "
                     f"{fmt_delta(pct_change(v, pv))})")
    if (d := decompose_revenue(cur, prev)):
        lines.append("Doanh thu thay đổi vì:")
        lines.append(d)

    total_delta = None
    if cur.get("revenue") is not None and prev.get("revenue") is not None:
        total_delta = cur.get("revenue") - prev.get("revenue")

    if channels:
        lines.append(f"Theo kênh bán (đóng góp lớn nhất vào chênh lệch, tối đa {TOP_CONTRIBUTORS}):")
        lines += _contrib_lines(channels, total_delta, "không có doanh thu", TOP_CONTRIBUTORS)
    if products:
        lines.append(f"Theo sản phẩm (chỉ xét top 20 doanh thu của mỗi kỳ; 'ngoài top 20' = không biết số cụ thể, "
                     f"chênh lệch chỉ là ước lượng; tối đa {TOP_CONTRIBUTORS}):")
        lines += _contrib_lines(products, None, "ngoài top 20", TOP_CONTRIBUTORS)
    lines.append(events or "Lịch sự kiện: không có ngày lễ/khuyến mãi nào được ghi cho kỳ này và kỳ so sánh "
                           "(lịch có thể chưa đầy đủ).")
    if errors:
        lines.append("Lỗi một phần: " + "; ".join(dict.fromkeys(errors)) + ". Phần tương ứng bị thiếu.")
    lines.append("Khi trả lời: chỉ nêu nguyên nhân có bằng chứng ở trên (kênh/sản phẩm/số đơn/giá trị đơn/sự kiện); "
                 "phần còn lại nói rõ là giả thuyết.")
    return "\n".join(lines)


@tool
async def explain_change(period_text: str, config: RunnableConfig) -> str:
    """Tìm lý do doanh số của một kỳ tăng hoặc giảm so với kỳ liền trước cùng độ dài.

    Trả về: chỉ số chính + mức thay đổi; doanh thu đổi do SỐ ĐƠN hay do GIÁ TRỊ ĐƠN; kênh bán và sản phẩm đóng
    góp nhiều nhất vào chênh lệch; ngày lễ/khuyến mãi trong kỳ. Dùng cho "vì sao tăng/giảm", "do đâu".
    period_text giữ nguyên cụm người dùng nói; không tự tính ngày.
    """
    user_id, role = _caller(config)
    try:
        p = resolve_period(period_text)
        prev_name, prev_p = comparison_periods(p)[0]
    except PeriodError as e:
        return f"Không xác định được khoảng thời gian: {e}"

    errors: list[str] = []

    async def rows_of(file: str, period: Period) -> Optional[list[dict]]:
        try:
            return await run_query_file(file, period, role=role, user_id=user_id)
        except QueryFailed as e:
            errors.append(str(e))
            return None

    try:
        cur = await collect_kpis(p, role=role, user_id=user_id)
        prev = await collect_kpis(prev_p, role=role, user_id=user_id)
        ch_cur, ch_prev = await rows_of("revenue_by_channel.sql", p), await rows_of("revenue_by_channel.sql", prev_p)
        pr_cur, pr_prev = await rows_of("top_products.sql", p), await rows_of("top_products.sql", prev_p)
    except PermissionDenied as e:
        return f"Không có quyền xem số liệu này: {e}"

    if not any(v is not None for v in cur.values.values()):
        return f"Không lấy được số liệu cho {p.label}: " + ("; ".join(cur.errors + errors) or "không có dữ liệu") + "."

    channels = breakdown(ch_cur, ch_prev, "channel") if ch_cur is not None and ch_prev is not None else None
    products = breakdown(pr_cur, pr_prev, "product") if pr_cur is not None and pr_prev is not None else None
    events = knowledge.events_text(p.start, p.end)
    return build_explanation(cur, prev, prev_name, channels, products, events,
                             errors + cur.errors + prev.errors)