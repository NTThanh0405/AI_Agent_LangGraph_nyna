-- sql/create_readonly_role.sql
-- Tạo 2 tài khoản PostgreSQL cho agent. Chạy bằng tài khoản admin (superuser) của server.
--   agent_readonly   : CHỈ ĐỌC, dùng để truy vấn dữ liệu phân tích (lớp bảo vệ chính).
--   agent_checkpoint : chỉ ghi được trong schema agent_state, dùng để lưu hội thoại của agent.
-- Đổi tên database, mật khẩu và danh sách bảng cho đúng với hệ thống của bạn trước khi chạy.

-- 1) Tài khoản chỉ-đọc cho agent
CREATE ROLE agent_readonly LOGIN PASSWORD 'DOI_MAT_KHAU_1' CONNECTION LIMIT 10;
ALTER ROLE agent_readonly SET default_transaction_read_only = on;
ALTER ROLE agent_readonly SET statement_timeout = '15s';

GRANT CONNECT ON DATABASE your_db TO agent_readonly;
GRANT USAGE ON SCHEMA public TO agent_readonly;

-- Chỉ cấp SELECT cho đúng những bảng agent được phép đọc (khớp app/agent/permissions.py).
-- Muốn che cột nhạy cảm thì cấp theo cột, ví dụ: GRANT SELECT (id, name) ON public.customers ...
GRANT SELECT ON public.orders, public.customers, public.products TO agent_readonly;

-- 2) Tài khoản lưu hội thoại (checkpoint) - tách riêng, không đụng tới dữ liệu phân tích
CREATE ROLE agent_checkpoint LOGIN PASSWORD 'DOI_MAT_KHAU_2' CONNECTION LIMIT 5;
GRANT CONNECT ON DATABASE your_db TO agent_checkpoint;
CREATE SCHEMA agent_state AUTHORIZATION agent_checkpoint;
ALTER ROLE agent_checkpoint SET search_path = agent_state;