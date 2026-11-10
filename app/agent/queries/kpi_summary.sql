-- app/agent/queries/kpi_summary.sql
-- Chỉ số chính của kỳ báo cáo (một dòng): doanh thu, số đơn, giá trị đơn trung bình, số khách.
-- Tham số: start, end_exclusive. Nguồn: dwh.fact_sales (bill POS).
-- Cùng định nghĩa với revenue_by_day.sql: doanh thu = final_amount, bỏ bill is_deleted.
-- customers chỉ đếm khách có mã hội viên (customer_key = -1 là khách vãng lai/đối tác, không đếm).

SELECT
    SUM(s.final_amount)                                            AS revenue,
    COUNT(*)                                                       AS orders,
    SUM(s.final_amount) / NULLIF(COUNT(*), 0)                      AS aov,
    COUNT(DISTINCT s.customer_key) FILTER (WHERE s.customer_key <> -1) AS customers
FROM dwh.fact_sales AS s
WHERE s.tran_date >= %(start)s
  AND s.tran_date <  %(end_exclusive)s
  AND s.is_deleted = FALSE