# tests/test_graph_flow.py: chạy cả graph v2 với LLM giả (không tốn chi phí, không gọi mạng).
import os

for k, v in {"ANTHROPIC_API_KEY": "x", "TELEGRAM_BOT_TOKEN": "x", "DB_PASSWORD": "x"}.items():
    os.environ.setdefault(k, v)

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import MemorySaver

from app.agent.graph import build_graph
from app.agent.nodes import _common as C
from app.agent.nodes import router_rules
from app.agent.nodes.analyst import AnalystOutput
from app.agent.state import new_turn_state
from app.agent.tools import ALL_TOOLS


class FakeLLM:
    def __init__(self, script):
        self.script, self.calls, self.bound = list(script), [], None

    def bind_tools(self, tools):
        self.bound = [t.name for t in tools]
        return self

    def with_structured_output(self, schema):
        outer = self

        class _S:
            async def ainvoke(self, msgs):
                return outer._next(msgs)
        return _S()

    def _next(self, msgs):
        self.calls.append(msgs)
        item = self.script.pop(0) if self.script else self.script_default
        return item

    script_default = AIMessage(content="(mặc định)")

    async def ainvoke(self, msgs):
        return self._next(msgs)


def tool_call(name, args=None, cid="c1"):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args or {}, "id": cid, "type": "tool_call"}])


@pytest.fixture
def llms(monkeypatch):
    fakes = {}

    def install(**scripts):
        for agent, script in scripts.items():
            fakes[agent] = FakeLLM(script)
        monkeypatch.setattr(C, "get_llm", lambda agent: fakes[agent])
        return fakes
    return install


async def run(graph, text, thread="t1"):
    cfg = {"configurable": {"thread_id": thread, "user_id": "1", "user_role": "boss"}, "recursion_limit": 60}
    return await graph.ainvoke(new_turn_state("1", "boss", HumanMessage(content=text)), cfg)


@pytest.mark.asyncio
async def test_smalltalk_skips_analyst_and_workers(llms):
    f = llms(analyst=[], coder=[], designer=[], supervisor=[AIMessage(content="Chào bạn!")])
    out = await run(build_graph(ALL_TOOLS), "xin chào")
    assert out["messages"][-1].content == "Chào bạn!"
    assert not f["analyst"].calls and not f["coder"].calls


@pytest.mark.asyncio
async def test_data_question_runs_analyst_coder_chat(llms):
    f = llms(
        analyst=[AnalystOutput(question="x", intent="lookup", metrics=["revenue"], period_text="hôm qua")],
        coder=[tool_call("run_select_query", {"sql": "select 1"}), AIMessage(content="SQL: select 1. revenue=1000000")],
        designer=[], supervisor=[AIMessage(content="Doanh thu hôm qua là 1.000.000 ₫")],
    )
    out = await run(build_graph(ALL_TOOLS), "doanh thu hôm qua bao nhiêu")
    assert out["messages"][-1].content.startswith("Doanh thu")
    # messages chỉ có hội thoại thật: không lẫn tool_use/tool_result
    assert [type(m).__name__ for m in out["messages"]] == ["HumanMessage", "AIMessage"]
    assert out["brief"]["period_start"] and out["brief"]["period_start"] == out["brief"]["period_end"]  # "hôm qua" -> 1 ngày
    from app.agent.tools import API_TOOL_NAMES, TOOLS_BY_AGENT
    # chưa có khối API: coder nhận đúng tool khai báo trong TOOLS_BY_AGENT, không có tool gọi API
    assert f["coder"].bound == [t.name for t in TOOLS_BY_AGENT["coder"] if t.name not in API_TOOL_NAMES]
    assert {"run_select_query", "business_snapshot", "explain_change"} <= set(f["coder"].bound)
    assert any(isinstance(m, ToolMessage) for m in out["coder_messages"])
    sys_prompt = C.extract_text(f["supervisor"].calls[0][0].content)
    assert "revenue=1000000" in sys_prompt  # câu trả lời cuối thấy số liệu gốc từ tool
    assert not f["designer"].calls


@pytest.mark.asyncio
async def test_dashboard_goes_through_designer_and_link_reaches_chat(llms):
    f = llms(
        analyst=[AnalystOutput(question="x", intent="dashboard", needs_dashboard=True)],
        coder=[AIMessage(content="đã có số liệu")],
        designer=[tool_call("create_dashboard", {"title": "d"}), AIMessage(content="link: https://dashboard.nynahouse.com/d/abc")],
        supervisor=[AIMessage(content="Dashboard: https://dashboard.nynahouse.com/d/abc")],
    )
    out = await run(build_graph(ALL_TOOLS), "tạo dashboard doanh thu")
    assert f["designer"].bound == ["list_tables", "describe_table", "list_dashboard_presets", "create_dashboard"]
    assert "https://dashboard.nynahouse.com/d/abc" in C.extract_text(f["supervisor"].calls[0][0].content)
    assert out["designer_done"] and out["coder_done"]


