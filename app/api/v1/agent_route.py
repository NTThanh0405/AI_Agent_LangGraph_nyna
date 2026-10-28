import uuid

from fastapi import APIRouter, HTTPException, Request

from app.schemas.request import AgentRequest
from app.schemas.response import AgentResponse

router = APIRouter(prefix="/agent", tags=["agent"])


def _extract_text(content) -> str:
    # Claude có thể trả về chuỗi hoặc danh sách các block nội dung
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
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Agent error: {e}")

    answer = _extract_text(result["messages"][-1].content)
    return AgentResponse(answer=answer, thread_id=thread_id)