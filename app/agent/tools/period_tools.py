# app/agent/tools/period_tools.py
# Tool get_date_range: đổi cụm thời gian tiếng Việt ("hôm qua", "tuần này", "ngày 3/11",
# "từ 1/11 đến 5/11"...) thành khoảng ngày cụ thể theo giờ Việt Nam (UTC+7), kèm điều kiện
# SQL và kỳ so sánh. Logic nằm ở app/agent/periods.py; tool chỉ định dạng kết quả thành
# chữ cho LLM. Lỗi trả về dạng chữ để LLM tự hỏi lại người dùng, không làm sập lượt chạy.

from langchain_core.tools import tool

from app.agent.periods import (
    Period,
    PeriodError,
    comparison_periods,
    resolve_period,
    today_local,
    weekday_name,
)


def _sql_range(p: Period) -> str:
    return f">= '{p.start.isoformat()}' AND < '{p.end_exclusive.isoformat()}'"


@tool
def get_date_range(period_text: str) -> str:
    """Đổi MỘT cụm thời gian thành khoảng ngày cụ thể (giờ Việt Nam, UTC+7).

    LUÔN gọi tool này trước khi viết SQL có lọc theo thời gian; không tự tính ngày.
    Mỗi lần chỉ truyền MỘT kỳ (muốn so sánh nhiều kỳ thì gọi nhiều lần, hoặc dùng
    luôn "Kỳ so sánh" trong kết quả).

    Hiểu được: hôm nay, hôm qua, hôm kia, tuần này/trước, tháng này/trước, quý này/trước,
    năm nay/ngoái, tháng 10, tháng 10/2025, quý 3, quý 3/2025, năm 2025, 7 ngày qua,
    3 tuần qua, 3 tháng qua, ngày 3/11, 3/11/2025, 15 tháng 10, "từ 1/11 đến 5/11".
    """
    today = today_local()
    try:
        p = resolve_period(period_text, today)
        comparisons = comparison_periods(p)
    except PeriodError as e:
        return f"Không xác định được khoảng thời gian: {e}"

    lines = [
        f"Hôm nay: {weekday_name(today)}, {today.strftime('%d/%m/%Y')} (UTC+7).",
        f"Kỳ: {p.label}",
        f"Từ ngày: {p.start.isoformat()} (gồm)",
        f"Đến ngày: {p.end.isoformat()} (gồm)",
        f"Số ngày: {p.days}",
        f"Điều kiện SQL cho cột ngày: cột_ngày {_sql_range(p)}",
        "Nếu cột là timestamptz hoặc lưu giờ UTC, đổi sang giờ Việt Nam rồi mới lọc theo ngày.",
    ]
    if p.partial:
        lines.append(
            "Lưu ý: kỳ này gồm cả hôm nay nên số liệu CHƯA ĐỦ NGÀY, "
            "phải nói rõ điều này khi trả lời."
        )
    lines.append("Kỳ so sánh (cùng độ dài, để so công bằng):")
    for name, cp in comparisons:
        lines.append(f"- {name}: {cp.label} | từ {cp.start.isoformat()} đến {cp.end.isoformat()} "
                     f"| SQL: cột_ngày {_sql_range(cp)}")
    return "\n".join(lines)