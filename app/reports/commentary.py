# app/reports/commentary.py
# (TÙY CHỌN, bật bằng commentary_enabled trong config/reports.yaml)
# Viết 2-3 dòng nhận xét cho báo cáo sáng bằng ĐÚNG MỘT lần gọi LLM rẻ. Báo cáo vẫn do SQL cố định
# dựng; LLM chỉ đọc số đã tính rồi nhận xét, không được thêm số mới.
#
# Hàm chính: make_commentary(report_text) -> str, được builder.py gọi (có timeout) và kiểm tra lại:
# nhận xét chứa số không có trong báo cáo sẽ bị bỏ. Hàm này cứ để lỗi nổi lên (raise): builder.py
# bắt lỗi và gửi báo cáo không có nhận xét.
#
# Model: mặc định claude-haiku-5-5 (rẻ, nhanh). Muốn đổi, thêm vào .env:
#   COMMENTARY_MODEL=claude-haiku-5-5
# Khoá API dùng ANTHROPIC_API_KEY trong .env (cùng khoá với agent chính).

import logging
import os
from functools import lru_cache
from pathlib import Path

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, SystemMessage

logger = logging.getLogger(__name__)

PROMPT_PATH = Path(__file__).resolve().parents[1] / "agent" / "prompts" / "report_commentary.md"
ENV_PATH = Path(__file__).resolve().parents[2] / ".env"
DEFAULT_MODEL = "claude-haiku-5-5"
MAX_INPUT_CHARS = 6000   # báo cáo dài hơn thì cắt bớt phần cuối trước khi gửi cho model
MAX_OUTPUT_TOKENS = 300
REQUEST_TIMEOUT = 30     # giây; builder.py còn có timeout riêng (mặc định 20 giây) bao ngoài

_FALLBACK_PROMPT = (
    "Bạn viết nhận xét ngắn (2-3 câu tiếng Việt) cho báo cáo kinh doanh. Chỉ dùng các con số có sẵn "
    "trong báo cáo, không thêm số mới, không đoán nguyên nhân, không đưa lời khuyên chắc chắn. "
    "Không dùng markdown hay emoji. Chỉ trả về phần nhận xét."
)


def _ensure_env() -> None:
    """Nếu config.py nạp .env vào Settings thay vì os.environ thì ANTHROPIC_API_KEY có thể chưa có
    trong môi trường. Nạp thêm từ .env (không ghi đè biến đã có). Không có python-dotenv thì bỏ qua."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        return
    try:
        from dotenv import load_dotenv

        load_dotenv(ENV_PATH, override=False)
    except Exception:
        logger.debug("Không nạp thêm được %s", ENV_PATH, exc_info=True)


def _model_name() -> str:
    name = (os.environ.get("COMMENTARY_MODEL") or "").strip() or DEFAULT_MODEL
    return name.split(":", 1)[1] if name.startswith("anthropic:") else name


@lru_cache(maxsize=1)
def _system_prompt() -> str:
    try:
        text = PROMPT_PATH.read_text(encoding="utf-8-sig").strip()
        if text:
            return text
    except OSError:
        logger.warning("Không đọc được %s, dùng prompt mặc định", PROMPT_PATH)
    return _FALLBACK_PROMPT


@lru_cache(maxsize=1)
def _model():
    _ensure_env()
    # temperature=0 để nhận xét ổn định giữa các lần chạy
    return ChatAnthropic(
        model=_model_name(),
        temperature=0,
        max_tokens=MAX_OUTPUT_TOKENS,
        timeout=REQUEST_TIMEOUT,
    )


def _as_text(content) -> str:
    """Nội dung trả về có thể là chuỗi hoặc danh sách khối."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict) and part.get("type") == "text":
                parts.append(str(part.get("text", "")))
        return "".join(parts)
    return str(content or "")


async def make_commentary(report_text: str) -> str:
    """Nhận chữ báo cáo đã dựng, trả 2-3 câu nhận xét (chưa có tiêu đề "Nhận xét", builder.py tự thêm)."""
    body = report_text if len(report_text) <= MAX_INPUT_CHARS else report_text[:MAX_INPUT_CHARS]
    messages = [
        SystemMessage(content=_system_prompt()),
        HumanMessage(content=f"Báo cáo:\n\n{body}\n\nViết nhận xét."),
    ]
    reply = await _model().ainvoke(messages)
    return _as_text(reply.content).strip()