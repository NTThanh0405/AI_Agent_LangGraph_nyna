# app/agent/tools/snapshot_tools.py
# business_snapshot: ảnh chụp nhanh KPI của một kỳ = số liệu + % thay đổi so với kỳ trước
# + so với mục tiêu (config/targets.yaml) + cờ vượt ngưỡng (config/thresholds.yaml).
#
# Số liệu lấy từ các file SQL cố định (kpi_summary.sql, customers_new_vs_returning.sql) nên cùng định
# nghĩa với báo cáo sáng. Tool CHỈ gắn cờ, không gửi cảnh báo. Ngưỡng/mục tiêu chưa đặt (null) thì
# bỏ qua phần đó và nói rõ, không tự đặt giá trị.

import logging
from typing import Optional

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

from app.agent.business_config import Flag, Targets, Thresholds, load_targets, load_thresholds
from app.agent.periods import Period, PeriodError, comparison_periods, resolve_period
from app.agent.permissions import PermissionDenied
from app.agent.tools._queries import (
    KPI_METRICS,
    METRIC_LABELS,
    Kpis,
    collect_kpis,
    fmt_delta,
    fmt_metric,
    partial_note,
    pct_change,
    vn_num,
)
from app.agent.tools.sql_tools import _caller

logger = logging.getLogger(__name__)

_FLAG_TEXT = {"warn": "[CẢNH BÁO]", "critical": "[NGHIÊM TRỌNG]"}


def _flag_text(flag: Flag, what: str) -> Optional[str]:
    if flag.level in _FLAG_TEXT:
        return f"{_FLAG_TEXT[flag.level]} {what}"
    return None


def build_snapshot(
    current: Kpis,
    previous: list[tuple[str, Kpis]],
    targets: Targets,
    thresholds: Thresholds,
) -> str:
    """Dựng chữ cho LLM. Hàm thuần (không đụng DB) để test được.

    `previous`: danh sách (tên kỳ so sánh, KPI của kỳ đó). Cờ vượt ngưỡng chỉ tính theo kỳ so sánh ĐẦU TIÊN
    (kỳ liền trước cùng độ dài); các kỳ còn lại chỉ hiển thị % thay đổi.
    """
    p = current.period
    lines = [f"Tóm tắt: {p.label}"]
    if (note := partial_note(p)):
        lines.append(note + " Phải nói rõ khi trả lời; đừng kết luận 'giảm' chỉ vì kỳ chưa đủ ngày.")
    for name, k in previous:
        lines.append(f"So sánh với {name}: {k.period.label}")

    flags: list[str] = []
    for metric in KPI_METRICS:
        value = current.get(metric)
        parts = [f"• {METRIC_LABELS[metric]}: {fmt_metric(metric, value)}"]

        for i, (name, k) in enumerate(previous):
            prev_val = k.get(metric)
            pct = pct_change(value, prev_val)
            parts.append(f"{fmt_delta(pct)} so với {name} ({fmt_metric(metric, prev_val)})")
            if i == 0:
                flag = thresholds.change_flag(metric, value, prev_val, partial=p.partial)
                if (t := _flag_text(flag, f"{METRIC_LABELS[metric]} {fmt_delta(flag.pct)} so với {name}")):
                    parts.append(t)
                    flags.append(t)
                elif flag.level is None and flag.note and metric in thresholds.change:
                    parts.append(f"(không gắn cờ: {flag.note})")

        target = targets.for_period(metric, p.start, p.end)
        if target is not None:
            ach = value / target * 100 if (value is not None and target > 0) else None
            seg = f"Mục tiêu {fmt_metric(metric, target)}"
            if ach is not None:
                seg += f", đạt {vn_num(ach, 1)}%"
            parts.append(seg)
            aflag = thresholds.achievement_flag(metric, ach)
            if (t := _flag_text(aflag, f"{METRIC_LABELS[metric]} mới đạt {vn_num(aflag.pct or 0, 1)}% mục tiêu")):
                parts.append(t)
                flags.append(t)
        lines.append(" | ".join(parts))

    if not thresholds.has_any():
        lines.append("Chưa đặt ngưỡng cảnh báo (config/thresholds.yaml còn trống) nên không có cờ.")
    elif flags:
        lines.append("Cờ vượt ngưỡng: " + "; ".join(flags))
    else:
        lines.append("Không có chỉ số nào vượt ngưỡng đã đặt.")
    if not targets.has_any():
        lines.append("Chưa đặt mục tiêu (config/targets.yaml còn trống) nên không so với mục tiêu.")

    errors = list(current.errors)
    for _, k in previous:
        errors += [e for e in k.errors if e not in errors]
    if errors:
        lines.append("Lỗi một phần: " + "; ".join(errors) + ". Các chỉ số tương ứng bị thiếu.")
    return "\n".join(lines)


@tool
async def business_snapshot(period_text: str, config: RunnableConfig) -> str:
    """Ảnh chụp nhanh các chỉ số chính của MỘT kỳ: doanh thu, số đơn, giá trị đơn TB, số khách, khách mới,
    khách quay lại; kèm % thay đổi so với kỳ trước, tỷ lệ đạt mục tiêu và cờ vượt ngưỡng cảnh báo.

    Dùng khi người dùng hỏi tình hình chung ("tuần này thế nào", "có gì bất thường không", "đạt mục tiêu chưa").
    period_text giữ nguyên cụm người dùng nói ("hôm qua", "tuần này", "tháng 10/2026"); không tự tính ngày.
    Chỉ số có "[CẢNH BÁO]" / "[NGHIÊM TRỌNG]" là đã vượt ngưỡng do chủ doanh nghiệp đặt.
    """
    user_id, role = _caller(config)
    try:
        p: Period = resolve_period(period_text)
        comparisons = comparison_periods(p)
    except PeriodError as e:
        return f"Không xác định được khoảng thời gian: {e}"

    try:
        current = await collect_kpis(p, role=role, user_id=user_id)
        previous = [(name, await collect_kpis(cp, role=role, user_id=user_id)) for name, cp in comparisons]
    except PermissionDenied as e:
        return f"Không có quyền xem số liệu này: {e}"

    if not any(v is not None for v in current.values.values()):
        detail = ("; ".join(current.errors)) or "không có dòng dữ liệu"
        return f"Không lấy được số liệu cho {p.label} ({detail})."

    return build_snapshot(current, previous, load_targets(), load_thresholds())