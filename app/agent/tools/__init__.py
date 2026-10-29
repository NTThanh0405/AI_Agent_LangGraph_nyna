from app.agent.tools.dashboard_tools import create_dashboard
from app.agent.tools.sql_tools import describe_table, list_tables, run_select_query

ALL_TOOLS = [list_tables, describe_table, run_select_query, create_dashboard]