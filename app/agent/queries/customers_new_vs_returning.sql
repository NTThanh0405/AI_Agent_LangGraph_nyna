-- app/agent/queries/bottom_products.sql
-- Món bán chậm nhất kỳ báo cáo, xếp theo doanh thu tăng dần (lấy 20 dòng, báo cáo hiện 5 dòng đầu).
-- Tham số: start, end_exclusive. Nguồn: dwh.fact_sale_items + dwh.fact_sales + dwh.dim_item.
-- Cùng định nghĩa với top_products.sql: ngày lấy từ bill cha, bỏ bill is_deleted và món tặng.
--
-- Khác top_products.sql ở hai điểm:
--   1. Món KHÔNG bán được cái nào trong kỳ vẫn hiện (doanh thu 0) — đây thường là món cần chú ý
--      nhất. "Món còn bán" = có bán trong kỳ hoặc trong 28 ngày trước kỳ, để không kéo vào các món
--      đã ngừng kinh doanh từ lâu (dim_item không có cờ đang bán/ngừng bán).
--   2. Bỏ dòng món con / topping (parent_item_id có giá trị), vì giá rẻ nên sẽ chiếm hết danh sách.
--      GIẢ ĐỊNH: parent_item_id có giá trị nghĩa là topping hoặc món trong combo — kiểm lại với dữ liệu thật.

WITH mon AS (
    SELECT
        i.item_key,
        SUM(i.quantity) FILTER (WHERE s.tran_date >= %(start)s) AS quantity,
        SUM(i.amount)   FILTER (WHERE s.tran_date >= %(start)s) AS revenue
    FROM dwh.fact_sale_items AS i
    JOIN dwh.fact_sales      AS s ON s.sale_key = i.sale_key
    WHERE s.tran_date >= CAST(%(start)s AS date) - 28
      AND s.tran_date <  %(end_exclusive)s
      AND s.is_deleted = FALSE
      AND i.is_gift IS NOT TRUE
      AND COALESCE(i.parent_item_id, '') = ''
      AND i.item_key <> -1
    GROUP BY i.item_key
)
SELECT
    COALESCE(NULLIF(d.item_name, ''), d.item_id)   AS product,
    COALESCE(m.quantity, 0)                        AS quantity,
    COALESCE(m.revenue, 0)                         AS revenue
FROM mon        AS m
JOIN dwh.dim_item AS d ON d.item_key = m.item_key
ORDER BY revenue ASC, quantity ASC, product
LIMIT 20