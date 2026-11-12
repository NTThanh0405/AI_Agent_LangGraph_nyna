# tests/test_coder_tools.py
# Coder được gắn các tool phân tích/tri thức viết sẵn; "cùng kỳ ..." không bị đoán thành cả năm.

from datetime import date

import pytest

from app.agent.graph import _tools_for
from app.agent.periods import PeriodError, resolve_period
from app.agent.tools import ALL_TOOLS, TOOLS_BY_AGENT

CODER_EXTRA = {"get_sample_rows", "business_snapshot", "compare_periods", "analyze_trend", "explain_change",
               "get_date_range", "get_period_events", "get_business_targets", "get_business_knowledge"}


def test_tool_names_are_unique():
    names = [t.name for t in ALL_TOOLS]
    assert len(names) == len(set(names))


def test_coder_gets_analysis_and_context_tools():
    coder = {t.name for t in TOOLS_BY_AGENT["coder"]}
    assert CODER_EXTRA <= coder
    # main.py dựng graph bằng build_graph(ALL_TOOLS): tool phải nằm trong ALL_TOOLS mới tới được agent
    assert CODER_EXTRA <= {t.name for t in _tools_for("coder", {t.name for t in ALL_TOOLS})}


def test_designer_does_not_get_data_analysis_tools():
    assert not (CODER_EXTRA - {"get_sample_rows"}) & {t.name for t in TOOLS_BY_AGENT["designer"]}


@pytest.mark.parametrize("text", ["cùng kỳ năm ngoái", "Cùng kỳ tháng trước", "cung ky nam truoc"])
def test_same_period_phrase_is_not_guessed(text):
    with pytest.raises(PeriodError, match="khoảng ngày cụ thể"):
        resolve_period(text, today=date(2026, 11, 12))


def test_explicit_range_used_by_coder_still_works():
    p = resolve_period("từ 02/11/2026 đến 08/11/2026", today=date(2026, 11, 12))
    assert (p.start, p.end) == (date(2026, 11, 2), date(2026, 11, 8))
    p = resolve_period("năm ngoái", today=date(2026, 11, 12))      # "năm ngoái" đứng riêng vẫn là cả năm
    assert (p.start, p.end) == (date(2025, 1, 1), date(2025, 12, 31))
