# tests/test_report_queries.py
# Kiểm tra CÁC FILE SQL THẬT trong app/agent/queries/ (không cần database): mỗi mục báo cáo có đủ file,
# qua được guard.check_template, chỉ đọc bảng trong dwh và manager được phép xem hết các bảng đó.
# Test này bắt lỗi trước khi service chạy: sửa file SQL sai (thiếu dấu ngoặc, tham số lạ, bảng ngoài dwh).

import pytest

from app.agent.permissions import can_access
from app.reports import builder


@pytest.fixture(autouse=True)
def fresh_cache():
    builder._prepare.cache_clear()
    yield
    builder._prepare.cache_clear()


@pytest.mark.parametrize("key", sorted(builder.SECTIONS))
def test_section_sql_file_exists_and_passes_guard(key):
    section = builder.SECTIONS[key]
    assert (builder.QUERY_DIR / section.sql_file).is_file(), f"thiếu {section.sql_file}"
    checked = builder._prepare(section.sql_file)
    assert checked.tables, "câu SQL không đọc bảng nào"


@pytest.mark.parametrize("role", ["manager", "boss"])
@pytest.mark.parametrize("key", sorted(builder.SECTIONS))
def test_report_tables_are_allowed_for_manager_and_boss(key, role):
    checked = builder._prepare(builder.SECTIONS[key].sql_file)
    denied = [t for t in checked.tables if not can_access(role, t)]
    assert not denied, f"role {role} không được xem {denied}"


@pytest.mark.parametrize("key", sorted(builder.SECTIONS))
def test_report_sql_only_reads_dwh(key):
    checked = builder._prepare(builder.SECTIONS[key].sql_file)
    assert all(t.startswith("dwh.") for t in checked.tables)
