from typing import Annotated

from langgraph.graph.message import add_messages
from typing_extensions import TypedDict


class AgentState(TypedDict):
    # add_messages: tin nhắn mới được nối thêm vào danh sách, không ghi đè
    messages: Annotated[list, add_messages]