# tests/test_guard.py
# Kiểm tra guard.check_select: chỉ cho một câu SELECT an toàn, chặn mọi thứ còn lại.
# Chỉ phân tích chuỗi SQL, KHÔNG kết nối hay ghi vào database.

import pytest

from app.db.guard import CheckedQuery, UnsafeSQLError, check_select, check_template


def test_unsafe_error_is_value_error():
    assert issubclass(UnsafeSQLError, ValueError)


# ---------------------------------------------------------------- câu hợp lệ

def test_simple_select_ok():
    q = check_select("SELECT * FROM fact_sales", 100)
    assert isinstance(q, CheckedQuery)
    assert q.tables == frozenset({"dwh.fact_sales"})


def test_limit_is_max_rows_plus_one():
    q = check_select("SELECT * FROM fact_sales", 50)
    assert q.sql.rstrip().endswith("LIMIT 51")
    assert q.sql.startswith("SELECT * FROM (")


def test_default_schema_written_into_sql():
    q = check_select("SELECT * FROM fact_sales", 10)
    assert "dwh.fact_sales" in q.sql.lower()


def test_explicit_schema_kept():
    q = check_select("SELECT * FROM dwh.fact_sales", 10)
    assert q.tables == frozenset({"dwh.fact_sales"})


def test_table_names_are_lowercased():
    q = check_select("SELECT * FROM DWH.Fact_Sales", 10)
    assert q.tables == frozenset({"dwh.fact_sales"})


def test_join_returns_all_tables():
    q = check_select(
        "SELECT s.tran_ts FROM fact_sales s JOIN dim_store d ON s.store_key = d.store_key",
        10,
    )
    assert q.tables == frozenset({"dwh.fact_sales", "dwh.dim_store"})


def test_subquery_tables_detected():
    q = check_select("SELECT * FROM (SELECT * FROM fact_sales) x", 10)
    assert q.tables == frozenset({"dwh.fact_sales"})


def test_union_allowed_and_tables_detected():
    q = check_select(
        "SELECT store_key FROM fact_sales UNION SELECT store_key FROM fact_online_orders",
        10,
    )
    assert q.tables == frozenset({"dwh.fact_sales", "dwh.fact_online_orders"})


def test_select_without_table_has_no_tables():
    q = check_select("SELECT 1", 10)
    assert q.tables == frozenset()


def test_cte_name_is_not_counted_as_table():
    q = check_select(
        "WITH a AS (SELECT * FROM fact_sales) SELECT * FROM a",
        10,
    )
    assert q.tables == frozenset({"dwh.fact_sales"})


def test_cte_shadowing_real_table_still_reports_real_table():
    # Bên trong định nghĩa CTE, "salaries" là bảng thật => phải được báo để kiểm tra quyền.
    q = check_select(
        "WITH salaries AS (SELECT * FROM salaries) SELECT * FROM salaries",
        10,
    )
    assert "dwh.salaries" in q.tables


# ------------------------------------------------------------ câu bị chặn

@pytest.mark.parametrize(
    "sql",
    [
        "DROP TABLE dwh.fact_sales",
        "DELETE FROM dwh.fact_sales",
        "UPDATE dwh.fact_sales SET amount = 0",
        "INSERT INTO dwh.fact_sales (sale_id) VALUES ('x')",
        "TRUNCATE dwh.fact_sales",
        "CREATE TABLE x (id int)",
        "ALTER TABLE dwh.fact_sales ADD COLUMN x int",
        "GRANT ALL ON dwh.fact_sales TO public",
        "SET statement_timeout = 0",
    ],
)
def test_blocks_non_select(sql):
    with pytest.raises(UnsafeSQLError):
        check_select(sql, 100)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1; SELECT 2",
        "SELECT 1; DROP TABLE dwh.fact_sales",
        "SELECT * FROM fact_sales; DELETE FROM fact_sales",
    ],
)
def test_blocks_multiple_statements(sql):
    with pytest.raises(UnsafeSQLError):
        check_select(sql, 100)


