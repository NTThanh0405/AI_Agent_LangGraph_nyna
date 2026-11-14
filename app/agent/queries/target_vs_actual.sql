-- app/agent/queries/target_vs_actual.sql
-- Số THỰC TẾ để so với mục tiêu (một dòng). Mục tiêu KHÔNG nằm trong database: lấy từ
-- config/targets.yaml (app/agent/business_config.py), builder.py ghép hai phần lại.
-- Tham số: start, end_exclusive. builder.py chạy file này 2 lần: cho ngày hôm qua và cho
-- luỹ kế từ đầu tháng đến hôm qua.
-- Tên cột phải trùng tên chỉ số trong targets.yaml (revenue, orders, aov).
-- Cùng định nghĩa với kpi_summary.sql: doanh thu = final_amount, bỏ bill is_deleted.

SELECT
    SUM(s.final_amount)                       AS revenue,
    COUNT(*)                                  AS orders,
    SUM(s.final_amount) / NULLIF(COUNT(*), 0) AS aov
FROM dwh.fact_sales AS s
WHERE s.tran_date >= %(start)s
  AND s.tran_date <  %(end_exclusive)s
  AND s.is_deleted = FALSE