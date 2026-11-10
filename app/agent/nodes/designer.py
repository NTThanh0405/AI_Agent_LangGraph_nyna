# app/agent/nodes/designer.py
# Designer: dựng dashboard/biểu đồ (list_tables, describe_table, create_dashboard).
# Nhận thêm ghi chú của Coder (SQL + số liệu) để dùng lại, không truy vấn trùng.
from app.agent import knowledge
from app.agent.nodes._worker import make_worker_node
from app.agent.tools import TOOLS_BY_AGENT


def _coder_context(state: dict) -> list:
    notes = state.get("coder_notes")
    return [f"Ghi chú của Coder (có thể dùng lại SQL và số liệu):\n{notes}" if notes else None]


def make_designer_node(tools=None):
    return make_worker_node(
        "designer", TOOLS_BY_AGENT["designer"] if tools is None else tools,
        loop_key="designer_messages", steps_key="designer_steps", done_key="designer_done",
        notes_key="designer_notes", extra_sections=_coder_context,
        docs=lambda: [knowledge.glossary(), knowledge.sql_rules()],
    )
