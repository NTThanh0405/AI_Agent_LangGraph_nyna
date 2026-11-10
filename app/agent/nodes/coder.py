# app/agent/nodes/coder.py
# Coder: viết và chạy SQL (list_tables, describe_table, run_select_query). Quyền đọc bảng theo role
# do chính các tool kiểm tra (permissions.py + guard.py), node này không tự nới quyền.
from app.agent import knowledge
from app.agent.nodes._worker import make_worker_node
from app.agent.tools import TOOLS_BY_AGENT


def make_coder_node(tools=None):
    return make_worker_node(
        "coder", TOOLS_BY_AGENT["coder"] if tools is None else tools,
        loop_key="coder_messages", steps_key="coder_steps", done_key="coder_done", notes_key="coder_notes",
        docs=lambda: [knowledge.glossary(), knowledge.sql_examples()],
    )
