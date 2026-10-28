# app/agent/graph.py
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from app.agent.nodes.chat import chat_node
from app.agent.state import AgentState


def build_graph(checkpointer=None):
    builder = StateGraph(AgentState)
    builder.add_node("chat", chat_node)
    builder.add_edge(START, "chat")
    builder.add_edge("chat", END)
    return builder.compile(checkpointer=checkpointer or MemorySaver())