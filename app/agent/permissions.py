# app/agent/permissions.py
# Phân quyền xem dữ liệu theo vai trò (user_role): mỗi vai trò được xem những bảng nào.
# Vai trò lạ hoặc để trống thì không được xem bảng nào.
# Cần khớp với quyền GRANT SELECT của role PostgreSQL trong sql/create_readonly_role.sql.

from typing import Iterable, Optional

# Khoá: vai trò (chữ thường). Giá trị: tập bảng dạng "schema.table";
# "*" là mọi bảng, "schema.*" là mọi bảng trong schema đó.
# SỬA theo database thật của bạn; các bảng dưới đây chỉ là ví dụ.
ROLE_TABLES: dict[str, set[str]] = {
    "boss": {"*"},
    "manager": {"public.orders", "public.customers", "public.products"},
}


class PermissionDenied(Exception):
    """Vai trò hiện tại không được xem bảng được yêu cầu."""


def can_access(role: Optional[str], table: str) -> bool:
    """Kiểm tra vai trò có được xem bảng "schema.table" không."""
    patterns = ROLE_TABLES.get((role or "").lower(), set())
    table = table.lower()
    schema = table.split(".", 1)[0]
    return "*" in patterns or table in patterns or f"{schema}.*" in patterns


def filter_tables(role: Optional[str], tables: Iterable[str]) -> list[str]:
    """Giữ lại những bảng mà vai trò được phép xem."""
    return [t for t in tables if can_access(role, t)]


def ensure_access(role: Optional[str], tables: Iterable[str]) -> None:
    """Raise PermissionDenied nếu có bảng nào vai trò không được xem."""
    denied = sorted(t for t in tables if not can_access(role, t))
    if denied:
        raise PermissionDenied(
            "Không có quyền truy cập các bảng: " + ", ".join(denied)
        )