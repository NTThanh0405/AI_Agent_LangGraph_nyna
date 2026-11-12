# Ví dụ SQL (PostgreSQL, schema dwh)

> Công thức chỉ số theo `glossary.md`. File này chỉ dạy CÁCH VIẾT SQL cho đúng các công thức đó.

## Quy tắc bắt buộc khi viết SQL

1. Luôn ghi rõ schema: `dwh.fact_sales`, không viết `fact_sales`. Chỉ một câu SELECT. Không dùng `SELECT *`.
2. Doanh thu = SUM(`s.final_amount`) trên `dwh.fact_sales` (alias `s`). Không cộng doanh thu từ
   `fact_online_orders` hay `fact_hub_bills` (đơn online và bill HUB đã nằm trong `fact_sales`).
3. Bill hợp lệ: `s.is_deleted = FALSE`. Luôn có điều kiện này.
4. Ngày bán: lọc và nhóm theo `s.tran_date` (DATE, đã là ngày Việt Nam).
   Giờ trong ngày: `EXTRACT(HOUR FROM s.tran_ts AT TIME ZONE 'Asia/Ho_Chi_Minh')`.
   Không dùng `tran_ts::date` để nhóm ngày (lệch múi giờ UTC).
5. KHOẢNG NGÀY: dùng đúng ngày hệ thống đã tính sẵn trong bản tóm tắt phân tích, viết thành
   literal `DATE 'YYYY-MM-DD'`. Các ví dụ dưới dùng tuần 02/11–08/11/2026 làm mẫu; thay bằng khoảng
   ngày thật. Chỉ khi hệ thống KHÔNG đưa khoảng ngày mới tự tính từ giờ Việt Nam:
   hôm nay `(now() AT TIME ZONE 'Asia/Ho_Chi_Minh')::date`, đầu tuần (thứ Hai)
   `date_trunc('week', now() AT TIME ZONE 'Asia/Ho_Chi_Minh')::date`, đầu tháng tương tự với `'month'`.
6. Lọc khoảng ngày: `tran_date BETWEEN DATE 'đầu_kỳ' AND DATE 'cuối_kỳ'` (gồm cả hai đầu).
   Cột kiểu timestamptz (`created_ts`, `updated_ts`, `change_ts`): lọc theo khoảng
   `>= (DATE 'đầu_kỳ')::timestamp AT TIME ZONE 'Asia/Ho_Chi_Minh'` và
   `< (DATE 'cuối_kỳ' + 1)::timestamp AT TIME ZONE 'Asia/Ho_Chi_Minh'` để dùng được index.
7. Khoá `-1` (không xác định): khi XẾP HẠNG thì loại `-1` (`store_key <> -1`, `item_key <> -1`...);
   khi tính TỔNG hoặc tỉ trọng thì GIỮ `-1` và hiện là "Không xác định". Đếm khách luôn có `customer_key <> -1`.
8. Tên kênh hiển thị (dùng y nguyên biểu thức này, giống dashboard và `queries/`):
   `CASE WHEN c.channel_key = -1 THEN 'Không xác định' ELSE COALESCE(NULLIF(c.source_name, ''), NULLIF(c.channel, ''), c.source_id) END`.
   Lọc theo một kênh cụ thể thì chạy `SELECT DISTINCT channel, source_name FROM dwh.dim_channel` trước, không đoán tên.
9. JOIN sang bảng chi tiết (`fact_sale_items`, `fact_sale_payments`) thì KHÔNG SUM cột của
   `fact_sales` (bị nhân dòng). Cần cả số của bill lẫn số của món thì gộp bảng chi tiết theo
   `sale_key` trong một CTE trước rồi mới JOIN (ví dụ 4).
10. Doanh thu theo món: SUM(`i.amount`), bỏ món tặng (`i.is_gift IS NOT TRUE`). Không so tổng tiền
    theo món với doanh thu bill. Topping/món con: dòng có `COALESCE(i.parent_item_id, '') <> ''`.
11. Chia thì dùng `NULLIF(mẫu_số, 0)`. Số tiền làm tròn `ROUND(x, 0)`, phần trăm `ROUND(x, 1)`.
    Đặt alias cột không trùng nhau (ví dụ `doanh_thu`, `so_bill`).
12. Không truy vấn số điện thoại, email, địa chỉ, ngày sinh của khách: các cột này bị che ở
    tài khoản chỉ-đọc (truy vấn sẽ lỗi quyền). Nhận diện khách bằng `customer_name` và `membership_id`.

