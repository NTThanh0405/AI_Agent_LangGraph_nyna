# app/agent/nodes/_worker.py
# Node "worker" dùng chung cho Coder và Designer: vòng LLM <-> tool riêng của từng agent.
# - Chỉ bind đúng tool của agent (TOOLS_BY_AGENT), vòng tool lưu ở state[loop_key], không lẫn vào messages.
# - Đủ max_steps (config/agents.yaml) mà vẫn đòi tool thì ép tổng kết bằng dữ liệu đã có.
# - Không đưa chữ tổng kết vào messages (tránh tin AI kết thúc hội thoại); lưu ở state[notes_key].

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig

from app.agent.models import load_agents_config
from app.agent.nodes import _common as C

FORCE_FINISH = (
    "Đã hết số bước cho phép. Không gọi thêm tool. Hãy tổng kết ngay bằng dữ liệu đã lấy được "
    "và nói rõ phần nào còn thiếu."
)


def make_worker_node(agent: str, tools: list, loop_key: str, steps_key: str, done_key: str, notes_key: str,
                     extra_sections=None):
    """extra_sections(state) -> list[str|None]: phần ngữ cảnh thêm vào system prompt (ngoài bản tóm tắt)."""

    async def node(state: dict, config: RunnableConfig) -> dict:
        spec = load_agents_config().get(agent)
        steps = state.get(steps_key) or 0
        loop = list(state.get(loop_key) or [])
        forced = steps >= spec.max_steps

        sections = [C.brief_section(state.get("brief"))]
        if extra_sections:
            sections += extra_sections(state)
        system = SystemMessage(content=C.system_text(agent, config, *sections))

        llm = C.get_llm(agent)
        if tools:
            llm = llm.bind_tools(tools)
        msgs = [system, *C.plain_history(state["messages"]), *loop]
        if forced:
            msgs.append(HumanMessage(content=FORCE_FINISH))
        resp = await llm.ainvoke(msgs)

        if getattr(resp, "tool_calls", None) and not forced:
            return {loop_key: [resp], steps_key: steps + 1}
        notes = C.extract_text(resp.content).strip()
        return {done_key: True, notes_key: notes or "(không có ghi chú)"}

    return node