@pytest.mark.asyncio
async def test_worker_step_limit_forces_finish(llms):
    f = llms(
        analyst=[AnalystOutput(question="x", intent="lookup")],
        coder=[], supervisor=[AIMessage(content="xong")],
    )
    f["coder"].script_default = tool_call("list_tables")  # LLM đòi tool mãi
    out = await run(build_graph(ALL_TOOLS), "câu hỏi khó")
    assert out["coder_done"] is True
    from app.agent.models import load_agents_config
    assert out["coder_steps"] == load_agents_config().get("coder").max_steps
    assert out["coder_notes"]  # luôn có ghi chú dù LLM vẫn đòi tool


@pytest.mark.asyncio
async def test_agent_cannot_run_tool_outside_its_list(llms):
    llms(
        analyst=[AnalystOutput(question="x", intent="lookup")],
        coder=[tool_call("create_dashboard", {"title": "x"}), AIMessage(content="không làm được")],
        designer=[], supervisor=[AIMessage(content="ok")],
    )
    out = await run(build_graph(ALL_TOOLS), "tạo dashboard giúp")
    errs = [m for m in out["coder_messages"] if isinstance(m, ToolMessage)]
    assert errs and errs[0].status == "error" and "không được dùng" in errs[0].content


@pytest.mark.asyncio
async def test_tools_arg_restricts_agents(llms):
    from app.agent.tools import TOOLS_BY_AGENT
    only_sql = [t for t in ALL_TOOLS if t.name == "run_select_query"]
    f = llms(analyst=[AnalystOutput(question="x", intent="lookup")], coder=[AIMessage(content="ok")],
             designer=[], supervisor=[AIMessage(content="ok")])
    await run(build_graph(only_sql), "hỏi gì đó")
    assert f["coder"].bound == ["run_select_query"]


@pytest.mark.asyncio
async def test_report_shortcut_skips_all_llms(llms, monkeypatch):
    async def fake_report(user_id, role):
        return "📊 Báo cáo kinh doanh hôm qua\n• Doanh thu: 1 ₫"
    monkeypatch.setattr(router_rules, "build_report_text", fake_report)
    f = llms(analyst=[], coder=[], designer=[], supervisor=[])
    out = await run(build_graph(ALL_TOOLS), "báo cáo hôm nay")
    assert out["messages"][-1].content.startswith("📊 Báo cáo")
    assert not any(x.calls for x in f.values())


@pytest.mark.asyncio
async def test_second_turn_resets_turn_state_and_history_stays_clean(llms):
    f = llms(
        analyst=[AnalystOutput(question="x", intent="lookup")] * 2,
        coder=[tool_call("run_select_query"), AIMessage(content="a"), AIMessage(content="b")],
        designer=[], supervisor=[AIMessage(content="câu 1"), AIMessage(content="câu 2")],
    )
    g = build_graph(ALL_TOOLS, MemorySaver())
    await run(g, "hỏi 1", thread="same")
    out = await run(g, "hỏi 2", thread="same")
    assert [type(m).__name__ for m in out["messages"]] == ["HumanMessage", "AIMessage"] * 2
    assert out["coder_steps"] == 0 and out["coder_messages"] == []  # vòng tool của lượt 1 đã bị xoá
    prev = f["supervisor"].calls[1]  # lời gọi LLM của lượt 2: có lịch sử chữ, không có tool_use
    assert all(not getattr(m, "tool_calls", None) for m in prev)


@pytest.mark.asyncio
async def test_analyst_bad_output_falls_back_to_minimal_brief(llms):
    from langchain_core.exceptions import OutputParserException
    llms(analyst=[], coder=[AIMessage(content="n")], designer=[], supervisor=[AIMessage(content="ok")])
    C.get_llm("analyst").script_default = None  # structured output trả None
    out = await run(build_graph(ALL_TOOLS), "doanh thu")
    assert out["brief"]["intent"] == "lookup" and out["brief"]["question"] == "doanh thu"


@pytest.mark.asyncio
async def test_agents_receive_business_knowledge_and_cache_marker(llms):
    f = llms(
        analyst=[AnalystOutput(question="x", intent="lookup", period_text="từ 19/10/2026 đến 21/10/2026")],
        coder=[AIMessage(content="n")], designer=[], supervisor=[AIMessage(content="ok")],
    )
    await run(build_graph(ALL_TOOLS), "doanh thu 19 đến 21/10")
    coder_sys = f["coder"].calls[0][0]
    assert isinstance(coder_sys.content, list) and coder_sys.content[0]["cache_control"] == {"type": "ephemeral"}
    assert "Quy tắc bắt buộc khi viết SQL" in C.extract_text(coder_sys.content)       # sql_examples.md
    chat_sys = C.extract_text(f["supervisor"].calls[0][0].content)
    assert "Ngày Phụ nữ Việt Nam" in chat_sys                                            # calendar.yaml theo kỳ trong brief
    assert "Chưa rõ" not in chat_sys and "[ĐIỀN" not in chat_sys


@pytest.mark.asyncio
async def test_coder_gets_api_tools_only_when_api_scopes_exist(llms, api_scopes_config):
    f = llms(analyst=[AnalystOutput(question="x", intent="lookup")], coder=[AIMessage(content="n")],
             designer=[], supervisor=[AIMessage(content="ok")])
    await run(build_graph(ALL_TOOLS), "doanh thu house tuần này")
    assert f["coder"].bound == ["list_tables", "describe_table", "run_select_query",
                                "list_business_apis", "call_business_api"]
