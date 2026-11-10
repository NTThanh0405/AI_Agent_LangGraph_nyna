-- app/agent/queries/revenue_by_channel.sql
-- Doanh thu và số đơn theo kênh bán, kênh lớn nhất đứng đầu.
-- Tham số: start, end_exclusive. Nguồn: dwh.fact_sales + dwh.dim_channel.
-- Tên kênh lấy từ source_name, thiếu thì dùng channel; bill chưa gán kênh (channel_key = -1)
-- gộp thành "Không xác định". Cùng định nghĩa doanh thu với revenue_by_day.sql.

SELECT
    CASE WHEN s.channel_key = -1 THEN 'Không xác định'
         ELSE COALESCE(NULLIF(c.source_name, ''), NULLIF(c.channel, ''), c.source_id)
    END                                            AS channel,
    SUM(s.final_amount)                            AS revenue,
    COUNT(*)                                       AS orders
FROM dwh.fact_sales AS s
JOIN dwh.dim_channel AS c ON c.channel_key = s.channel_key
WHERE s.tran_date >= %(start)s
  AND s.tran_date <  %(end_exclusive)s
  AND s.is_deleted = FALSE
GROUP BY 1
ORDER BY revenue DESC