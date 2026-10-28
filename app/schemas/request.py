from typing import Optional

from pydantic import BaseModel, Field


class AgentRequest(BaseModel):
    message: str = Field(..., min_length=1, description="Nội dung người dùng gửi cho agent")
    thread_id: Optional[str] = Field(
        default=None,
        description="ID hội thoại. Bỏ trống để tạo hội thoại mới",
    )