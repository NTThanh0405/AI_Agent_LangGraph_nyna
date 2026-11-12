from app.agent.tools.dashboard_tools import create_dashboard, list_dashboard_presets
from app.agent.tools.explore_tools import describe_table, list_tables
from app.agent.tools.sql_tools import run_select_query

ALL_TOOLS = [list_tables, describe_table, run_select_query, create_dashboard, list_dashboard_presets]

# Tool theo từng agent. graph.py còn lọc thêm theo tên tool nếu build_graph(tools=...) được truyền vào.
TOOLS_BY_AGENT = {
    "coder": [list_tables, describe_table, run_select_query],
    "designer": [list_tables, describe_table, list_dashboard_presets, create_dashboard],
}
# Tên các tool gọi API của khối source=api (house, misa). graph.py bỏ các tool này khi
# config/scopes.yaml chưa có khối api nào. Hiện chưa có api_tools.py nên để rỗng; khi thêm
# app/agent/tools/api_tools.py thì import tool vào đây, gắn vào TOOLS_BY_AGENT và điền tên vào set này.
API_TOOL_NAMES: set[str] = set()