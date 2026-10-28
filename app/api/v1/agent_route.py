# app/api/v1/agent_route.py
# Endpoint POST /api/v1/agent/invoke: n8n (từ Telegram) gửi câu hỏi vào đây,
# route chạy graph LangGraph (có gọi tool truy vấn PostgreSQL) rồi trả câu trả lời.

import logging
import uuid

from fastapi import APIRouter, HTTPException, Request
from langgraph.errors import GraphRecursionError

from app.schemas.request import AgentRequest
from app.schemas.response import AgentResponse

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/agent", tags=["agent"])

# Số bước tối đa của vòng lặp chat <-> tools trong một lượt hỏi.
# Chặn trường hợp agent gọi tool lặp vô hạn (tốn tiền, treo request).
MAX_GRAPH_STEPS = 12


def _extract_text(content) -> str:
    """Lấy phần chữ từ nội dung tin nhắn AI.

    Claude có thể trả về chuỗi, hoặc danh sách block (text, tool_use...).
    Chỉ ghép các block dạng text, bỏ qua block gọi tool.
    """
    if isinstance(content, str):
        return content
    return "".join(
        block.get("text", "")
        for block in content
        if isinstance(block, dict) and block.get("type", "text") == "text"
    )


@router.post("/invoke", response_model=AgentResponse)
async def invoke_agent(body: AgentRequest, request: Request) -> AgentResponse:
    """Nhận một câu hỏi, chạy agent và trả câu trả lời."""
    graph = request.app.state.graph  # graph đã compile sẵn ở app/main.py

    # thread_id là ID mà client (n8n) nhìn thấy; bỏ trống thì tạo hội thoại mới.
    thread_id = body.thread_id or str(uuid.uuid4())

    # Gắn user_id vào thread nội bộ để hội thoại của mỗi người tách biệt:
    # người khác đoán được thread_id cũng không đọc được hội thoại này.
    internal_thread = f"{body.user_id}:{thread_id}" if body.user_id else thread_id

    config = {
        "configurable": {
            "thread_id": internal_thread,
            # Để tool đọc được người hỏi là ai khi kiểm tra quyền xem bảng.
            "user_id": body.user_id,
            "user_role": body.user_role,
        },
        "recursion_limit": MAX_GRAPH_STEPS,
    }

    # Trạng thái đầu vào của graph: câu hỏi + thông tin người hỏi.
    # user_role do n8n gửi lên, nên n8n phải tự xác định vai trò từ Telegram
    # user id (danh sách cho phép), không lấy từ nội dung người dùng gõ.
    initial_state = {
        "messages": [("user", body.message)],
        "user_id": body.user_id,
        "user_role": body.user_role,
    }

    try:
        result = await graph.ainvoke(initial_state, config)
    except GraphRecursionError:
        # Agent gọi tool quá nhiều vòng mà chưa ra được câu trả lời.
        logger.warning(
            "Vượt giới hạn bước (thread_id=%s, user_id=%s)", thread_id, body.user_id
        )
        raise HTTPException(
            status_code=500,
            detail="Câu hỏi quá phức tạp, vui lòng chia nhỏ rồi hỏi lại.",
        )
    except Exception:
        # Ghi log đầy đủ để debug, nhưng không lộ chi tiết lỗi cho client.
        logger.exception(
            "Agent error (thread_id=%s, user_id=%s)", thread_id, body.user_id
        )
        raise HTTPException(
            status_code=500, detail="Agent gặp lỗi, vui lòng thử lại sau."
        )

    # Tin nhắn cuối cùng là câu trả lời của AI sau khi đã đọc xong kết quả tool.
    answer = _extract_text(result["messages"][-1].content)
    return AgentResponse(answer=answer, thread_id=thread_id)