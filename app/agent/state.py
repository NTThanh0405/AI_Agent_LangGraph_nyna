# app/agent/state.py
# State chạy xuyên suốt graph v2 (supervisor + analyst/coder/designer + chat).
# `messages` CHỈ chứa hội thoại thật (người dùng và câu trả lời cuối), được checkpoint lưu giữa các lượt.
# Vòng tool của từng worker nằm riêng trong coder_messages / designer_messages và bị xoá đầu mỗi lượt,
# nên lịch sử không bao giờ có tool_use dở dang. Các trường còn lại là điều phối trong MỘT lượt hỏi
# và được runner đặt lại bằng new_turn_state().

from typing import Annotated, Any, Optional, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages

RESET = "__reset__"  # giá trị đặc biệt để xoá danh sách tin của vòng tool


def loop_reducer(left, right):
    """Như add_messages, nhưng nhận [RESET] để xoá sạch danh sách (đặt lại đầu mỗi lượt)."""
    if right is None:
        return left or []
    if isinstance(right, list) and right and right[0] == RESET:
        return add_messages([], right[1:]) if len(right) > 1 else []
    return add_messages(left or [], right)


class AgentState(TypedDict, total=False):
    messages: Annotated[list[AnyMessage], add_messages]
    user_id: Optional[str]
    user_role: Optional[str]  # do server tra từ bảng users, quyết định bảng được đọc (permissions.py)

    # ---- điều phối trong một lượt ----
    brief: Optional[dict[str, Any]]      # AnalysisBrief.model_dump(mode="json")
    next_agent: Optional[str]            # analyst | coder | designer | chat | end
    handoffs: int                        # số lần supervisor đã chạy trong lượt (chống lặp vô hạn)
    coder_done: bool
    designer_done: bool
    coder_steps: int
    designer_steps: int
    coder_notes: Optional[str]           # tổng kết của Coder: SQL đã dùng + số liệu chính
    designer_notes: Optional[str]
    coder_messages: Annotated[list[AnyMessage], loop_reducer]
    designer_messages: Annotated[list[AnyMessage], loop_reducer]


def new_turn_state(user_id: str, role: str, human_message) -> dict:
    """State đầu vào cho một lượt hỏi: tin mới + đặt lại mọi trường điều phối của lượt trước."""
    return {
        "messages": [human_message],
        "user_id": user_id,
        "user_role": role,
        "brief": None,
        "next_agent": None,
        "handoffs": 0,
        "coder_done": False,
        "designer_done": False,
        "coder_steps": 0,
        "designer_steps": 0,
        "coder_notes": None,
        "designer_notes": None,
        "coder_messages": [RESET],
        "designer_messages": [RESET],
    }