---

## 1. Tổng quan KPI một khoảng ngày

Hỏi: "Tuần này bán được bao nhiêu?", "KPI tháng này?"

```sql
SELECT COUNT(*)                                             AS so_bill,
       ROUND(SUM(s.final_amount), 0)                        AS doanh_thu,
       ROUND(SUM(s.final_amount) / NULLIF(COUNT(*), 0), 0)  AS aov,
       COUNT(DISTINCT s.customer_key) FILTER (WHERE s.customer_key <> -1) AS so_khach_thanh_vien
FROM dwh.fact_sales s
WHERE s.is_deleted = FALSE
  AND s.tran_date BETWEEN DATE '2026-11-02' AND DATE '2026-11-08';
```

## 2. Doanh thu theo ngày (kể cả ngày không bán được)

Hỏi: "Doanh thu từng ngày tuần này?", "7 ngày qua bán thế nào?"
Lưu ý: tạo đủ các ngày bằng `generate_series` để ngày không có bill vẫn hiện số 0.

```sql
SELECT d::date                                       AS ngay,
       COUNT(s.sale_key)                             AS so_bill,
       ROUND(COALESCE(SUM(s.final_amount), 0), 0)    AS doanh_thu
FROM generate_series(DATE '2026-11-02', DATE '2026-11-08', INTERVAL '1 day') AS d
LEFT JOIN dwh.fact_sales s
       ON s.tran_date = d::date AND s.is_deleted = FALSE
GROUP BY d
ORDER BY d;
```

## 3. Doanh thu theo cửa hàng

Hỏi: "Cửa hàng nào bán tốt nhất tuần này?", "So sánh các chi nhánh."

```sql
SELECT st.store_name                                        AS cua_hang,
       COUNT(*)                                             AS so_bill,
       ROUND(SUM(s.final_amount), 0)                        AS doanh_thu,
       ROUND(SUM(s.final_amount) / NULLIF(COUNT(*), 0), 0)  AS aov
FROM dwh.fact_sales s
JOIN dwh.dim_store st ON st.store_key = s.store_key
WHERE s.is_deleted = FALSE
  AND s.store_key <> -1
  AND s.tran_date BETWEEN DATE '2026-11-02' AND DATE '2026-11-08'
GROUP BY st.store_key, st.store_name
ORDER BY doanh_thu DESC;
```

## 4. Số món/đơn theo cửa hàng (vừa số của bill vừa số của món)

Hỏi: "Trung bình mỗi đơn bao nhiêu món?", "Cửa hàng nào khách gọi nhiều món nhất?"
Lưu ý: gộp `fact_sale_items` theo `sale_key` TRƯỚC (CTE `mon`), rồi mới JOIN với bill;
JOIN thẳng rồi SUM `final_amount` sẽ bị nhân dòng (xem Sai 1).

```sql
WITH bill AS (
  SELECT s.sale_key, s.store_key, s.final_amount
  FROM dwh.fact_sales s
  WHERE s.is_deleted = FALSE
    AND s.tran_date BETWEEN DATE '2026-11-02' AND DATE '2026-11-08'
),
mon AS (
  SELECT i.sale_key, SUM(i.quantity) AS so_mon
  FROM dwh.fact_sale_items i
  JOIN bill b ON b.sale_key = i.sale_key
  GROUP BY i.sale_key
)
SELECT st.store_name                                              AS cua_hang,
       COUNT(*)                                                   AS so_bill,
       ROUND(SUM(b.final_amount), 0)                              AS doanh_thu,
       ROUND(SUM(COALESCE(m.so_mon, 0)) / NULLIF(COUNT(*), 0), 2) AS so_mon_moi_don
FROM bill b
JOIN dwh.dim_store st ON st.store_key = b.store_key
LEFT JOIN mon m ON m.sale_key = b.sale_key
WHERE b.store_key <> -1
GROUP BY st.store_key, st.store_name
ORDER BY doanh_thu DESC;
```

## 5. Doanh thu theo kênh bán và tỉ trọng

Hỏi: "Doanh thu theo kênh?", "Grab chiếm bao nhiêu %?"
Lưu ý: GIỮ kênh `-1` (hiện "Không xác định") để tỉ trọng cộng đủ 100%.

