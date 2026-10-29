# app/agent/graph.py
# Dựng graph LangGraph cho agent phân tích dữ liệu (vòng lặp ReAct):
#   START -> chat -> (có gọi tool?) -- có --> tools -> chat -> ... -> END
#                                   -- không -> END
# Node "chat" để LLM quyết định gọi tool; node "tools" chạy tool (truy vấn PostgreSQL)
# rồi trả kết quả về cho "chat". Tool lỗi được trả về cho LLM dưới dạng ToolMessage
# thay vì làm sập cả lượt. Hội thoại nhớ theo thread_id qua checkpointer (mặc định RAM).

from typing import Optional, Sequence

from langchain_core.tools import BaseTool
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition

from app.agent.nodes.chat import chat_node
from app.agent.state import AgentState


def build_graph(
    tools: Sequence[BaseTool],
    checkpointer: Optional[BaseCheckpointSaver] = None,
):
    """Tạo và compile graph. Bỏ trống checkpointer thì dùng MemorySaver (mất khi restart)."""
    builder = StateGraph(AgentState)
    builder.add_node("chat", chat_node)
    builder.add_node("tools", ToolNode(list(tools), handle_tool_errors=True))

    builder.add_edge(START, "chat")
    builder.add_conditional_edges("chat", tools_condition, {"tools": "tools", END: END})
    builder.add_edge("tools", "chat")

    return builder.compile(
        checkpointer=checkpointer if checkpointer is not None else MemorySaver()
    )