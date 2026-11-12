# tests/test_knowledge.py
# Kiểm tra tri thức nghiệp vụ nạp cho agent (app/agent/knowledge/) và các ví dụ SQL trong sql_examples.md.
# Mọi ví dụ SQL phải qua guard.check_select và manager phải xem được bảng của nó, nếu không agent sẽ
# học theo ví dụ sai (và lỗi chỉ lộ ra khi có người hỏi).

import re
from datetime import date

import pytest

from app.agent import knowledge
from app.agent.permissions import can_access
from app.db.guard import check_select

PERSONAL_COLUMNS = ("phone_number", "email", "birthday", "address")  # bị che ở mức database (create_readonly_role.sql)


def _examples() -> list[str]:
    good_part = knowledge.sql_examples().split("## Ví dụ SAI")[0]
    return re.findall(r"```sql\n(.*?)```", good_part, re.S)


def test_loaders_return_text():
    for text in (knowledge.business_context(), knowledge.glossary(), knowledge.sql_examples(), knowledge.sql_rules()):
        assert len(text) > 200


def test_unknown_section_is_not_sent_to_agent():
    text = knowledge.business_context()
    assert "Chưa rõ" not in text and "[ĐIỀN" not in text


def test_glossary_matches_dwh_schema_not_old_orders_table():
    g = knowledge.glossary()
    assert "orders." not in g and "dwh.fact_sales" in g and "is_deleted" in g


def test_sql_rules_are_only_the_rules():
    rules = knowledge.sql_rules()
    assert "Quy tắc bắt buộc" in rules and "## 1. Tổng quan KPI" not in rules


_N_EXAMPLES = len(_examples())  # đếm theo file, thêm/bớt ví dụ không phải sửa test


def test_sql_examples_not_empty():
    assert _N_EXAMPLES >= 10


@pytest.mark.parametrize("n", range(_N_EXAMPLES))
def test_every_sql_example_passes_guard_and_manager_access(n):
    examples = _examples()
    q = check_select(examples[n].strip().rstrip(";"), 200)
    assert all(can_access("manager", t) for t in q.tables)


def test_examples_do_not_select_hidden_personal_columns():
    for sql in _examples():
        for col in PERSONAL_COLUMNS:
            assert not re.search(rf"\b{col}\b", sql), f"ví dụ đọc cột bị che: {col}"


def test_events_overlap_and_flags():
    text = knowledge.events_text(date(2026, 11, 10), date(2026, 11, 12))
    assert "Ngày độc thân 11/11" in text
    tet = knowledge.events_text(date(2027, 2, 5), date(2027, 2, 6), include_previous=False)
    assert "Tết Nguyên đán" in tet and "chưa xác nhận" in tet
    assert knowledge.events_text(date(2026, 3, 1), date(2026, 3, 3)) is None
    assert knowledge.events_text(None, None) is None


def test_events_previous_period_included_only_when_asked():
    # Black Friday 27-29/11; kỳ 1-3/12 có kỳ trước cùng độ dài là 28-30/11
    assert "Black Friday" in knowledge.events_text(date(2026, 12, 1), date(2026, 12, 3))
    assert knowledge.events_text(date(2026, 12, 1), date(2026, 12, 3), include_previous=False) is None


def test_bad_calendar_entries_are_skipped():
    events = knowledge.parse_events({"events": [
        {"name": "ok", "start": "2026-01-01", "end": "2026-01-02"},
        {"name": "đảo ngày", "start": "2026-01-05", "end": "2026-01-01"},
        {"name": "thiếu ngày"},
        {"name": "ngày sai", "start": "không phải ngày", "end": "2026-01-01"},
    ]})
    assert [e.name for e in events] == ["ok"]
    assert knowledge.parse_events(None) == ()