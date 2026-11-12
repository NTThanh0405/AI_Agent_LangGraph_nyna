from app.agent.tools.analysis_tools import analyze_trend, compare_periods, explain_change
from app.agent.tools.context_tools import get_business_knowledge, get_business_targets, get_period_events
from app.agent.tools.dashboard_tools import create_dashboard, list_dashboard_presets
from app.agent.tools.explore_tools import describe_table, get_sample_rows, list_tables
from app.agent.tools.period_tools import get_date_range
from app.agent.tools.snapshot_tools import business_snapshot
from app.agent.tools.sql_tools import run_select_query

# Tool phân tích dùng SQL viết sẵn trong app/agent/queries/ (cùng định nghĩa với báo cáo sáng) và tool tri thức.
# Tool đọc database đều kiểm tra quyền của người hỏi (ensure_access_user); tool tri thức không chạm database.
ANALYSIS_TOOLS = [business_snapshot, compare_periods, analyze_trend, explain_change]
CONTEXT_TOOLS = [get_date_range, get_period_events, get_business_targets, get_business_knowledge]

ALL_TOOLS = [
    list_tables, describe_table, get_sample_rows, run_select_query,
    *ANALYSIS_TOOLS, *CONTEXT_TOOLS,
    create_dashboard, list_dashboard_presets,
]

# Tool theo từng agent. graph.py còn lọc thêm theo tên tool nếu build_graph(tools=...) được truyền vào.
TOOLS_BY_AGENT = {
    "coder": [list_tables, describe_table, get_sample_rows, run_select_query, *ANALYSIS_TOOLS, *CONTEXT_TOOLS],
    "designer": [list_tables, describe_table, list_dashboard_presets, create_dashboard],
}
# Tên các tool gọi API của khối source=api (house, misa). graph.py bỏ các tool này khi
# config/scopes.yaml chưa có khối api nào. Hiện chưa có api_tools.py nên để rỗng; khi thêm
# app/agent/tools/api_tools.py thì import tool vào đây, gắn vào TOOLS_BY_AGENT và điền tên vào set này.
API_TOOL_NAMES: set[str] = set()