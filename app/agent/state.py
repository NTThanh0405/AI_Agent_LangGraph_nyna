# app/agent/state.py
# Định nghĩa "trạng thái" (state) chạy xuyên suốt graph LangGraph.
# Mỗi node trong graph nhận state này vào và trả về phần state cần cập nhật.

from typing import Annotated, Optional, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages


class AgentState(TypedDict):
    """Trạng thái của agent trong một lượt hội thoại."""

    # Danh sách tin nhắn của hội thoại (người dùng, AI, kết quả tool).
    # add_messages là reducer: tin nhắn mới được NỐI THÊM vào danh sách
    # thay vì ghi đè, nhờ vậy agent nhớ được ngữ cảnh và các lần gọi tool.
    messages: Annotated[list[AnyMessage], add_messages]

    # ID người dùng (ví dụ Telegram user id) - dùng cho log và phân quyền.
    user_id: Optional[str]

    # Vai trò người dùng (ví dụ "boss", "manager") - quyết định bảng nào
    # được phép truy vấn trong PostgreSQL (xem app/agent/permissions.py).
    user_role: Optional[str]