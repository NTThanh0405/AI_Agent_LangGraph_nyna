from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from app.agent.nodes import chat_node
from app.agent.state import AgentState


def build_graph():
    builder = StateGraph(AgentState)
    builder.add_node("chat", chat_node)
    builder.add_edge(START, "chat")
    builder.add_edge("chat", END)
    # MemorySaver giúp agent nhớ hội thoại theo thread_id (lưu trong RAM)
    return builder.compile(checkpointer=MemorySaver())