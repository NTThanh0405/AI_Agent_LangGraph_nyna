from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class AgentRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    message: str = Field(
        ...,
        min_length=1,
        max_length=4096,
        description="Nội dung người dùng gửi cho agent",
    )
    thread_id: Optional[str] = Field(
        default=None,
        max_length=128,
        description="ID hội thoại. Bỏ trống để tạo hội thoại mới",
    )
    user_id: Optional[str] = Field(
        default=None,
        max_length=64,
        description="ID người dùng (ví dụ Telegram user id), dùng cho phân quyền và log",
    )
    user_role: Optional[str] = Field(
        default=None,
        max_length=32,
        description="Vai trò của người dùng (ví dụ boss, manager)",
    )