@pytest.mark.parametrize("sql", ["", "   ", ";"])
def test_blocks_empty(sql):
    with pytest.raises(UnsafeSQLError):
        check_select(sql, 100)


def test_blocks_unparseable_sql():
    with pytest.raises(UnsafeSQLError):
        check_select("SELECT FROM WHERE ((", 100)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT pg_sleep(100)",
        "SELECT pg_read_file('/etc/passwd')",
        "SELECT lo_import('/etc/passwd')",
        "SELECT set_config('statement_timeout', '0', false)",
        "SELECT nextval('some_seq')",
        "SELECT setval('some_seq', 1)",
        "SELECT query_to_xml('select * from dwh.dim_customer', true, false, '')",
    ],
)
def test_blocks_dangerous_functions(sql):
    with pytest.raises(UnsafeSQLError):
        check_select(sql, 100)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM information_schema.tables",
        "SELECT * FROM pg_catalog.pg_tables",
        "SELECT * FROM pg_user",
        "SELECT * FROM pg_catalog.pg_roles",
    ],
)
def test_blocks_system_tables(sql):
    with pytest.raises(UnsafeSQLError):
        check_select(sql, 100)


def test_blocks_cross_database_query():
    with pytest.raises(UnsafeSQLError):
        check_select("SELECT * FROM otherdb.dwh.fact_sales", 100)


def test_blocks_select_into():
    with pytest.raises(UnsafeSQLError):
        check_select("SELECT * INTO newtable FROM fact_sales", 100)


def test_blocks_select_for_update():
    with pytest.raises(UnsafeSQLError):
        check_select("SELECT * FROM fact_sales FOR UPDATE", 100)

# ================================================= check_template (file SQL viết sẵn trong queries/)

PARAMS = frozenset({"start", "end", "end_exclusive"})
TPL = "SELECT SUM(final_amount) AS revenue FROM fact_sales WHERE tran_date >= %(start)s AND tran_date < %(end_exclusive)s"


def test_template_keeps_named_params_and_adds_schema():
    q = check_template(TPL, allowed_params=PARAMS)
    assert q.tables == frozenset({"dwh.fact_sales"})
    assert "%(start)s" in q.sql and "%(end_exclusive)s" in q.sql
    assert "dwh.fact_sales" in q.sql.lower()


def test_template_is_not_wrapped_with_limit():
    assert "LIMIT" not in check_template(TPL, allowed_params=PARAMS).sql.upper()


def test_template_own_limit_is_kept():
    q = check_template("SELECT item_id FROM dim_item ORDER BY item_id LIMIT 20", allowed_params=PARAMS)
    assert q.sql.rstrip().endswith("LIMIT 20")


def test_template_rejects_unknown_param():
    with pytest.raises(UnsafeSQLError):
        check_template("SELECT 1 FROM fact_sales WHERE a = %(evil)s", allowed_params=PARAMS)


@pytest.mark.parametrize("sql", [
    "DELETE FROM fact_sales WHERE tran_date >= %(start)s",
    "SELECT 1; DROP TABLE fact_sales",
    "SELECT pg_sleep(100)",
    "SELECT * FROM pg_catalog.pg_tables",
    "SELECT * FROM fact_sales FOR UPDATE",
    "",
])
def test_template_blocks_the_same_dangerous_sql_as_check_select(sql):
    with pytest.raises(UnsafeSQLError):
        check_template(sql, allowed_params=PARAMS)


def test_template_literal_percent_must_be_doubled():
    q = check_template("SELECT 1 FROM dim_item WHERE item_name LIKE 'a%%'", allowed_params=PARAMS)
    assert "%%" in q.sql


def test_template_join_and_cte_tables_detected():
    q = check_template(
        "WITH s AS (SELECT store_key FROM fact_sales WHERE tran_date >= %(start)s) "
        "SELECT d.store_name FROM s JOIN dim_store d ON d.store_key = s.store_key", allowed_params=PARAMS)
    assert q.tables == frozenset({"dwh.fact_sales", "dwh.dim_store"})
