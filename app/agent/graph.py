# app/agent/graph.py
# Dựng graph LangGraph cho agent phân tích dữ liệu.
#
# Luồng chạy (vòng lặp ReAct):
#   START -> chat -> (có gọi tool?) -- có --> tools -> chat -> ... -> END
#                                   -- không -> END
# Node "chat" để LLM suy nghĩ/quyết định gọi tool; node "tools" chạy tool
# (truy vấn PostgreSQL) rồi trả kết quả về cho "chat" đọc và trả lời.

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
    """Tạo và compile graph.

    Args:
        tools: danh sách tool agent được phép gọi (liệt kê bảng, chạy SELECT...).
        checkpointer: nơi lưu hội thoại theo thread_id. Khi chạy thật nên truyền
            PostgresSaver để không mất hội thoại khi restart; nếu bỏ trống sẽ
            dùng MemorySaver (lưu trong RAM, chỉ phù hợp khi chạy thử).
    """
    builder = StateGraph(AgentState)

    # Node 1: LLM suy nghĩ và quyết định có gọi tool hay không.
    builder.add_node("chat", chat_node)

    # Node 2: thực thi các tool mà LLM yêu cầu, trả kết quả lại dạng ToolMessage.
    builder.add_node("tools", ToolNode(list(tools)))

    # Bắt đầu luôn đi vào node chat.
    builder.add_edge(START, "chat")

    # Sau chat: nếu tin nhắn AI có yêu cầu gọi tool -> sang "tools",
    # ngược lại -> kết thúc và trả câu trả lời cho người dùng.
    builder.add_conditional_edges("chat", tools_condition, {"tools": "tools", END: END})

    # Sau khi chạy tool xong, quay lại chat để LLM đọc kết quả và trả lời.
    builder.add_edge("tools", "chat")

    # Checkpointer giúp agent nhớ hội thoại theo thread_id.
    return builder.compile(checkpointer=checkpointer or MemorySaver())