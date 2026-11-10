# app/agent/nodes/router_rules.py
# Luật cứng đường tắt, KHÔNG gọi LLM: câu quen thuộc được xử lý ngay.
#   "báo cáo hôm nay/hôm qua/sáng"  -> dựng đúng báo cáo như bản gửi 7h sáng (reports/builder.py)
#   chào hỏi / cảm ơn              -> đi thẳng tới câu trả lời, bỏ qua bước phân tích
# builder.py được import TRONG hàm để agent không import reports ở cấp module (tránh vòng import).

import logging
import re
import unicodedata
from typing import Optional

logger = logging.getLogger(__name__)

# Mục dùng khi người hỏi không có cấu hình riêng trong reports.yaml
DEFAULT_REPORT_SECTIONS = ("kpi_summary", "revenue_by_channel", "top_products")

_REPORT_RE = re.compile(
    r"^(cho (minh|toi|em) )?(xem |gui )?bao cao( kinh doanh)?( sang)?( hom (nay|qua))?( di| nhe)?$"
)
_SMALLTALK_RE = re.compile(
    r"^(xin chao|chao|chao ban|chao bot|hello|hi|hey|alo|cam on|cam on ban|thanks|thank you|ok|oke|okie)$"
)

MSG_NO_REPORT = "Mình chưa dựng được báo cáo lúc này (có thể bạn chưa có quyền xem các mục báo cáo hoặc dữ liệu đang lỗi). Bạn thử lại sau nhé."


def _normalize(text: str) -> str:
    t = unicodedata.normalize("NFD", text.lower())
    t = "".join(c for c in t if unicodedata.category(c) != "Mn").replace("đ", "d")
    t = re.sub(r"[^\w\s]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def match_rule(text: str) -> Optional[str]:
    """Trả "report" | "smalltalk" | None. Chỉ khớp khi CẢ câu là câu quen (câu dài có thêm yêu cầu thì
    đi qua agent như bình thường)."""
    t = _normalize(text or "")
    if not t:
        return None
    if _REPORT_RE.match(t):
        return "report"
    if _SMALLTALK_RE.match(t):
        return "smalltalk"
    return None


async def build_report_text(user_id: str, role: str) -> str:
    """Dựng báo cáo ngày hôm qua cho người hỏi. Mục lấy từ cấu hình riêng của họ trong reports.yaml,
    không có thì dùng mặc định. builder.build_report kiểm tra quyền từng mục theo role."""
    from app.db.pool import get_data_pool
    from app.reports.builder import ReportError, build_report
    from app.reports.subscriptions import get_store

    sections = DEFAULT_REPORT_SECTIONS
    try:
        cfg = get_store().get()
        mgr = cfg.managers.get(int(user_id)) if cfg else None
        if mgr and mgr.sections:
            sections = tuple(mgr.sections)
    except (ValueError, TypeError):
        pass
    try:
        built = await build_report(get_data_pool(role), role, sections)
    except ReportError:
        logger.warning("Đường tắt báo cáo: không dựng được (user_id=%s, role=%s)", user_id, role)
        return MSG_NO_REPORT
    return built.text
