-- app/agent/queries/target_vs_actual.sql
-- Thực tế so với mục tiêu doanh thu trong kỳ báo cáo (một dòng).
-- Tham số: start, end_exclusive.
--
-- !!! CHƯA DÙNG ĐƯỢC: schema dwh hiện KHÔNG có bảng mục tiêu. File này giả định một bảng
-- dwh.sales_targets do bạn tạo, ví dụ:
--     CREATE TABLE dwh.sales_targets (
--         target_date   date NOT NULL,           -- một dòng mỗi ngày (chia đều mục tiêu tháng)
--         store_key     bigint NOT NULL DEFAULT -1,  -- -1 = toàn hệ thống
--         target_amount numeric(18,4) NOT NULL,
--         PRIMARY KEY (target_date, store_key)
--     );
-- Chưa có bảng này thì KHÔNG đưa target_vs_actual vào sections trong reports.yaml.
-- achievement_pct tính theo thang 0-100 (builder hiển thị thêm dấu %).

SELECT
    t.target,
    a.actual,
    a.actual / NULLIF(t.target, 0) * 100 AS achievement_pct
FROM (
    SELECT SUM(target_amount) AS target
    FROM dwh.sales_targets
    WHERE target_date >= %(start)s
      AND target_date <  %(end_exclusive)s
) AS t
CROSS JOIN (
    SELECT SUM(final_amount) AS actual
    FROM dwh.fact_sales
    WHERE tran_date >= %(start)s
      AND tran_date <  %(end_exclusive)s
      AND is_deleted = FALSE
) AS a
