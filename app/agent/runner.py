# app/agent/runner.py
# Chạy MỘT lượt hỏi của agent cho một người dùng: khoá theo thread, kiểm tra phiên hết hạn,
# chạy graph có giới hạn bước và thời gian, rồi trả câu trả lời dạng chữ.
# Telegram hay HTTP, nên bot (hoặc kênh khác sau này) gọi chung một chỗ.
# Lượt lỗi, quá hạn hoặc vượt giới hạn bước thì phiên bị xoá để không để lại hội thoại
# dở dang (tool_use chưa có kết quả) làm hỏng các lượt sau.

import asyncio
import logging
import time
from dataclasses import dataclass

import anthropic
from langgraph.errors import GraphRecursionError

from app.agent.sessions import get_lock, reset, thread_for, touch
from app.config import get_settings

logger = logging.getLogger(__name__)

MSG_BUSY = "Mình đang xử lý câu hỏi trước của bạn, vui lòng đợi xong rồi gửi lại."
MSG_TOO_COMPLEX = "Câu hỏi quá phức tạp, vui lòng chia nhỏ rồi hỏi lại."
MSG_TIMEOUT = "Câu hỏi xử lý quá lâu nên đã bị dừng. Hãy thu hẹp phạm vi hoặc chia nhỏ câu hỏi."
MSG_EMPTY = "Mình chưa tạo được câu trả lời, bạn thử hỏi lại nhé."
MSG_LLM_UNAVAILABLE = "Hệ thống AI đang quá tải hoặc mất kết nối. Bạn thử lại sau ít phút nhé."
MSG_ERROR = "Agent gặp lỗi, vui lòng thử lại sau."

# Mã HTTP của Anthropic nghĩa là "dịch vụ tạm thời không dùng được" (thử lại sau là được)
_LLM_RETRYABLE_STATUS = {429, 500, 502, 503, 504, 529}


@dataclass(frozen=True)
class TurnResult:
    answer: str
    ok: bool = True              # False nếu là thông báo lỗi/bận
    busy: bool = False           # thread đang có lượt khác chạy
    session_reset: bool = False  # phiên cũ vừa hết hạn nên đã bắt đầu hội thoại mới


def extract_text(content) -> str:
    """Lấy phần chữ từ nội dung tin nhắn AI (Claude có thể trả chuỗi hoặc danh sách block)."""
    if isinstance(content, str):
        return content
    return "".join(
        block.get("text", "")
        for block in content
        if isinstance(block, dict) and block.get("type", "text") == "text"
    )


def _llm_unavailable(exc: Exception) -> bool:
    if isinstance(exc, anthropic.APIConnectionError):  # gồm cả APITimeoutError
        return True
    return (
        isinstance(exc, anthropic.APIStatusError)
        and exc.status_code in _LLM_RETRYABLE_STATUS
    )


async def _safe_reset(graph, thread: str) -> None:
    try:
        await reset(graph, thread)
    except Exception:
        logger.exception("Không xoá được phiên %s sau lỗi", thread)


async def run_turn(graph, *, user_id: str, role: str, message: str) -> TurnResult:
    """Chạy một lượt hỏi. Không raise: mọi lỗi được đổi thành TurnResult(ok=False)."""
    settings = get_settings()
    thread = thread_for(user_id)

    lock = get_lock(thread)
    if lock.locked():
        return TurnResult(MSG_BUSY, ok=False, busy=True)

    # role do server tra từ allowlist (app/telegram/auth.py), không lấy từ nội dung tin nhắn.
    config = {
        "configurable": {
            "thread_id": thread,
            "user_id": user_id,
            "user_role": role,
        },
        "recursion_limit": settings.graph_max_steps,
    }
    initial_state = {
        "messages": [("user", message)],
        "user_id": user_id,
        "user_role": role,
    }

    started = time.monotonic()
    async with lock:
        try:
            session_reset = await touch(graph, thread)
            async with asyncio.timeout(settings.invoke_timeout_seconds):
                result = await graph.ainvoke(initial_state, config)
        except GraphRecursionError:
            logger.warning("Vượt giới hạn bước (thread=%s)", thread)
            await _safe_reset(graph, thread)
            return TurnResult(MSG_TOO_COMPLEX, ok=False)
        except TimeoutError:
            logger.warning("Quá thời gian %ss (thread=%s)", settings.invoke_timeout_seconds, thread)
            await _safe_reset(graph, thread)
            return TurnResult(MSG_TIMEOUT, ok=False)
        except Exception as e:
            await _safe_reset(graph, thread)
            if _llm_unavailable(e):
                logger.warning("Anthropic không sẵn sàng (thread=%s): %s", thread, e)
                return TurnResult(MSG_LLM_UNAVAILABLE, ok=False)
            logger.exception("Agent error (thread=%s)", thread)
            return TurnResult(MSG_ERROR, ok=False)

    answer = extract_text(result["messages"][-1].content).strip()
    logger.info("Lượt hỏi xong (user_id=%s, role=%s, %.1fs)", user_id, role, time.monotonic() - started)
    return TurnResult(answer or MSG_EMPTY, ok=bool(answer), session_reset=session_reset)