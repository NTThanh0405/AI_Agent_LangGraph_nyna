# app/agent/permissions.py
# Phân quyền xem dữ liệu theo vai trò (user_role): mỗi vai trò được xem những bảng nào.
# Vai trò lạ hoặc để trống bị từ chối hoàn toàn, kể cả câu SELECT không đụng bảng nào.
# Đây là lớp phân quyền theo vai trò duy nhất; PostgreSQL chỉ có một role chỉ-đọc dùng
# chung (agent_readonly) và che cột cá nhân của khách (sql/create_readonly_role.sql).

from typing import Iterable, Optional

# Khoá: vai trò (chữ thường). Giá trị: tập bảng dạng "schema.table";
# "*" là mọi bảng, "schema.*" là mọi bảng trong schema đó.
# Danh sách của manager là giả định (bán hàng, đơn online, món, cửa hàng, khuyến mãi,
# kèm dim_customer đã che cột cá nhân); chưa cho xem hội viên và voucher. Sửa theo nhu cầu.
ROLE_TABLES: dict[str, set[str]] = {
    "boss": {"dwh.*"},
    "manager": {
        "dwh.dim_store",
        "dwh.dim_employee",
        "dwh.dim_item",
        "dwh.dim_channel",
        "dwh.dim_promotion",
        "dwh.dim_membership_type",
        "dwh.dim_customer",
        "dwh.fact_sales",
        "dwh.fact_sale_items",
        "dwh.fact_sale_payments",
        "dwh.fact_online_orders",
        "dwh.fact_online_order_items",
        "dwh.fact_order_status_log",
        "dwh.fact_item_stock_events",
    },
}


class PermissionDenied(Exception):
    """Vai trò hiện tại không được xem bảng được yêu cầu."""


def is_known_role(role: Optional[str]) -> bool:
    return (role or "").lower() in ROLE_TABLES


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
    """Raise PermissionDenied nếu vai trò không hợp lệ hoặc có bảng không được xem."""
    if not is_known_role(role):
        raise PermissionDenied("Vai trò không hợp lệ hoặc chưa được cấp quyền")
    denied = sorted(t for t in tables if not can_access(role, t))
    if denied:
        raise PermissionDenied(
            "Không có quyền truy cập các bảng: " + ", ".join(denied)
        )