```sql
SELECT CASE WHEN c.channel_key = -1 THEN 'Không xác định'
            ELSE COALESCE(NULLIF(c.source_name, ''), NULLIF(c.channel, ''), c.source_id) END AS kenh,
       COUNT(*)                                        AS so_bill,
       ROUND(SUM(s.final_amount), 0)                   AS doanh_thu,
       ROUND(100.0 * SUM(s.final_amount)
             / NULLIF(SUM(SUM(s.final_amount)) OVER (), 0), 1) AS ty_trong_pct
FROM dwh.fact_sales s
JOIN dwh.dim_channel c ON c.channel_key = s.channel_key
WHERE s.is_deleted = FALSE
  AND s.tran_date BETWEEN DATE '2026-11-02' AND DATE '2026-11-08'
GROUP BY 1
ORDER BY doanh_thu DESC;
```

## 6. Tỉ trọng kênh trong từng cửa hàng

Hỏi: "Cửa hàng nào phụ thuộc Grab nhiều nhất?", "Mỗi chi nhánh bán qua app bao nhiêu %?"
Lưu ý: tỉ trọng ở đây là trên doanh thu CỦA CỬA HÀNG đó (`PARTITION BY` cửa hàng), không phải toàn chuỗi.

```sql
SELECT st.store_name                                   AS cua_hang,
       CASE WHEN c.channel_key = -1 THEN 'Không xác định'
            ELSE COALESCE(NULLIF(c.source_name, ''), NULLIF(c.channel, ''), c.source_id) END AS kenh,
       ROUND(SUM(s.final_amount), 0)                   AS doanh_thu,
       ROUND(100.0 * SUM(s.final_amount)
             / NULLIF(SUM(SUM(s.final_amount)) OVER (PARTITION BY st.store_key), 0), 1) AS ty_trong_trong_cua_hang_pct
FROM dwh.fact_sales s
JOIN dwh.dim_store   st ON st.store_key = s.store_key
JOIN dwh.dim_channel c  ON c.channel_key = s.channel_key
WHERE s.is_deleted = FALSE
  AND s.store_key <> -1
  AND s.tran_date BETWEEN DATE '2026-11-02' AND DATE '2026-11-08'
GROUP BY st.store_key, st.store_name, 2
ORDER BY cua_hang, doanh_thu DESC;
```

## 7. Theo giờ và khung giờ

Hỏi: "Giờ nào đông khách nhất?", "Ca sáng bán bao nhiêu?"
Lưu ý: khung giờ theo glossary: Sáng 5–9h (5 giờ), Ngày 10–21h (12 giờ), Đêm 22–4h (7 giờ).
Bill không có `tran_ts` thì giờ = NULL, xếp vào "Không rõ giờ".

```sql
WITH g AS (
  SELECT EXTRACT(HOUR FROM s.tran_ts AT TIME ZONE 'Asia/Ho_Chi_Minh')::int AS gio,
         s.final_amount
  FROM dwh.fact_sales s
  WHERE s.is_deleted = FALSE
    AND s.tran_date BETWEEN DATE '2026-11-02' AND DATE '2026-11-08'
)
SELECT CASE WHEN gio IS NULL THEN 'Không rõ giờ'
            WHEN gio BETWEEN 5 AND 9 THEN 'Sáng (5–10h)'
            WHEN gio BETWEEN 10 AND 21 THEN 'Ngày (10–22h)'
            ELSE 'Đêm (22–5h)' END              AS khung_gio,
       gio,
       COUNT(*)                                 AS so_bill,
       ROUND(SUM(final_amount), 0)              AS doanh_thu
FROM g
GROUP BY 1, 2
ORDER BY gio NULLS LAST;
```

## 8. Top món bán chạy

Hỏi: "Top 10 món bán chạy tuần này?"
Lưu ý: "bán chạy" mặc định xếp theo doanh thu (glossary); người dùng hỏi "bán được nhiều ly/phần nhất"
thì đổi `ORDER BY so_luong DESC` và nói rõ đang xếp theo số lượng.

```sql
SELECT COALESCE(NULLIF(d.item_name, ''), d.item_id)   AS mon,
       SUM(i.quantity)                                AS so_luong,
       ROUND(SUM(i.amount), 0)                        AS doanh_thu
FROM dwh.fact_sale_items i
JOIN dwh.fact_sales s ON s.sale_key = i.sale_key
JOIN dwh.dim_item   d ON d.item_key = i.item_key
WHERE s.is_deleted = FALSE
  AND i.item_key <> -1
  AND i.is_gift IS NOT TRUE
  AND s.tran_date BETWEEN DATE '2026-11-02' AND DATE '2026-11-08'
GROUP BY d.item_key, d.item_name, d.item_id
ORDER BY doanh_thu DESC
LIMIT 10;
```

