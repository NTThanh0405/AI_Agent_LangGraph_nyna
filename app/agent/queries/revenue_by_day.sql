-- app/agent/queries/revenue_by_day.sql
-- Doanh thu, số đơn, giá trị đơn trung bình theo từng ngày trong kỳ báo cáo.
-- Tham số: start (ngày đầu, gồm), end_exclusive (ngày sau ngày cuối).
-- Nguồn: dwh.fact_sales (một dòng = một bill, gồm cả đơn online và bill HUB).
--
-- GIẢ ĐỊNH CẦN XÁC NHẬN VỚI KẾ TOÁN:
--   1. Doanh thu = final_amount (số tiền khách thực trả sau giảm giá). Nếu công ty tính
--      doanh thu theo cột khác thì chỉ đổi đúng một chỗ SUM(...) bên dưới
--      (amount_after_vat, amount_org...). Giá trong fact_sales đã gồm VAT.
--   2. Bill bị xoá (is_deleted = TRUE) không tính. Chưa lọc theo status vì chưa biết
--      mã nào là bill huỷ/hợp lệ; nếu có, thêm một dòng AND status ... vào WHERE.
--   3. fact_sales đã gồm cả đơn online và bill HUB nên KHÔNG cộng thêm fact_online_orders hay fact_hub_bills
--      (quy tắc 8 trong knowledge/sql_examples.md).
--
-- Alias cột khớp COLUMN_LABELS của builder.py: day, revenue, orders, aov.

SELECT
    s.tran_date                                    AS day,
    SUM(s.final_amount)                            AS revenue,
    COUNT(*)                                       AS orders,
    SUM(s.final_amount) / NULLIF(COUNT(*), 0)      AS aov
FROM dwh.fact_sales AS s
WHERE s.tran_date >= %(start)s
  AND s.tran_date <  %(end_exclusive)s
  AND s.is_deleted = FALSE
GROUP BY s.tran_date
ORDER BY s.tran_date
