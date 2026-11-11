# app/agent/tools/context_tools.py
# Tool lấy tri thức nghiệp vụ cho agent (KHÔNG chạm database):
#   get_business_knowledge : bảng thuật ngữ / bối cảnh doanh nghiệp (knowledge/*.md)
#   get_period_events      : ngày lễ, khuyến mãi, thay đổi vận hành trong một kỳ (knowledge/calendar.yaml)
#   get_business_targets   : mục tiêu của một kỳ (config/targets.yaml, chia đều theo ngày)
# Lỗi trả về dạng chữ để LLM tự xử lý, không làm sập lượt chạy.

from typing import Literal

from langchain_core.tools import tool

from app.agent import knowledge
from app.agent.business_config import load_targets
from app.agent.periods import PeriodError, resolve_period
from app.agent.tools._queries import KPI_METRICS, METRIC_LABELS, fmt_metric


@tool
def get_business_knowledge(topic: Literal["glossary", "business_context"]) -> str:
    """Lấy tài liệu nghiệp vụ của doanh nghiệp.

    topic = "glossary": định nghĩa chỉ số (doanh thu, số đơn, AOV, khách mới/quay lại, kênh bán...).
    topic = "business_context": nguồn dữ liệu, bảng chính, quy ước thời gian, cách đọc biến động.
    Gọi khi cần chắc chắn cách tính một chỉ số trước khi trả lời hoặc giải thích số liệu.
    """
    text = knowledge.glossary() if topic == "glossary" else knowledge.business_context()
    return text or "Chưa có tài liệu cho mục này."


@tool
def get_period_events(period_text: str) -> str:
    """Liệt kê ngày lễ, khuyến mãi, thay đổi vận hành rơi vào MỘT kỳ và kỳ liền trước (kỳ so sánh).

    Gọi trước khi giải thích vì sao doanh số tăng/giảm. period_text giữ nguyên cụm người dùng nói
    ("tuần này", "tháng 10/2026", "7 ngày qua"); không tự tính ngày.
    """
    try:
        p = resolve_period(period_text)
    except PeriodError as e:
        return f"Không xác định được khoảng thời gian: {e}"
    text = knowledge.events_text(p.start, p.end)
    if text is None:
        return (f"Không có sự kiện đặc biệt nào trong lịch cho {p.label} và kỳ so sánh. "
                "Lịch có thể chưa đầy đủ, nên không khẳng định là kỳ này không có sự kiện.")
    return text


@tool
def get_business_targets(period_text: str) -> str:
    """Mục tiêu kinh doanh của MỘT kỳ (doanh thu, số đơn, giá trị đơn TB, khách mới...).

    Mục tiêu tháng được chia đều theo ngày cho kỳ ngắn hơn một tháng. Chỉ số nào chưa đặt mục tiêu thì
    báo "chưa đặt mục tiêu": KHÔNG được tự đoán hay bịa mục tiêu.
    """
    try:
        p = resolve_period(period_text)
    except PeriodError as e:
        return f"Không xác định được khoảng thời gian: {e}"

    targets = load_targets()
    if not targets.has_any():
        return "Chưa đặt mục tiêu nào (config/targets.yaml còn trống). Không có số liệu mục tiêu để so sánh."

    lines = [f"Mục tiêu cho {p.label}:"]
    for metric in KPI_METRICS:
        value = targets.for_period(metric, p.start, p.end)
        lines.append(f"• {METRIC_LABELS[metric]}: "
                     + (fmt_metric(metric, value) if value is not None else "chưa đặt mục tiêu (hoặc không tính được cho kỳ này)"))
    if p.partial:
        lines.append("Lưu ý: kỳ gồm cả hôm nay; mục tiêu ở trên chỉ tính tới ngày cuối của kỳ (theo tiến độ), "
                     "không phải mục tiêu cả tháng.")
    return "\n".join(lines)