# tests/test_reviewer.py: reviewer (tuỳ chọn) — kiểm tra số bằng code và chạy trong graph với LLM giả.
import os

for k, v in {"ANTHROPIC_API_KEY": "x", "TELEGRAM_BOT_TOKEN": "x", "DB_PASSWORD": "x"}.items():
    os.environ.setdefault(k, v)

import pytest
import yaml
from langchain_core.messages import AIMessage, HumanMessage

from app.agent import graph as graph_mod
from app.agent.models import parse_agents_config
from app.agent.nodes import _common as C
from app.agent.nodes import reviewer as R
from app.agent.nodes import supervisor as S
from app.agent.nodes.analyst import AnalystOutput
from app.agent.state import new_turn_state
from app.agent.tools import ALL_TOOLS

DATA = "Doanh thu: 41.488.581 ₫ | ▼ 13,9% so với kỳ trước (48.193.068 ₫). Số đơn: 395 (kỳ trước 454)."


# ---------------------------------------------------------------- kiểm tra số bằng code

@pytest.mark.parametrize("answer", [
    "Doanh thu 41,5 triệu ₫, giảm 13,9%, 395 đơn.",            # rút gọn
    "Doanh thu 41.488.581 ₫ so với 48.193.068 ₫.",             # đầy đủ
    "Giảm 6,7 triệu ₫ so với kỳ trước.",                       # chênh lệch tính ra
    "AOV khoảng 105k.",                                        # thương: doanh thu / số đơn
    "Doanh thu 41.49M.",                                       # dạng M
    "Báo cáo 07:00 ngày 12/11/2026, top 5 món, link https://x.test/ab12.html hết hạn sau 24 giờ.",
])
def test_supported_numbers_pass(answer):
    assert R.unsupported_numbers(answer, DATA, [24]) == []


def test_made_up_numbers_are_caught():
    assert R.unsupported_numbers("Doanh thu 52,3 triệu ₫, tăng 8,2%, 512 đơn.", DATA) == ["52,3 triệu", "8,2%", "512"]


# ---------------------------------------------------------------- trong graph

class FakeLLM:
    def __init__(self, script):
        self.script, self.calls = list(script), []

    def bind_tools(self, tools):
        return self

    def with_structured_output(self, schema):
        outer = self

        class _S:
            async def ainvoke(self, msgs):
                return outer._next(msgs)
        return _S()

    def _next(self, msgs):
        self.calls.append(msgs)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def ainvoke(self, msgs):
        return self._next(msgs)


def _cfg(enabled: bool, **opts):
    data = yaml.safe_load(open("config/agents.yaml", encoding="utf-8"))
    data["agents"]["reviewer"].update({"enabled": enabled, **opts})
    return parse_agents_config(data)


@pytest.fixture
def setup(monkeypatch):
    def install(enabled=True, answer="Doanh thu tuần trước 41,5 triệu ₫.", reviewer=(), intent="lookup", **opts):
        cfg = _cfg(enabled, **opts)
        for mod in (graph_mod, R, S):
            monkeypatch.setattr(mod, "load_agents_config", lambda *a, **k: cfg)
        fakes = {
            "analyst": FakeLLM([AnalystOutput(question="x", intent=intent, metrics=["revenue"])]),
            "coder": FakeLLM([AIMessage(content=f"SQL: select ... {DATA}")]),
            "designer": FakeLLM([]),
            "supervisor": FakeLLM([AIMessage(content=answer)]),
            "reviewer": FakeLLM(list(reviewer)),
        }
        monkeypatch.setattr(C, "get_llm", lambda agent: fakes[agent])
        return fakes
    return install


async def run(text="doanh thu tuần trước?"):
    g = graph_mod.build_graph(ALL_TOOLS)
    cfg = {"configurable": {"thread_id": "t", "user_id": "1", "user_role": "boss"}, "recursion_limit": 60}
    return g, await g.ainvoke(new_turn_state("1", "boss", HumanMessage(content=text)), cfg), cfg


