from app.agent.tools.dashboard_tools import create_dashboard
from app.agent.tools.sql_tools import run_select_query
from app.agent.tools.explore_tools import describe_table, list_tables

ALL_TOOLS = [list_tables, describe_table, run_select_query, create_dashboard]