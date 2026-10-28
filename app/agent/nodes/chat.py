# app/agent/nodes/chat.py
# Node "chat" của graph: gọi Claude, để Claude quyết định trả lời luôn hay gọi
# tool truy vấn PostgreSQL trước. Chứa system prompt của agent phân tích dữ liệu.

from functools import lru_cache

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import SystemMessage

from app.agent.state import AgentState
from app.agent.tools import ALL_TOOLS  # các tool truy vấn DB (file sẽ thêm ở bước sau)
from app.config import get_settings

# Prompt gốc: vai trò + quy tắc làm việc với dữ liệu.
SYSTEM_PROMPT = (
    "Bạn là trợ lý phân tích dữ liệu nội bộ cho ban lãnh đạo. "
    "Trả lời bằng tiếng Việt, ngắn gọn, rõ ràng, nêu số liệu cụ thể. "
    "Quy trình: (1) dùng tool liệt kê bảng và xem cột để hiểu cấu trúc dữ liệu, "
    "(2) viết câu SELECT phù hợp rồi chạy bằng tool, (3) dựa trên kết quả để trả lời. "
    "Chỉ được dùng số liệu lấy từ database. "
    "Nếu không có dữ liệu hoặc truy vấn lỗi thì nói thẳng là không biết, "
    "tuyệt đối không bịa số liệu. "
    "Bạn chỉ được đọc dữ liệu, không được thay đổi dữ liệu."
)


@lru_cache
def get_llm():
    """Tạo client Claude đã gắn sẵn các tool (chỉ tạo một lần, dùng lại)."""
    s = get_settings()
    llm = ChatAnthropic(
        model=s.model_name,
        api_key=s.anthropic_api_key,
        temperature=s.temperature,
        max_tokens=s.max_tokens,
        timeout=s.llm_timeout,
        max_retries=2,
    )
    # bind_tools cho phép Claude yêu cầu gọi tool trong câu trả lời của nó.
    return llm.bind_tools(ALL_TOOLS)


def _build_system_message(state: AgentState) -> SystemMessage:
    """Ghép system prompt với thông tin người đang hỏi (vai trò)."""
    role = state.get("user_role") or "không xác định"
    return SystemMessage(content=f"{SYSTEM_PROMPT}\nVai trò người hỏi: {role}.")


async def chat_node(state: AgentState) -> dict:
    """Chạy một lượt LLM: nhận lịch sử tin nhắn, trả về tin nhắn AI mới.

    Tin nhắn AI có thể chứa yêu cầu gọi tool; khi đó graph sẽ chuyển sang node
    "tools" (xem app/agent/graph.py), chạy xong quay lại đây.
    """
    messages = [_build_system_message(state), *state["messages"]]
    response = await get_llm().ainvoke(messages)
    return {"messages": [response]}