-- app/agent/queries/customers_new_vs_returning.sql
-- Khách mới và khách quay lại trong kỳ báo cáo (một dòng).
-- Tham số: start, end_exclusive. Nguồn: dwh.fact_sales.
-- Khách mới = lần mua đầu tiên (toàn lịch sử) rơi trong kỳ; quay lại = đã mua trước ngày start.
-- Chỉ tính khách có mã hội viên (customer_key <> -1); khách vãng lai không nhận diện được.
-- Lưu ý: "lần mua đầu" tính theo dữ liệu đang có trong DWH, nếu DWH nạp từ giữa chừng
-- thì khách cũ có thể bị tính là mới.

SELECT
    COUNT(*) FILTER (WHERE h.first_date >= %(start)s)  AS new_customers,
    COUNT(*) FILTER (WHERE h.first_date <  %(start)s)  AS returning_customers
FROM (
    SELECT
        s.customer_key,
        MIN(s.tran_date) AS first_date,
        MAX(s.tran_date) AS last_date
    FROM dwh.fact_sales AS s
    WHERE s.customer_key <> -1
      AND s.is_deleted = FALSE
      AND s.tran_date < %(end_exclusive)s
    GROUP BY s.customer_key
) AS h
WHERE h.last_date >= %(start)s