## 9. Món bán chậm (gồm cả món không bán được)

Hỏi: "Món nào bán chậm nhất?", "Món nào tuần này không bán được?"
Lưu ý: món không bán được không có dòng nào trong kỳ, nên lấy danh sách món từ kỳ này + 28 ngày
trước rồi đếm phần trong kỳ; bỏ topping/món con (giá rẻ sẽ chiếm hết danh sách).

```sql
WITH mon AS (
  SELECT i.item_key,
         COALESCE(SUM(i.quantity) FILTER (WHERE s.tran_date >= DATE '2026-11-02'), 0) AS so_luong,
         COALESCE(SUM(i.amount)   FILTER (WHERE s.tran_date >= DATE '2026-11-02'), 0) AS doanh_thu
  FROM dwh.fact_sale_items i
  JOIN dwh.fact_sales s ON s.sale_key = i.sale_key
  WHERE s.is_deleted = FALSE
    AND i.item_key <> -1
    AND i.is_gift IS NOT TRUE
    AND COALESCE(i.parent_item_id, '') = ''
    AND s.tran_date BETWEEN DATE '2026-11-02' - 28 AND DATE '2026-11-08'
  GROUP BY i.item_key
)
SELECT COALESCE(NULLIF(d.item_name, ''), d.item_id) AS mon,
       m.so_luong,
       ROUND(m.doanh_thu, 0)                        AS doanh_thu
FROM mon m
JOIN dwh.dim_item d ON d.item_key = m.item_key
ORDER BY m.doanh_thu ASC, m.so_luong ASC
LIMIT 10;
```

## 10. So sánh với kỳ trước (cùng số ngày)

Hỏi: "Doanh thu tuần này so với tuần trước?"
Lưu ý: kỳ trước = cùng số ngày, liền ngay trước. Kỳ chưa kết thúc (tuần này mới qua 3 ngày) thì
cuối kỳ là hôm nay và kỳ trước cũng chỉ 3 ngày, không so với cả tuần trước.
Ở đây kỳ này 02–08/11 (7 ngày) → kỳ trước 26/10–01/11.

```sql
WITH k AS (
  SELECT DATE '2026-11-02' AS tu, DATE '2026-11-08' AS den,
         DATE '2026-11-08' - DATE '2026-11-02' + 1 AS n
),
r AS (
  SELECT SUM(s.final_amount) FILTER (WHERE s.tran_date >= k.tu)  AS dt_nay,
         COUNT(*)            FILTER (WHERE s.tran_date >= k.tu)  AS bill_nay,
         SUM(s.final_amount) FILTER (WHERE s.tran_date <  k.tu)  AS dt_truoc,
         COUNT(*)            FILTER (WHERE s.tran_date <  k.tu)  AS bill_truoc
  FROM dwh.fact_sales s
  CROSS JOIN k
  WHERE s.is_deleted = FALSE
    AND s.tran_date BETWEEN k.tu - k.n AND k.den
)
SELECT ROUND(dt_nay, 0)                                                AS doanh_thu_ky_nay,
       ROUND(dt_truoc, 0)                                              AS doanh_thu_ky_truoc,
       ROUND(100.0 * (dt_nay - dt_truoc) / NULLIF(dt_truoc, 0), 1)     AS doanh_thu_thay_doi_pct,
       bill_nay                                                        AS so_bill_ky_nay,
       bill_truoc                                                      AS so_bill_ky_truoc,
       ROUND(100.0 * (bill_nay - bill_truoc) / NULLIF(bill_truoc, 0), 1) AS so_bill_thay_doi_pct
FROM r;
```

## 11. Khách mới và khách quay lại

Hỏi: "Tháng này có bao nhiêu khách mới / khách quay lại?"
Lưu ý: chỉ tìm ngày mua đầu cho những khách CÓ MUA trong kỳ (không quét toàn bộ khách).
DWH chỉ có dữ liệu từ một mốc nên "khách mới" có thể bị tính dư cho giai đoạn đầu; nói rõ khi trả lời.

