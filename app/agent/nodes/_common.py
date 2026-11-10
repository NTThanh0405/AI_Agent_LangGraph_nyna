# app/agent/nodes/_common.py
# Phần dùng chung của các node: LLM theo agent, ghép system prompt, rút gọn lịch sử, thực thi tool theo agent.
#
# QUY TẮC LỊCH SỬ: LLM chỉ nhận hội thoại dạng chữ (người dùng + câu trả lời cuối của các lượt trước),
# không nhận tool_use/tool_result của lượt cũ. Vòng tool của worker nằm riêng trong state và chỉ được
# đưa vào đúng worker đó, nên lịch sử không bao giờ có tool_use dở dang.

import asyncio
import json
import logging
from datetime import datetime
from functools import lru_cache
from typing import Iterable, Optional
from zoneinfo import ZoneInfo

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool

from app.agent.models import get_model
from app.agent.prompts import load_prompt

logger = logging.getLogger(__name__)

MAX_HISTORY_MESSAGES = 30
MAX_TOOL_TEXT_EACH = 6000
MAX_TOOL_TEXT_TOTAL = 20000
_WEEKDAYS = ("thứ Hai", "thứ Ba", "thứ Tư", "thứ Năm", "thứ Sáu", "thứ Bảy", "Chủ nhật")


@lru_cache(maxsize=None)
def get_llm(agent: str):
    """Model của agent (theo config/agents.yaml). Test thay hàm này bằng LLM giả."""
    return get_model(agent)


def extract_text(content) -> str:
    if isinstance(content, str):
        return content
    return "".join(
        b.get("text", "") for b in content if isinstance(b, dict) and b.get("type", "text") == "text"
    )


def last_human_text(messages: Iterable[BaseMessage]) -> str:
    for m in reversed(list(messages)):
        if isinstance(m, HumanMessage):
            return extract_text(m.content).strip()
    return ""


def plain_history(messages: list[BaseMessage], limit: int = MAX_HISTORY_MESSAGES) -> list[BaseMessage]:
    """Chỉ giữ chữ của người dùng và AI; bỏ tool_use/tool_result; cắt còn `limit` tin gần nhất và
    bắt đầu bằng tin người dùng (API của Claude yêu cầu tin đầu tiên là của người dùng)."""
    out: list[BaseMessage] = []
    for m in messages:
        if isinstance(m, HumanMessage):
            text = extract_text(m.content).strip()
            if text:
                out.append(HumanMessage(content=text))
        elif isinstance(m, AIMessage):
            text = extract_text(m.content).strip()
            if text:
                out.append(AIMessage(content=text))
    out = out[-limit:]
    while out and not isinstance(out[0], HumanMessage):
        out.pop(0)
    return out


def today_text() -> str:
    now = datetime.now(ZoneInfo("Asia/Ho_Chi_Minh"))
    return f"{now:%d/%m/%Y} ({_WEEKDAYS[now.weekday()]})"


def system_text(agent_prompt: str, config: RunnableConfig, *sections: Optional[str]) -> str:
    role = (config.get("configurable") or {}).get("user_role") or "không xác định"
    head = f"{load_prompt(agent_prompt)}\n\nHôm nay là {today_text()}. Vai trò người hỏi: {role}."
    extra = [s for s in sections if s]
    return head + ("\n\n" + "\n\n".join(extra) if extra else "")


def brief_section(brief: Optional[dict]) -> Optional[str]:
    if not brief:
        return None
    return "Bản tóm tắt phân tích (JSON):\n" + json.dumps(brief, ensure_ascii=False)


def tool_results_text(loop: Iterable[BaseMessage]) -> str:
    """Gộp kết quả tool của một vòng worker thành chữ (cắt độ dài) để đưa cho bước soạn câu trả lời."""
    parts, total = [], 0
    for m in loop:
        if not isinstance(m, ToolMessage):
            continue
        text = extract_text(m.content).strip()
        if len(text) > MAX_TOOL_TEXT_EACH:
            text = text[:MAX_TOOL_TEXT_EACH] + " …(đã cắt bớt)"
        if total + len(text) > MAX_TOOL_TEXT_TOTAL:
            parts.append("…(còn kết quả khác, đã bỏ bớt)")
            break
        parts.append(f"[{m.name or 'tool'}] {text}")
        total += len(text)
    return "\n".join(parts)


# ---------------------------------------------------------------- thực thi tool theo agent

def make_tool_node(agent: str, tools: list[BaseTool], loop_key: str):
    """Node chạy các tool_call ở tin AI cuối của `loop_key`. Chỉ chạy tool nằm trong danh sách của
    agent này; tool lạ hoặc lỗi được trả về cho LLM dưới dạng ToolMessage, không làm sập lượt hỏi."""
    by_name = {t.name: t for t in tools}

    async def _one(call: dict, config: RunnableConfig) -> ToolMessage:
        name, call_id = call.get("name", ""), call.get("id", "")
        tool = by_name.get(name)
        if tool is None:
            logger.warning("Agent %s gọi tool ngoài danh sách: %s", agent, name)
            return ToolMessage(content=f"Lỗi: agent này không được dùng tool '{name}'.",
                               name=name, tool_call_id=call_id, status="error")
        try:
            result = await tool.ainvoke(
                {"type": "tool_call", "id": call_id, "name": name, "args": call.get("args") or {}}, config
            )
        except Exception as e:  # tool lỗi -> LLM tự xử lý (sửa SQL, xin lỗi người dùng...)
            logger.warning("Tool %s lỗi: %s", name, e)
            return ToolMessage(content=f"Lỗi khi chạy tool: {e}", name=name,
                               tool_call_id=call_id, status="error")
        if isinstance(result, ToolMessage):
            return result
        return ToolMessage(content=str(result), name=name, tool_call_id=call_id)

    async def node(state: dict, config: RunnableConfig) -> dict:
        loop = state.get(loop_key) or []
        last = loop[-1] if loop else None
        calls = getattr(last, "tool_calls", None) or []
        if not calls:
            return {}
        results = await asyncio.gather(*[_one(c, config) for c in calls])
        return {loop_key: list(results)}

    return node
