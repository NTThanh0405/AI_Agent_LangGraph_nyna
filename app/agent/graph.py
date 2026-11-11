# app/agent/graph.py
# Graph v2 (supervisor + 3 worker):
#   START -> supervisor -+-> analyst --------------> supervisor
#                        +-> coder <-> coder_tools -> supervisor
#                        +-> designer <-> designer_tools -> supervisor
#                        +-> chat -> END         (soạn câu trả lời cuối)
#                        +-> END                 (đường tắt: báo cáo đã có sẵn câu trả lời)
# Chống lặp: supervisor giới hạn max_handoffs, mỗi worker giới hạn max_steps (config/agents.yaml),
# runner đặt recursion_limit theo models.recursion_limit().

from typing import Optional, Sequence

from langchain_core.tools import BaseTool
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from app.agent.nodes._common import make_tool_node
from app.agent.nodes.analyst import analyst_node
from app.agent.nodes.chat import chat_node
from app.agent.nodes.coder import make_coder_node
from app.agent.nodes.designer import make_designer_node
from app.agent.nodes.supervisor import END_MARK, route_from_supervisor, supervisor_node
from app.agent.state import AgentState
from app.agent.scopes import load_scopes_config
from app.agent.tools import API_TOOL_NAMES, TOOLS_BY_AGENT


def _tools_for(agent: str, allowed: Optional[set[str]]) -> list[BaseTool]:
    # Chưa có khối dữ liệu API nào (hiện chỉ có coffee) thì không gắn tool gọi API: đỡ token, LLM khỏi gọi nhầm
    skip = set() if load_scopes_config().api_scopes() else API_TOOL_NAMES
    return [t for t in TOOLS_BY_AGENT.get(agent, [])
            if (allowed is None or t.name in allowed) and t.name not in skip]


def build_graph(tools: Optional[Sequence[BaseTool]] = None, checkpointer: Optional[BaseCheckpointSaver] = None):
    """`tools` (tuỳ chọn, tương thích v1): nếu truyền thì mỗi agent chỉ được dùng phần giao của
    danh sách của nó với `tools`. Bỏ trống checkpointer thì dùng MemorySaver (mất khi restart)."""
    allowed = {t.name for t in tools} if tools is not None else None
    coder_tools = _tools_for("coder", allowed)
    designer_tools = _tools_for("designer", allowed)

    g = StateGraph(AgentState)
    g.add_node("supervisor", supervisor_node)
    g.add_node("analyst", analyst_node)
    g.add_node("coder", make_coder_node(coder_tools))
    g.add_node("coder_tools", make_tool_node("coder", coder_tools, "coder_messages"))
    g.add_node("designer", make_designer_node(designer_tools))
    g.add_node("designer_tools", make_tool_node("designer", designer_tools, "designer_messages"))
    g.add_node("chat", chat_node)

    g.add_edge(START, "supervisor")
    g.add_conditional_edges(
        "supervisor", route_from_supervisor,
        {"analyst": "analyst", "coder": "coder", "designer": "designer", "chat": "chat", END_MARK: END},
    )
    g.add_edge("analyst", "supervisor")
    def _after_worker(agent: str):
        """Worker xong (đã tổng kết) -> về supervisor; chưa -> chạy tool của chính nó."""
        done_key, tools_node = f"{agent}_done", f"{agent}_tools"

        def route(state: dict) -> str:
            return "supervisor" if state.get(done_key) else tools_node
        return route

    for worker in ("coder", "designer"):
        g.add_conditional_edges(
            worker, _after_worker(worker),
            {"supervisor": "supervisor", f"{worker}_tools": f"{worker}_tools"},
        )
        g.add_edge(f"{worker}_tools", worker)
    g.add_edge("chat", END)

    return g.compile(checkpointer=checkpointer if checkpointer is not None else MemorySaver())
