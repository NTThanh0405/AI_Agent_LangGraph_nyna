-- app/agent/queries/top_products.sql
-- Sản phẩm bán chạy nhất kỳ báo cáo, xếp theo doanh thu (lấy 20 dòng, báo cáo hiện 5 dòng đầu).
-- Tham số: start, end_exclusive. Nguồn: dwh.fact_sale_items + dwh.fact_sales + dwh.dim_item.
-- Ngày lấy từ bill cha (fact_sales.tran_date), bỏ bill is_deleted và món tặng (is_gift).
-- GIẢ ĐỊNH: fact_sale_items.amount là thành tiền của dòng món. Nếu amount đã trừ hay chưa
-- trừ giảm giá khác với quy ước kế toán thì đổi SUM(i.amount) cho phù hợp.

SELECT
    COALESCE(NULLIF(d.item_name, ''), d.item_id)   AS product,
    SUM(i.quantity)                                AS quantity,
    SUM(i.amount)                                  AS revenue
FROM dwh.fact_sale_items AS i
JOIN dwh.fact_sales AS s ON s.sale_key = i.sale_key
JOIN dwh.dim_item   AS d ON d.item_key = i.item_key
WHERE s.tran_date >= %(start)s
  AND s.tran_date <  %(end_exclusive)s
  AND s.is_deleted = FALSE
  AND i.is_gift IS NOT TRUE
GROUP BY d.item_key, d.item_name, d.item_id
ORDER BY revenue DESC NULLS LAST
LIMIT 20