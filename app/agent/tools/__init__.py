from app.agent.tools.dashboard_tools import create_dashboard, list_dashboard_presets
from app.agent.tools.explore_tools import describe_table, list_tables
from app.agent.tools.sql_tools import run_select_query

ALL_TOOLS = [list_tables, describe_table, run_select_query, create_dashboard, list_dashboard_presets]

# Tool theo từng agent. graph.py còn lọc thêm theo tên tool nếu build_graph(tools=...) được truyền vào.
TOOLS_BY_AGENT = {
    "coder": [list_tables, describe_table, run_select_query],
    "designer": [list_tables, describe_table, list_dashboard_presets, create_dashboard],
}