```sql
WITH khach_trong_ky AS (
  SELECT DISTINCT s.customer_key
  FROM dwh.fact_sales s
  WHERE s.is_deleted = FALSE
    AND s.customer_key <> -1
    AND s.tran_date BETWEEN DATE '2026-11-02' AND DATE '2026-11-08'
),
lan_dau AS (
  SELECT s.customer_key, MIN(s.tran_date) AS ngay_dau
  FROM dwh.fact_sales s
  JOIN khach_trong_ky k ON k.customer_key = s.customer_key
  WHERE s.is_deleted = FALSE
  GROUP BY s.customer_key
)
SELECT COUNT(*) FILTER (WHERE ngay_dau >= DATE '2026-11-02') AS khach_moi,
       COUNT(*) FILTER (WHERE ngay_dau <  DATE '2026-11-02') AS khach_quay_lai
FROM lan_dau;
```

## 12. Khách chi nhiều nhất

Hỏi: "Top 10 khách hàng chi tiêu nhiều nhất tháng này?"
Lưu ý: không lấy số điện thoại (bị che); nhận diện khách bằng tên và mã hội viên.

```sql
SELECT c.customer_name,
       c.membership_id,
       COUNT(*)                           AS so_bill,
       ROUND(SUM(s.final_amount), 0)      AS tong_chi
FROM dwh.fact_sales s
JOIN dwh.dim_customer c ON c.customer_key = s.customer_key
WHERE s.is_deleted = FALSE
  AND s.customer_key <> -1
  AND s.tran_date BETWEEN DATE '2026-11-01' AND DATE '2026-11-30'
GROUP BY c.customer_key, c.customer_name, c.membership_id
ORDER BY tong_chi DESC
LIMIT 10;
```

## 13. Theo phương thức thanh toán

Hỏi: "Khách trả bằng tiền mặt hay chuyển khoản nhiều hơn?"
Lưu ý: SUM `p.amount` của `fact_sale_payments`, không SUM cột của `fact_sales`. Một bill có thể
trả bằng nhiều phương thức nên tổng `so_bill` các dòng có thể lớn hơn số bill thật.
Xem giá trị bằng `SELECT DISTINCT payment_method FROM dwh.fact_sale_payments` trước khi lọc theo tên.

```sql
SELECT p.payment_method                AS phuong_thuc,
       COUNT(DISTINCT p.sale_key)      AS so_bill,
       ROUND(SUM(p.amount), 0)         AS so_tien
FROM dwh.fact_sale_payments p
JOIN dwh.fact_sales s ON s.sale_key = p.sale_key
WHERE s.is_deleted = FALSE
  AND s.tran_date BETWEEN DATE '2026-11-02' AND DATE '2026-11-08'
GROUP BY p.payment_method
ORDER BY so_tien DESC;
```

## 14. Khuyến mãi được dùng nhiều nhất

Hỏi: "Chương trình khuyến mãi nào được dùng nhiều nhất?"
Lưu ý: `promotion_key = -1` là bill không có khuyến mãi, loại ra. `tien_giam` ở đây chỉ là
`amount_discount_price`; bill còn có `amount_discount_detail`, `amount_discount_extra` — chưa xác
nhận cột nào là tổng tiền giảm, nên nói rõ đang dùng cột nào khi trả lời.

```sql
SELECT pr.promotion_name                      AS khuyen_mai,
       COUNT(*)                               AS so_bill,
       ROUND(SUM(s.final_amount), 0)          AS doanh_thu,
       ROUND(SUM(s.amount_discount_price), 0) AS tien_giam
FROM dwh.fact_sales s
JOIN dwh.dim_promotion pr ON pr.promotion_key = s.promotion_key
WHERE s.is_deleted = FALSE
  AND s.promotion_key <> -1
  AND s.tran_date BETWEEN DATE '2026-11-02' AND DATE '2026-11-08'
GROUP BY pr.promotion_key, pr.promotion_name
ORDER BY so_bill DESC;
```

## 15. Theo nhân viên

Hỏi: "Nhân viên nào thu ngân nhiều bill nhất?"

```sql
SELECT e.user_name                      AS nhan_vien,
       COUNT(*)                         AS so_bill,
       ROUND(SUM(s.final_amount), 0)    AS doanh_thu
FROM dwh.fact_sales s
JOIN dwh.dim_employee e ON e.employee_key = s.employee_key
WHERE s.is_deleted = FALSE
  AND s.employee_key <> -1
  AND s.tran_date BETWEEN DATE '2026-11-02' AND DATE '2026-11-08'
GROUP BY e.employee_key, e.user_name
ORDER BY so_bill DESC
LIMIT 10;
```

