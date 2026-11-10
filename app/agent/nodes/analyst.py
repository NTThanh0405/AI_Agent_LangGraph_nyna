# app/agent/nodes/analyst.py
# Analyst: hiểu câu hỏi -> AnalysisBrief (không truy vấn, không trả lời người dùng).
# Cụm thời gian do LLM chép lại (period_text) rồi được đổi thành ngày bằng periods.resolve_period,
# nên khoảng ngày luôn giống đường tắt báo cáo và các lệnh /tuan /thang (LLM không tự tính ngày).

import logging
from typing import Optional

from langchain_core.exceptions import OutputParserException
from langchain_core.runnables import RunnableConfig

from app.agent import knowledge
from app.agent.contracts import AnalysisBrief
from app.agent.nodes import _common as C
from app.agent.periods import PeriodError, resolve_period

logger = logging.getLogger(__name__)


class AnalystOutput(AnalysisBrief):
    """Cái LLM điền: AnalysisBrief + cụm thời gian nguyên văn."""

    period_text: Optional[str] = None


def build_brief(out: Optional[AnalystOutput], question: str) -> AnalysisBrief:
    q = (question or "").strip()[:2000] or "(trống)"
    if out is None:
        return AnalysisBrief(question=q, intent="lookup")
    data = out.model_dump()
    period_text = data.pop("period_text", None)
    data["question"] = q  # luôn giữ nguyên câu hỏi gốc
    data["preferred_query"] = None
    if period_text and period_text.strip():
        try:
            p = resolve_period(period_text)
            data["period_start"], data["period_end"] = p.start, p.end
        except PeriodError:
            logger.info("Analyst: không đổi được cụm thời gian %r, giữ ngày LLM đưa (nếu có)", period_text)
    if (data.get("period_start") is None) != (data.get("period_end") is None):
        data["period_start"] = data["period_end"] = None
    try:
        return AnalysisBrief(**data)
    except ValueError:
        return AnalysisBrief(question=q, intent=data.get("intent") or "lookup")


async def analyst_node(state: dict, config: RunnableConfig) -> dict:
    question = C.last_human_text(state["messages"])
    system = C.system_message("analyst", config, [knowledge.business_context(), knowledge.glossary()])
    llm = C.get_llm("analyst").with_structured_output(AnalystOutput)
    out = None
    try:
        out = await llm.ainvoke([system, *C.plain_history(state["messages"])])
    except (OutputParserException, ValueError) as e:  # lỗi định dạng đầu ra -> làm tiếp với brief tối thiểu
        logger.warning("Analyst trả dữ liệu sai dạng, dùng brief tối thiểu: %s", e)
    if out is not None and not isinstance(out, AnalystOutput):
        out = None
    brief = build_brief(out, question)
    return {"brief": brief.model_dump(mode="json")}
