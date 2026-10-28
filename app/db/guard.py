# app/db/guard.py
# Kiểm tra câu SQL do LLM sinh ra trước khi chạy: chỉ cho đúng một câu SELECT,
# chặn lệnh ghi/DDL, hàm nguy hiểm và bảng hệ thống, rồi trả về SQL đã chuẩn hoá
# (có giới hạn số dòng) cùng danh sách bảng được tham chiếu để kiểm tra quyền.
# Đây chỉ là lớp bảo vệ phụ; lớp chính là role chỉ-đọc (sql/create_readonly_role.sql).

from dataclasses import dataclass

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError


class UnsafeSQLError(ValueError):
    """Câu SQL không được phép chạy."""


@dataclass(frozen=True)
class CheckedQuery:
    sql: str            # SQL đã chuẩn hoá, bọc LIMIT, sẵn sàng để chạy
    tables: frozenset   # các bảng được dùng, dạng "schema.table" (chữ thường)


def _node_types(names: list[str]) -> tuple:
    # Bỏ qua tên không có trong phiên bản sqlglot đang cài
    return tuple(getattr(exp, n) for n in names if hasattr(exp, n))


# Loại node cấm xuất hiện ở bất kỳ đâu trong câu lệnh (kể cả trong CTE/subquery)
_FORBIDDEN_NODES = _node_types(
    [
        "Insert", "Update", "Delete", "Drop", "Create", "Alter", "AlterTable",
        "Merge", "Command", "Into", "Lock", "Copy", "TruncateTable", "Set",
        "Grant", "Transaction", "Commit", "Rollback",
    ]
)

# Hàm bị chặn theo tên đầy đủ hoặc tiền tố (có thể đọc file, ngủ, chạy SQL gián tiếp...)
_BLOCKED_FUNCTION_PREFIXES = ("pg_", "lo_", "dblink", "txid_")
_BLOCKED_FUNCTIONS = {"set_config", "nextval", "setval", "currval"}
_BLOCKED_FUNCTION_PARTS = ("to_xml",)  # query_to_xml, table_to_xml... chạy SQL dạng chuỗi

_SYSTEM_SCHEMAS = {"pg_catalog", "information_schema"}


def _function_name(fn: exp.Expression) -> str:
    name = fn.name if isinstance(fn, exp.Anonymous) else fn.sql_name()
    return (name or "").lower()


def _with_of(node: exp.Expression):
    """Lấy mệnh đề WITH của một node (tên arg khác nhau giữa các bản sqlglot)."""
    w = node.args.get("with") or node.args.get("with_")
    return w if isinstance(w, exp.With) else None


def _visible_cte_names(table: exp.Table) -> set[str]:
    """Tên các CTE thực sự nhìn thấy được tại vị trí của `table`.

    Quy tắc giống PostgreSQL:
    - Phần thân câu lệnh (và subquery bên trong) thấy mọi CTE của WITH bao quanh.
    - Bên trong định nghĩa của một CTE chỉ thấy các CTE khai báo TRƯỚC nó;
      tên chính nó chỉ hợp lệ khi WITH RECURSIVE.
    Nhờ vậy `WITH salaries AS (SELECT * FROM salaries) ...` vẫn tính
    `salaries` bên trong là bảng thật và phải qua kiểm tra quyền.
    """
    visible: set[str] = set()
    child: exp.Expression = table
    parent = table.parent
    while parent is not None:
        if isinstance(parent, exp.With):
            # child là một CTE nằm trong WITH này
            if parent.args.get("recursive"):
                visible.update(c.alias.lower() for c in parent.expressions)
            else:
                for cte in parent.expressions:
                    if cte is child:
                        break
                    visible.add(cte.alias.lower())
        else:
            w = _with_of(parent)
            # Nếu child chính là WITH thì đã xử lý ở nhánh trên, bỏ qua
            if w is not None and child is not w:
                visible.update(c.alias.lower() for c in w.expressions)
        child, parent = parent, parent.parent
    return visible


def check_select(sql: str, max_rows: int) -> CheckedQuery:
    """Kiểm tra một câu SQL; trả về CheckedQuery hoặc raise UnsafeSQLError."""
    try:
        statements = [s for s in sqlglot.parse(sql, read="postgres") if s is not None]
    except SqlglotError as e:
        raise UnsafeSQLError(f"SQL không hợp lệ: {str(e).splitlines()[0]}") from e

    if len(statements) != 1:
        raise UnsafeSQLError("Chỉ được gửi đúng một câu lệnh SQL mỗi lần")

    stmt = statements[0]
    if not isinstance(stmt, (exp.Select, exp.Union)):
        raise UnsafeSQLError("Chỉ được phép dùng câu lệnh SELECT")

    if stmt.find(*_FORBIDDEN_NODES):
        raise UnsafeSQLError("Câu lệnh chứa thao tác ghi hoặc thay đổi dữ liệu")

    for fn in stmt.find_all(exp.Func):
        name = _function_name(fn)
        if (
            name in _BLOCKED_FUNCTIONS
            or name.startswith(_BLOCKED_FUNCTION_PREFIXES)
            or any(part in name for part in _BLOCKED_FUNCTION_PARTS)
        ):
            raise UnsafeSQLError(f"Không được dùng hàm '{name}'")

    tables = set()
    for t in stmt.find_all(exp.Table):
        name = t.name.lower()
        if not name:  # ví dụ FROM generate_series(...)
            continue
        # Tên CTE đang nhìn thấy được tại đây thì không phải bảng thật
        if not t.db and name in _visible_cte_names(t):
            continue
        schema = (t.db or "public").lower()
        if t.catalog:
            raise UnsafeSQLError("Không được truy vấn chéo database")
        if schema in _SYSTEM_SCHEMAS or schema.startswith("pg_") or name.startswith("pg_"):
            raise UnsafeSQLError("Không được truy vấn bảng hệ thống")
        tables.add(f"{schema}.{name}")

    # Chạy lại đúng câu SQL đã phân tích (đã bỏ comment), bọc LIMIT + 1
    # để biết kết quả có bị cắt bớt hay không.
    normalized = stmt.sql(dialect="postgres")
    limited = f"SELECT * FROM ({normalized}) AS _agent_q LIMIT {int(max_rows) + 1}"
    return CheckedQuery(sql=limited, tables=frozenset(tables))