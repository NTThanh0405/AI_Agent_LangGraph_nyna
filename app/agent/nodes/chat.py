from functools import lru_cache

from langchain_anthropic import ChatAnthropic

from app.agent.state import AgentState
from app.config import get_settings


@lru_cache
def get_llm() -> ChatAnthropic:
    s = get_settings()
    return ChatAnthropic(
        model=s.model_name,
        api_key=s.anthropic_api_key,
        temperature=s.temperature,
        max_tokens=s.max_tokens,
    )


def chat_node(state: AgentState) -> dict:
    response = get_llm().invoke(state["messages"])
    return {"messages": [response]}