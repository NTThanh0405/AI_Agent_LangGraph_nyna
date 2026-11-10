# app/agent/nodes/supervisor.py
# Điều phối: quyết định agent tiếp theo sau mỗi bước. KHÔNG gọi LLM và KHÔNG có tool dữ liệu
# (nhanh, rẻ, đoán được); phần tổng hợp câu trả lời cuối do node `chat` làm.
#
#   chào hỏi / "báo cáo hôm nay"  -> đường tắt (router_rules)
#   analyst -> coder -> (designer nếu cần biểu đồ/dashboard) -> chat
#   analyst báo intent "other" (không cần dữ liệu) -> chat luôn
# Quá max_handoffs thì chuyển thẳng sang chat để chốt câu trả lời bằng những gì đã có.

from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableConfig

from app.agent.models import AgentsConfig, load_agents_config
from app.agent.nodes import router_rules
from app.agent.nodes._common import last_human_text

END_MARK = "end"


def wants_visual(brief: dict) -> bool:
    return bool(brief.get("needs_chart") or brief.get("needs_dashboard") or brief.get("intent") == "dashboard")


def decide_next(state: dict, cfg: AgentsConfig) -> str:
    """Hàm thuần (dễ test): agent tiếp theo dựa trên state hiện tại."""
    if (state.get("handoffs") or 0) >= cfg.max_handoffs:
        return "chat"
    brief = state.get("brief")
    if brief is None:
        return "analyst"
    if brief.get("intent") == "other":
        return "chat"
    if not state.get("coder_done"):
        return "coder"
    if wants_visual(brief) and cfg.get("designer").enabled and not state.get("designer_done"):
        return "designer"
    return "chat"


async def supervisor_node(state: dict, config: RunnableConfig) -> dict:
    handoffs = state.get("handoffs") or 0
    update: dict = {"handoffs": handoffs + 1}

    if handoffs == 0:  # đường tắt chỉ xét ở lần đầu của lượt
        rule = router_rules.match_rule(last_human_text(state["messages"]))
        if rule == "smalltalk":
            return {**update, "next_agent": "chat", "brief": {"question": last_human_text(state["messages"]), "intent": "other"}}
        if rule == "report":
            cfgv = config.get("configurable") or {}
            text = await router_rules.build_report_text(str(cfgv.get("user_id") or state.get("user_id")),
                                                        cfgv.get("user_role") or state.get("user_role") or "")
            return {**update, "next_agent": END_MARK, "messages": [AIMessage(content=text)]}

    return {**update, "next_agent": decide_next(state, load_agents_config())}


def route_from_supervisor(state: dict) -> str:
    return state.get("next_agent") or "chat"
