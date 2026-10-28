# app/api/v1/agent_route.py
import logging
import uuid

from fastapi import APIRouter, HTTPException, Request

from app.schemas.request import AgentRequest
from app.schemas.response import AgentResponse

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/agent", tags=["agent"])


def _extract_text(content) -> str:
    if isinstance(content, str):
        return content
    return "".join(
        block.get("text", "") for block in content if isinstance(block, dict)
    )


@router.post("/invoke", response_model=AgentResponse)
async def invoke_agent(body: AgentRequest, request: Request) -> AgentResponse:
    graph = request.app.state.graph
    thread_id = body.thread_id or str(uuid.uuid4())
    config = {"configurable": {"thread_id": thread_id}}

    try:
        result = await graph.ainvoke(
            {"messages": [("user", body.message)]}, config
        )
    except Exception:
        logger.exception("Agent error (thread_id=%s, user_id=%s)", thread_id, body.user_id)
        raise HTTPException(status_code=500, detail="Agent gặp lỗi, vui lòng thử lại sau.")

    answer = _extract_text(result["messages"][-1].content)
    return AgentResponse(answer=answer, thread_id=thread_id)