@pytest.mark.asyncio
async def test_disabled_reviewer_is_not_in_graph(setup):
    f = setup(enabled=False, answer="Doanh thu 99 triệu ₫.")
    g, out, _ = await run()
    assert "reviewer" not in g.get_graph().nodes
    assert out["messages"][-1].content == "Doanh thu 99 triệu ₫." and not f["reviewer"].calls


@pytest.mark.asyncio
async def test_correct_answer_needs_no_llm(setup):
    f = setup()
    _, out, _ = await run()
    assert out["messages"][-1].content == "Doanh thu tuần trước 41,5 triệu ₫."
    assert not f["reviewer"].calls                         # lớp 1 đủ, không tốn lượt gọi LLM


@pytest.mark.asyncio
async def test_wrong_number_is_replaced_by_valid_revision(setup):
    fixed = "Doanh thu tuần trước 41,5 triệu ₫, giảm 13,9%."
    f = setup(answer="Doanh thu tuần trước 52 triệu ₫.",
              reviewer=[R.ReviewResult(ok=False, issues=["sai doanh thu"], revised_answer=fixed)])
    _, out, _ = await run()
    assert out["messages"][-1].content == fixed
    assert [type(m).__name__ for m in out["messages"]] == ["HumanMessage", "AIMessage"]  # thay, không thêm tin
    assert f["reviewer"].calls


@pytest.mark.asyncio
async def test_revision_with_new_made_up_number_is_rejected(setup):
    setup(answer="Doanh thu tuần trước 52 triệu ₫.",
          reviewer=[R.ReviewResult(ok=False, revised_answer="Doanh thu tuần trước 60 triệu ₫.")])
    _, out, _ = await run()
    text = out["messages"][-1].content
    assert text.startswith("Doanh thu tuần trước 52 triệu ₫.") and "Lưu ý" in text and "52 triệu" in text


@pytest.mark.asyncio
async def test_llm_confirms_derived_number(setup):
    setup(answer="Doanh thu tuần trước 52 triệu ₫.", reviewer=[R.ReviewResult(ok=True)])
    _, out, _ = await run()
    assert out["messages"][-1].content == "Doanh thu tuần trước 52 triệu ₫."


@pytest.mark.asyncio
async def test_reviewer_error_never_blocks_answer(setup):
    setup(answer="Doanh thu tuần trước 52 triệu ₫.", reviewer=[RuntimeError("API sập")])
    _, out, _ = await run()
    assert out["messages"][-1].content.startswith("Doanh thu tuần trước 52 triệu ₫.")


@pytest.mark.asyncio
async def test_explain_change_is_always_reviewed_by_llm(setup):
    f = setup(intent="explain_change", reviewer=[R.ReviewResult(ok=True)])
    await run("vì sao doanh thu giảm?")
    assert f["reviewer"].calls


@pytest.mark.asyncio
async def test_add_note_false_keeps_answer_untouched(setup):
    setup(answer="Doanh thu tuần trước 52 triệu ₫.", reviewer=[R.ReviewResult(ok=False)], add_note=False)
    _, out, _ = await run()
    assert out["messages"][-1].content == "Doanh thu tuần trước 52 triệu ₫."


@pytest.mark.asyncio
async def test_next_turn_after_state_update_as_chat_still_runs(setup):
    """handlers.py (/reply) ghi state bằng aupdate_state(as_node="chat"); lượt sau vẫn phải chạy bình thường."""
    f = setup()
    g, _, cfg = await run()
    await g.aupdate_state(cfg, {"messages": [AIMessage(content="📊 Báo cáo hôm qua: 41.488.581 ₫")]}, as_node="chat")
    f["analyst"].script.append(AnalystOutput(question="y", intent="lookup"))
    f["coder"].script.append(AIMessage(content=DATA))
    f["supervisor"].script.append(AIMessage(content="Số đơn là 395."))
    out = await g.ainvoke(new_turn_state("1", "boss", HumanMessage(content="số đơn?")), cfg)
    assert out["messages"][-1].content == "Số đơn là 395."
