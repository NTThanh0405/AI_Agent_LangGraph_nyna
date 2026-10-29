# app/schemas/request.py
# Schema dữ liệu request gửi vào endpoint /agent/invoke: câu hỏi + thông tin người hỏi.
# user_id và thread_id chỉ cho phép chữ, số, "_" và "-" (không có ":" nên không thể trộn
# lẫn thread nội bộ của hai người); user_role phải là vai trò có trong ROLE_TABLES.

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.agent.permissions import ROLE_TABLES

ID_PATTERN = r"^[A-Za-z0-9_-]{1,64}$"
THREAD_PATTERN = r"^[A-Za-z0-9_-]{1,128}$"


class AgentRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    message: str = Field(..., min_length=1, max_length=4096)
    thread_id: Optional[str] = Field(
        default=None,
        pattern=THREAD_PATTERN,
        description="ID hội thoại. Bỏ trống để tạo hội thoại mới",
    )
    user_id: str = Field(
        ...,
        pattern=ID_PATTERN,
        description="ID người dùng (ví dụ Telegram user id)",
    )
    user_role: str = Field(..., description="Vai trò của người dùng, ví dụ boss, manager")

    @field_validator("user_role")
    @classmethod
    def _role_must_be_known(cls, v: str) -> str:
        role = v.lower()
        if role not in ROLE_TABLES:
            raise ValueError("user_role không hợp lệ")
        return role