# app/agent/nodes/chat.py
from functools import lru_cache

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import SystemMessage

from app.agent.state import AgentState
from app.config import get_settings

SYSTEM_PROMPT = (
    "Bạn là trợ lý công việc nội bộ cho ban lãnh đạo. "
    "Trả lời bằng tiếng Việt, ngắn gọn, rõ ràng. "
    "Nếu không có dữ liệu để trả lời thì nói thẳng là không biết, không được bịa số liệu."
)


@lru_cache
def get_llm() -> ChatAnthropic:
    s = get_settings()
    return ChatAnthropic(
        model=s.model_name,
        api_key=s.anthropic_api_key,
        temperature=s.temperature,
        max_tokens=s.max_tokens,
        timeout=s.llm_timeout,
        max_retries=2,
    )


async def chat_node(state: AgentState) -> dict:
    messages = [SystemMessage(content=SYSTEM_PROMPT), *state["messages"]]
    response = await get_llm().ainvoke(messages)
    return {"messages": [response]}