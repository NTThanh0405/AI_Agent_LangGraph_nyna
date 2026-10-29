# app/api/v1/agent_route.py
# Endpoint POST /api/v1/agent/invoke: n8n (từ Telegram) gửi câu hỏi vào đây,
# route chạy graph LangGraph (có gọi tool truy vấn PostgreSQL) rồi trả câu trả lời.
# Endpoint POST /api/v1/agent/reset: kết thúc phiên hội thoại (lệnh /new).
# Mỗi thread chỉ chạy một lượt một lúc; lượt lỗi, quá hạn hoặc vượt giới hạn bước thì phiên
# bị xoá để không để lại hội thoại dở dang làm hỏng các lượt sau.

import asyncio
import logging
import uuid

from fastapi import APIRouter, HTTPException, Query, Request
from langgraph.errors import GraphRecursionError

from app.agent.sessions import get_lock, reset, touch
from app.schemas.request import ID_PATTERN, THREAD_PATTERN, AgentRequest
from app.schemas.response import AgentResponse

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/agent", tags=["agent"])

MAX_GRAPH_STEPS = 20          # mỗi vòng chat <-> tools tốn 2 bước
INVOKE_TIMEOUT_SECONDS = 120  # tổng thời gian tối đa của một lượt hỏi

MSG_BUSY = "Mình đang xử lý câu hỏi trước của bạn, vui lòng đợi xong rồi gửi lại."
MSG_TOO_COMPLEX = "Câu hỏi quá phức tạp, vui lòng chia nhỏ rồi hỏi lại."
MSG_TIMEOUT = "Câu hỏi xử lý quá lâu nên đã bị dừng. Hãy thu hẹp phạm vi hoặc chia nhỏ câu hỏi."
MSG_EMPTY = "Mình chưa tạo được câu trả lời, bạn thử hỏi lại nhé."


def _internal_thread(user_id: str, thread_id: str) -> str:
    """Gắn user_id vào thread nội bộ: người khác đoán được thread_id cũng không đọc được."""
    return f"{user_id}:{thread_id}"


def _extract_text(content) -> str:
    """Lấy phần chữ từ nội dung tin nhắn AI (Claude có thể trả chuỗi hoặc danh sách block)."""
    if isinstance(content, str):
        return content
    return "".join(
        block.get("text", "")
        for block in content
        if isinstance(block, dict) and block.get("type", "text") == "text"
    )


async def _safe_reset(graph, thread: str) -> None:
    try:
        await reset(graph, thread)
    except Exception:
        logger.exception("Không xoá được phiên %s sau lỗi", thread)


@router.post("/invoke", response_model=AgentResponse)
async def invoke_agent(body: AgentRequest, request: Request) -> AgentResponse:
    graph = request.app.state.graph

    thread_id = body.thread_id or str(uuid.uuid4())
    internal_thread = _internal_thread(body.user_id, thread_id)

    lock = get_lock(internal_thread)
    if lock.locked():
        return AgentResponse(answer=MSG_BUSY, thread_id=thread_id)

    # user_role do n8n gửi lên (đã kiểm tra thuộc danh sách vai trò hợp lệ);
    # n8n phải tự xác định vai trò từ Telegram user id, không lấy từ nội dung người dùng gõ.
    config = {
        "configurable": {
            "thread_id": internal_thread,
            "user_id": body.user_id,
            "user_role": body.user_role,
        },
        "recursion_limit": MAX_GRAPH_STEPS,
    }
    initial_state = {
        "messages": [("user", body.message)],
        "user_id": body.user_id,
        "user_role": body.user_role,
    }

    async with lock:
        try:
            await touch(graph, internal_thread)
            async with asyncio.timeout(INVOKE_TIMEOUT_SECONDS):
                result = await graph.ainvoke(initial_state, config)
        except GraphRecursionError:
            logger.warning("Vượt giới hạn bước (thread=%s)", internal_thread)
            await _safe_reset(graph, internal_thread)
            return AgentResponse(answer=MSG_TOO_COMPLEX, thread_id=thread_id)
        except TimeoutError:
            logger.warning("Quá thời gian %ss (thread=%s)", INVOKE_TIMEOUT_SECONDS, internal_thread)
            await _safe_reset(graph, internal_thread)
            return AgentResponse(answer=MSG_TIMEOUT, thread_id=thread_id)
        except Exception:
            logger.exception("Agent error (thread=%s)", internal_thread)
            await _safe_reset(graph, internal_thread)
            raise HTTPException(status_code=500, detail="Agent gặp lỗi, vui lòng thử lại sau.")

    answer = _extract_text(result["messages"][-1].content).strip() or MSG_EMPTY
    return AgentResponse(answer=answer, thread_id=thread_id)


@router.post("/reset")
async def reset_session(
    request: Request,
    thread_id: str = Query(..., pattern=THREAD_PATTERN),
    user_id: str = Query(..., pattern=ID_PATTERN),
) -> dict:
    """Kết thúc phiên hiện tại; lần hỏi sau sẽ bắt đầu hội thoại mới (n8n gọi khi gõ /new)."""
    internal_thread = _internal_thread(user_id, thread_id)
    async with get_lock(internal_thread):
        await reset(request.app.state.graph, internal_thread)
    return {"status": "reset"}