# tests/check_report_permissions.py
# Kiểm tra mỗi mục báo cáo (file SQL trong app/agent/queries/) dùng những bảng nào và role manager
# có được xem các bảng đó theo app/agent/permissions.py không. Chạy từ thư mục gốc dự án:
#   python tests/check_report_permissions.py
#   python tests/check_report_permissions.py boss        # kiểm tra role khác
# Mục nào báo "BỊ CHẶN" sẽ bị builder bỏ qua khi gửi báo cáo; sửa ROLE_TABLES trong permissions.py
# (và GRANT SELECT trong sql/create_readonly_role.sql cho khớp) nếu manager cần xem bảng đó.

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from app.agent.permissions import can_access  # noqa: E402
from app.reports import builder  # noqa: E402


def main() -> int:
    role = sys.argv[1] if len(sys.argv) > 1 else "manager"
    print(f"Role: {role} | Thư mục SQL: {builder.QUERY_DIR}\n")
    problems = 0
    for key, section in builder.SECTIONS.items():
        try:
            tables = sorted(builder._prepare(section.sql_file).tables)  # v2: qua guard.check_template
        except FileNotFoundError:
            print(f"[THIẾU FILE] {key}: không có {section.sql_file}")
            problems += 1
            continue
        except Exception as e:  # SQL không đọc được hoặc không phải SELECT
            print(f"[LỖI SQL]    {key}: {e}")
            problems += 1
            continue
        denied = [t for t in tables if not can_access(role, t)]
        status = "BỊ CHẶN  " if denied else "OK       "
        problems += bool(denied)
        print(f"[{status}] {key}: bảng dùng = {', '.join(tables) or '(không có)'}"
              + (f"  -> role '{role}' không được xem: {', '.join(denied)}" if denied else ""))
    print(f"\n{'Tất cả đều ổn.' if not problems else f'Có {problems} mục cần xử lý.'}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())