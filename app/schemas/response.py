# app/schemas/response.py
# Schema dữ liệu response trả về từ endpoint /agent/invoke (câu trả lời + thread_id).

from pydantic import BaseModel, Field


class AgentResponse(BaseModel):
    answer: str = Field(..., description="Câu trả lời của agent")
    thread_id: str = Field(..., description="ID hội thoại, dùng cho các lượt hỏi tiếp theo")