## 16. Đơn online theo kênh và trạng thái

Hỏi: "Tuần này bao nhiêu đơn Grab bị huỷ?"
Lưu ý: `fact_online_orders` chỉ để xem trạng thái, KHÔNG để tính doanh thu. Chạy
`SELECT DISTINCT order_status FROM dwh.fact_online_orders` để biết tên trạng thái trước khi nói đơn nào
là "huỷ"; chưa có danh sách xác nhận thì nêu rõ là đang hiểu theo tên (CANCEL/REJECT/FAIL).

```sql
SELECT CASE WHEN c.channel_key = -1 THEN 'Không xác định'
            ELSE COALESCE(NULLIF(c.source_name, ''), NULLIF(c.channel, ''), c.source_id) END AS kenh,
       o.order_status                 AS trang_thai,
       COUNT(*)                       AS so_don
FROM dwh.fact_online_orders o
JOIN dwh.dim_channel c ON c.channel_key = o.channel_key
WHERE o.created_ts >= (DATE '2026-11-02')::timestamp AT TIME ZONE 'Asia/Ho_Chi_Minh'
  AND o.created_ts <  (DATE '2026-11-08' + 1)::timestamp AT TIME ZONE 'Asia/Ho_Chi_Minh'
GROUP BY 1, 2
ORDER BY kenh, so_don DESC;
```

## 17. Voucher theo chiến dịch

Hỏi: "Chiến dịch voucher nào được dùng nhiều nhất?"
Lưu ý: `status_name` có sẵn: 'Đã dùng', 'Hết hạn', 'Khả dụng'. Bảng nguồn event 7 chưa có dữ liệu
mẫu: nếu kết quả rỗng thì nói "chưa có dữ liệu voucher", không kết luận là không ai dùng.

```sql
SELECT vc.campaign_name                AS chien_dich,
       v.status_name                   AS trang_thai,
       COUNT(*)                        AS so_voucher
FROM dwh.fact_member_vouchers v
JOIN dwh.dim_voucher_campaign vc ON vc.campaign_key = v.campaign_key
GROUP BY vc.campaign_key, vc.campaign_name, v.status_name
ORDER BY vc.campaign_name, so_voucher DESC;
```

## 18. Món hết hàng gần đây

Hỏi: "Tuần qua món nào hay hết hàng?"

```sql
SELECT COALESCE(NULLIF(d.item_name, ''), d.item_id) AS mon,
       st.store_name                                AS cua_hang,
       COUNT(*)                                     AS so_lan_het_hang
FROM dwh.fact_item_stock_events e
JOIN dwh.dim_item  d  ON d.item_key  = e.item_key
JOIN dwh.dim_store st ON st.store_key = e.store_key
WHERE e.status = 'OUT_OF_STOCK'
  AND e.updated_ts >= (DATE '2026-11-02')::timestamp AT TIME ZONE 'Asia/Ho_Chi_Minh'
  AND e.updated_ts <  (DATE '2026-11-08' + 1)::timestamp AT TIME ZONE 'Asia/Ho_Chi_Minh'
GROUP BY d.item_key, d.item_name, d.item_id, st.store_key, st.store_name
ORDER BY so_lan_het_hang DESC
LIMIT 10;
```

## 19. Thay đổi hạng thành viên

Hỏi: "Tháng này có bao nhiêu khách lên hạng?"
Lưu ý: bảng ghi MỌI lần đổi hạng, cả lên lẫn xuống. Chưa có thứ tự hạng trong DWH nên không tự
phân biệt "lên" hay "xuống": liệt kê theo cặp hạng cũ → hạng mới và để người hỏi đọc.

```sql
SELECT o.membership_type_name AS hang_cu,
       n.membership_type_name AS hang_moi,
       COUNT(*)               AS so_khach
FROM dwh.fact_membership_type_changes ch
JOIN dwh.dim_membership_type o ON o.membership_type_key = ch.old_membership_type_key
JOIN dwh.dim_membership_type n ON n.membership_type_key = ch.new_membership_type_key
WHERE ch.change_ts >= (DATE '2026-11-01')::timestamp AT TIME ZONE 'Asia/Ho_Chi_Minh'
  AND ch.change_ts <  (DATE '2026-11-30' + 1)::timestamp AT TIME ZONE 'Asia/Ho_Chi_Minh'
GROUP BY o.membership_type_name, n.membership_type_name
ORDER BY so_khach DESC;
```

