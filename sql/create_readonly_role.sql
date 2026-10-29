-- sql/create_readonly_role.sql
-- Chạy bằng tài khoản postgres, ĐÃ KẾT NỐI VÀO database NynaGroup:
--   psql -U postgres -d "NynaGroup" -f sql/create_readonly_role.sql
-- Chỉ chạy MỘT lần (chạy lại sẽ lỗi vì role đã tồn tại).
-- Agent lưu hội thoại trong RAM (MemorySaver) nên không cần role/schema checkpoint.

-- 1) Role chỉ-đọc cho agent: chỉ thấy schema dwh
CREATE ROLE agent_readonly LOGIN PASSWORD 'nyna_ai_agent1'
    NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION CONNECTION LIMIT 10;
ALTER ROLE agent_readonly SET default_transaction_read_only = on;
ALTER ROLE agent_readonly SET statement_timeout = '15s';
ALTER ROLE agent_readonly SET timezone = 'Asia/Ho_Chi_Minh';  -- date_trunc/::date trên timestamptz ra giờ VN
ALTER ROLE agent_readonly SET search_path = dwh;

GRANT CONNECT ON DATABASE "NynaGroup" TO agent_readonly;
GRANT USAGE ON SCHEMA dwh TO agent_readonly;
GRANT SELECT ON ALL TABLES IN SCHEMA dwh TO agent_readonly;

-- Bảng dwh tạo sau này tự được đọc.
-- Đổi "postgres" thành role ĐANG TẠO bảng dwh (role chạy ETL), xem owner bằng:
--   SELECT DISTINCT tableowner FROM pg_tables WHERE schemaname = 'dwh';
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA dwh
    GRANT SELECT ON TABLES TO agent_readonly;

-- Che thông tin cá nhân của khách: bỏ quyền cả bảng, chỉ cấp các cột không nhạy cảm
REVOKE SELECT ON dwh.dim_customer FROM agent_readonly;
GRANT SELECT (customer_key, membership_id, customer_name, gender,
              membership_type_key, created_at)
    ON dwh.dim_customer TO agent_readonly;
-- Đã ẩn: phone_number, email, address, birthday.

-- Không cấp gì trên public và raw_json_hub, raw_json_dw, raw_ipos, raw_ipos_hub.
-- 4 schema raw_* = false.
SELECT s AS schema, has_schema_privilege('agent_readonly', s, 'USAGE') AS usage
FROM unnest(ARRAY['dwh','public','raw_ipos','raw_ipos_hub','raw_json_hub','raw_json_dw']) s;

-- 3) Kiểm tra quyền bảng (phải = true / false / false)
SELECT has_table_privilege('agent_readonly', 'dwh.fact_sales', 'SELECT')      AS doc_fact_sales,
       has_column_privilege('agent_readonly', 'dwh.dim_customer', 'phone_number', 'SELECT') AS doc_sdt_khach,
       has_table_privilege('agent_readonly', 'dwh.fact_sales', 'INSERT')      AS ghi_fact_sales;