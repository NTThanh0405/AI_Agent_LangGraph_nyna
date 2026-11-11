from app.agent.tools.analysis_tools import analyze_trend, compare_periods, explain_change
from app.agent.tools.context_tools import get_business_knowledge, get_business_targets, get_period_events
from app.agent.tools.dashboard_tools import create_dashboard
from app.agent.tools.snapshot_tools import business_snapshot
from app.agent.tools.sql_tools import run_select_query
from app.agent.tools.explore_tools import describe_table, list_tables

ALL_TOOLS = [list_tables, describe_table, run_select_query, create_dashboard]

# Tool của Analyst (chỉ ĐỌC, dùng file SQL cố định + config). Hiện analyst_node chưa chạy vòng tool
# (chỉ with_structured_output) và graph.py chưa dựng tool node cho analyst, nên danh sách này chưa được gọi;
# muốn dùng thì phải nối vào graph (hoặc đưa vào tool của coder).
ANALYST_TOOLS = [
    business_snapshot, compare_periods, analyze_trend, explain_change,
    get_business_knowledge, get_period_events, get_business_targets,
]

# Tool theo từng agent. graph.py còn lọc thêm theo tên tool nếu build_graph(tools=...) được truyền vào.
TOOLS_BY_AGENT = {
    "analyst": ANALYST_TOOLS,
    "coder": [list_tables, describe_table, run_select_query],
    "designer": [list_tables, describe_table, create_dashboard],
}