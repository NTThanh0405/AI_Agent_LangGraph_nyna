# app/agent/permissions.py
# Phân quyền xem dữ liệu theo vai trò (user_role): mỗi vai trò được xem những bảng nào.
# Vai trò lạ hoặc để trống bị từ chối hoàn toàn, kể cả câu SELECT không đụng bảng nào.
# Riêng vai trò staff: mặc định không xem bảng nào, chỉ xem bảng của các KHỐI dữ liệu được cấp
# (khối source=postgres trong config/scopes.yaml, ví dụ coffee -> dwh.*; quyền cấp lưu ở
# agent_state.scope_grants, xem app/agent/grants.py và app/agent/scopes.py).
# Khối source=api (house, misa) không có bảng; quyền gọi API do app/agent/tools/api_tools.py kiểm tra.
# PostgreSQL chỉ có một role chỉ-đọc dùng chung (agent_readonly) và che cột cá nhân của
# khách (sql/create_readonly_role.sql).
#
# Hai nhóm hàm:
#   - can_access / filter_tables / ensure_access: đồng bộ, CHỈ theo role (staff luôn bị chặn).
#   - can_access_user / filter_tables_user / ensure_access_user: async, có tính khối được cấp
#     riêng cho staff. Bắt buộc dùng await.

from typing import Iterable, Optional

from app.agent import scopes

STAFF_ROLE = "staff"

# Khoá: vai trò (chữ thường). Giá trị: tập bảng dạng "schema.table";
# "*" là mọi bảng, "schema.*" là mọi bảng trong schema đó.
# boss và manager được xem mọi bảng trong schema dwh, không xem schema khác.
ROLE_TABLES: dict[str, set[str]] = {
    "boss": {"dwh.*"},
    "manager": {"dwh.*"},
    "staff": set(),  # nhân viên: chỉ xem bảng của khối được cấp (config/scopes.yaml)
}


class PermissionDenied(Exception):
    """Vai trò hiện tại không được xem bảng được yêu cầu."""


def is_known_role(role: Optional[str]) -> bool:
    return (role or "").lower() in ROLE_TABLES


def _match(patterns: set[str], table: str) -> bool:
    """Bảng "schema.table" có khớp tập quyền không."""
    table = table.lower()
    schema = table.split(".", 1)[0]
    return "*" in patterns or table in patterns or f"{schema}.*" in patterns


# ---------------------------------------------------------------- chỉ theo role (đồng bộ)

def can_access(role: Optional[str], table: str) -> bool:
    """Kiểm tra vai trò có được xem bảng "schema.table" không (không tính quyền cấp riêng)."""
    return _match(ROLE_TABLES.get((role or "").lower(), set()), table)


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


# ---------------------------------------------------------------- có tính quyền cấp riêng (async)

async def _patterns(role: Optional[str], user_id) -> set[str]:
    """Tập bảng thực tế: bảng theo role, cộng bảng của các khối postgres được cấp nếu là staff.
    Khối api (house, misa) không thêm bảng nào."""
    role_l = (role or "").lower()
    patterns = set(ROLE_TABLES.get(role_l, set()))
    if role_l == STAFF_ROLE and user_id:
        granted = await scopes.user_scopes(role_l, user_id)
        patterns |= scopes.load_scopes_config().tables_for(granted)
    return patterns


async def can_access_user(role: Optional[str], user_id, table: str) -> bool:
    return _match(await _patterns(role, user_id), table)


async def filter_tables_user(role: Optional[str], user_id, tables: Iterable[str]) -> list[str]:
    p = await _patterns(role, user_id)  # chỉ truy vấn DB một lần
    return [t for t in tables if _match(p, t)]


async def ensure_access_user(role: Optional[str], user_id, tables: Iterable[str]) -> None:
    if not is_known_role(role):
        raise PermissionDenied("Vai trò không hợp lệ hoặc chưa được cấp quyền")
    p = await _patterns(role, user_id)
    denied = sorted(t for t in tables if not _match(p, t))
    if denied:
        raise PermissionDenied(
            "Không có quyền truy cập các bảng: " + ", ".join(denied)
        )