---

## Ví dụ SAI và cách sửa

### Sai 1: JOIN chi tiết rồi SUM tiền của bill (bị nhân dòng)
```sql
-- SAI: mỗi bill có nhiều món nên final_amount bị cộng lặp theo số món
SELECT SUM(s.final_amount)
FROM dwh.fact_sales s
JOIN dwh.fact_sale_items i ON i.sale_key = s.sale_key;
```
Sửa: tổng doanh thu chỉ query `dwh.fact_sales`. Cần cả số món thì gộp món theo `sale_key` trước (ví dụ 4).

### Sai 2: quên bill đã xoá và quên schema
```sql
-- SAI: không có dwh., không loại bill xoá
SELECT SUM(final_amount) FROM fact_sales;
```
Sửa: `FROM dwh.fact_sales s WHERE s.is_deleted = FALSE`.

### Sai 3: nhóm ngày theo giờ UTC
```sql
-- SAI: bill sau 17h (UTC) bị tính sang ngày hôm sau
SELECT tran_ts::date, SUM(final_amount) FROM dwh.fact_sales GROUP BY 1;
```
Sửa: nhóm theo `s.tran_date`, hoặc `(s.tran_ts AT TIME ZONE 'Asia/Ho_Chi_Minh')::date`.

### Sai 4: cộng doanh thu đơn online vào doanh thu bán hàng
```sql
-- SAI: đơn Grab đã nằm trong fact_sales, cộng thêm là tính đôi
SELECT (SELECT SUM(final_amount) FROM dwh.fact_sales)
     + (SELECT SUM(total_amount) FROM dwh.fact_online_orders);
```
Sửa: doanh thu theo kênh dùng `fact_sales` JOIN `dim_channel` (ví dụ 5).

### Sai 5: đếm khách gồm cả khách vãng lai
```sql
-- SAI: customer_key = -1 là "khách vãng lai", bị đếm thành 1 khách
SELECT COUNT(DISTINCT customer_key) FROM dwh.fact_sales;
```
Sửa: thêm `WHERE customer_key <> -1` và nói rõ đây là số khách thành viên.

### Sai 6: tính tổng hoặc tỉ trọng mà loại khoá -1
```sql
-- SAI: bỏ bill chưa gán kênh làm tổng doanh thu thấp hơn thực tế, tỉ trọng các kênh bị thổi lên
SELECT c.source_name, SUM(s.final_amount)
FROM dwh.fact_sales s JOIN dwh.dim_channel c ON c.channel_key = s.channel_key
WHERE s.is_deleted = FALSE AND s.channel_key <> -1
GROUP BY 1;
```
Sửa: chỉ loại `-1` khi XẾP HẠNG; khi tính tổng/tỉ trọng thì giữ và hiện "Không xác định" (ví dụ 5).

### Sai 7: so kỳ chưa kết thúc với cả kỳ trước
```sql
-- SAI: tuần này mới qua 3 ngày (09–11/11) nhưng đem so với cả 7 ngày tuần trước → luôn thấy "giảm mạnh"
SELECT SUM(s.final_amount) FILTER (WHERE s.tran_date >= DATE '2026-11-09')                         AS tuan_nay,
       SUM(s.final_amount) FILTER (WHERE s.tran_date BETWEEN DATE '2026-11-02' AND DATE '2026-11-08') AS tuan_truoc
FROM dwh.fact_sales s WHERE s.is_deleted = FALSE;
```
Sửa: kỳ trước cắt cùng số ngày (02–04/11), như ví dụ 10.

### Sai 8: lấy tổng tiền theo món làm doanh thu
```sql
-- SAI: giảm giá cả bill, phí dịch vụ, phí ship nằm ở cấp bill nên tổng này khác doanh thu
SELECT SUM(i.amount) AS doanh_thu
FROM dwh.fact_sale_items i JOIN dwh.fact_sales s ON s.sale_key = i.sale_key
WHERE s.is_deleted = FALSE;
```
Sửa: doanh thu luôn là SUM(`s.final_amount`) trên `fact_sales`; `i.amount` chỉ dùng cho doanh thu theo món.