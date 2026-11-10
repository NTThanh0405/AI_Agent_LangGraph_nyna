# Ví dụ SQL (PostgreSQL, schema dwh)

## Quy tắc bắt buộc khi viết SQL

1. Luôn ghi rõ schema: `dwh.fact_sales`, không viết `fact_sales`. Chỉ một câu SELECT.
2. Doanh thu bán hàng lấy từ `dwh.fact_sales` (alias `s`), cột `s.final_amount`.
   [CẦN XÁC NHẬN: final_amount là tiền khách thực trả sau giảm giá, đã gồm VAT.]
3. Bill hợp lệ: `s.is_deleted = FALSE`. Luôn có điều kiện này.
4. Ngày bán: dùng `s.tran_date` (kiểu DATE, đã là ngày giờ Việt Nam) để lọc và nhóm theo ngày.
   Giờ trong ngày: `EXTRACT(HOUR FROM s.tran_ts AT TIME ZONE 'Asia/Ho_Chi_Minh')`.
   Không dùng `tran_ts::date` để nhóm ngày (sẽ lệch múi giờ UTC).
5. Hôm nay (giờ VN): `(now() AT TIME ZONE 'Asia/Ho_Chi_Minh')::date`.
   Đầu tuần (thứ Hai): `date_trunc('week', now() AT TIME ZONE 'Asia/Ho_Chi_Minh')::date`.
   Đầu tháng: `date_trunc('month', now() AT TIME ZONE 'Asia/Ho_Chi_Minh')::date`.
6. Lọc khoảng ngày dạng `tran_date >= đầu_kỳ AND tran_date <= cuối_kỳ` (hoặc `< cuối_kỳ + 1`).
   Kỳ cụ thể thì gọi tool đổi "tuần này / tháng trước" ra ngày rồi điền vào.
7. Các dòng "Unknown" có khoá `-1` (khách vãng lai, cửa hàng/kênh/món không xác định).
   Khi đếm khách thì thêm `customer_key <> -1`; khi xếp hạng món thì thêm `item_key <> -1`.
8. KHÔNG cộng doanh thu từ `fact_online_orders` hay `fact_hub_bills`: đơn online và bill HUB
   đã nằm trong `fact_sales`, cộng thêm sẽ bị tính hai lần. Hai bảng đó chỉ dùng để xem
   trạng thái đơn online hoặc đối soát.
9. Khi JOIN sang bảng chi tiết (`fact_sale_items`, `fact_sale_payments`) thì KHÔNG
   SUM các cột của `fact_sales` (bị nhân dòng). Chỉ SUM cột của chính bảng chi tiết.
10. Không dùng `SELECT *`. Đặt alias cột không trùng nhau (ví dụ `doanh_thu`, `so_bill`).
11. Chia thì dùng `NULLIF(mẫu_số, 0)`. Số tiền làm tròn bằng `ROUND(x, 0)`.
12. Không truy vấn số điện thoại, email, địa chỉ, ngày sinh của khách: tài khoản chỉ-đọc đã bị che các cột này
    (truy vấn sẽ lỗi quyền). Cần nhận diện khách thì dùng `customer_name` và `membership_id`.

---

## 1. Tổng quan KPI một khoảng ngày

Hỏi: "Hôm nay bán được bao nhiêu?", "KPI tháng này?"
Lưu ý: AOV = doanh thu / số bill.

```sql
SELECT COUNT(*)                                        AS so_bill,
       ROUND(SUM(s.final_amount), 0)                   AS doanh_thu,
       ROUND(SUM(s.final_amount) / NULLIF(COUNT(*), 0), 0) AS aov,
       COUNT(DISTINCT s.customer_key) FILTER (WHERE s.customer_key <> -1) AS so_khach_thanh_vien
FROM dwh.fact_sales s
WHERE s.is_deleted = FALSE
  AND s.tran_date >= date_trunc('month', now() AT TIME ZONE 'Asia/Ho_Chi_Minh')::date
  AND s.tran_date <= (now() AT TIME ZONE 'Asia/Ho_Chi_Minh')::date;
```

## 2. Doanh thu theo ngày

Hỏi: "Doanh thu từng ngày tháng này?", "7 ngày qua bán thế nào?"

```sql
SELECT s.tran_date                      AS ngay,
       COUNT(*)                         AS so_bill,
       ROUND(SUM(s.final_amount), 0)    AS doanh_thu
FROM dwh.fact_sales s
WHERE s.is_deleted = FALSE
  AND s.tran_date >= (now() AT TIME ZONE 'Asia/Ho_Chi_Minh')::date - 6
  AND s.tran_date <= (now() AT TIME ZONE 'Asia/Ho_Chi_Minh')::date
GROUP BY s.tran_date
ORDER BY s.tran_date;
```

## 3. Doanh thu theo cửa hàng

Hỏi: "Cửa hàng nào bán tốt nhất tuần này?", "So sánh các chi nhánh."

```sql
SELECT st.store_name,
       COUNT(*)                         AS so_bill,
       ROUND(SUM(s.final_amount), 0)    AS doanh_thu
FROM dwh.fact_sales s
JOIN dwh.dim_store st ON st.store_key = s.store_key
WHERE s.is_deleted = FALSE
  AND s.store_key <> -1
  AND s.tran_date >= date_trunc('week', now() AT TIME ZONE 'Asia/Ho_Chi_Minh')::date
  AND s.tran_date <= (now() AT TIME ZONE 'Asia/Ho_Chi_Minh')::date
GROUP BY st.store_name
ORDER BY doanh_thu DESC;
```

## 4. Doanh thu theo kênh bán

Hỏi: "Doanh thu theo kênh?", "Grab chiếm bao nhiêu %?"
Lưu ý: kênh lấy từ `dim_channel` (qua `s.channel_key`). Chạy thử
`SELECT DISTINCT channel, source_name FROM dwh.dim_channel` để biết đúng tên kênh trước khi lọc.

```sql
SELECT c.channel,
       c.source_name,
       COUNT(*)                         AS so_bill,
       ROUND(SUM(s.final_amount), 0)    AS doanh_thu,
       ROUND(100.0 * SUM(s.final_amount)
             / NULLIF(SUM(SUM(s.final_amount)) OVER (), 0), 1) AS ty_trong_pct
FROM dwh.fact_sales s
JOIN dwh.dim_channel c ON c.channel_key = s.channel_key
WHERE s.is_deleted = FALSE
  AND s.tran_date >= date_trunc('month', now() AT TIME ZONE 'Asia/Ho_Chi_Minh')::date
  AND s.tran_date <= (now() AT TIME ZONE 'Asia/Ho_Chi_Minh')::date
GROUP BY c.channel, c.source_name
ORDER BY doanh_thu DESC;
```

## 5. Doanh thu theo khung giờ

Hỏi: "Giờ nào đông khách nhất?", "Ca sáng bán bao nhiêu?"

```sql
SELECT EXTRACT(HOUR FROM s.tran_ts AT TIME ZONE 'Asia/Ho_Chi_Minh')::int AS gio,
       COUNT(*)                         AS so_bill,
       ROUND(SUM(s.final_amount), 0)    AS doanh_thu
FROM dwh.fact_sales s
WHERE s.is_deleted = FALSE
  AND s.tran_date >= (now() AT TIME ZONE 'Asia/Ho_Chi_Minh')::date - 29
  AND s.tran_date <= (now() AT TIME ZONE 'Asia/Ho_Chi_Minh')::date
GROUP BY 1
ORDER BY 1;
```

## 6. Top món bán chạy

Hỏi: "Top 10 món bán chạy tuần này?"
Lưu ý: số lượng và tiền lấy từ `fact_sale_items` (alias `i`), còn lọc bill hợp lệ và ngày
phải JOIN về `fact_sales`. Loại món tặng (`is_gift`). Topping là dòng riêng nên số lượng
có thể gồm cả topping; nếu cần loại topping thì thêm `AND i.topping_item_id IS NULL`
[CẦN XÁC NHẬN cách lưu topping].

```sql
SELECT d.item_name,
       SUM(i.quantity)                  AS so_luong,
       ROUND(SUM(i.amount), 0)          AS doanh_thu
FROM dwh.fact_sale_items i
JOIN dwh.fact_sales s ON s.sale_key = i.sale_key
JOIN dwh.dim_item   d ON d.item_key = i.item_key
WHERE s.is_deleted = FALSE
  AND i.item_key <> -1
  AND COALESCE(i.is_gift, FALSE) = FALSE
  AND s.tran_date >= date_trunc('week', now() AT TIME ZONE 'Asia/Ho_Chi_Minh')::date
  AND s.tran_date <= (now() AT TIME ZONE 'Asia/Ho_Chi_Minh')::date
GROUP BY d.item_id, d.item_name
ORDER BY so_luong DESC
LIMIT 10;
```

## 7. So sánh với kỳ trước (cùng số ngày)

Hỏi: "Doanh thu tuần này so với tuần trước?"
Lưu ý: tuần này mới đi được vài ngày nên phải so với CÙNG số ngày đầu của tuần trước,
không so với cả tuần trước. % thay đổi = (nay - trước) / trước.

```sql
WITH b AS (
  SELECT (now() AT TIME ZONE 'Asia/Ho_Chi_Minh')::date AS hom_nay,
         date_trunc('week', now() AT TIME ZONE 'Asia/Ho_Chi_Minh')::date AS dau_tuan
),
r AS (
  SELECT SUM(s.final_amount) FILTER (
           WHERE s.tran_date >= b.dau_tuan AND s.tran_date <= b.hom_nay) AS tuan_nay,
         SUM(s.final_amount) FILTER (
           WHERE s.tran_date >= b.dau_tuan - 7
             AND s.tran_date <= b.hom_nay - 7)                          AS tuan_truoc
  FROM dwh.fact_sales s
  CROSS JOIN b
  WHERE s.is_deleted = FALSE
    AND s.tran_date >= b.dau_tuan - 7
    AND s.tran_date <= b.hom_nay
)
SELECT ROUND(tuan_nay, 0)   AS tuan_nay,
       ROUND(tuan_truoc, 0) AS tuan_truoc,
       ROUND(100.0 * (tuan_nay - tuan_truoc) / NULLIF(tuan_truoc, 0), 1) AS thay_doi_pct
FROM r;
```

## 8. Khách mới và khách quay lại

Hỏi: "Tháng này có bao nhiêu khách mới / khách quay lại?"
Lưu ý: khách mới = lần mua đầu tiên (trong dữ liệu DWH) rơi vào kỳ đang xét. Chỉ tính khách
thành viên (`customer_key <> -1`); khách vãng lai không nhận diện được. Nếu DWH chỉ có dữ liệu
từ một mốc nào đó thì "khách mới" có thể bị tính dư cho giai đoạn đầu.

```sql
WITH ky AS (
  SELECT date_trunc('month', now() AT TIME ZONE 'Asia/Ho_Chi_Minh')::date AS bat_dau,
         (now() AT TIME ZONE 'Asia/Ho_Chi_Minh')::date AS ket_thuc
),
lan_dau AS (
  SELECT s.customer_key, MIN(s.tran_date) AS ngay_dau
  FROM dwh.fact_sales s
  WHERE s.is_deleted = FALSE AND s.customer_key <> -1
  GROUP BY s.customer_key
),
khach_trong_ky AS (
  SELECT DISTINCT s.customer_key
  FROM dwh.fact_sales s
  CROSS JOIN ky
  WHERE s.is_deleted = FALSE AND s.customer_key <> -1
    AND s.tran_date >= ky.bat_dau AND s.tran_date <= ky.ket_thuc
)
SELECT COUNT(*) FILTER (WHERE l.ngay_dau >= ky.bat_dau)  AS khach_moi,
       COUNT(*) FILTER (WHERE l.ngay_dau <  ky.bat_dau)  AS khach_quay_lai
FROM khach_trong_ky k
JOIN lan_dau l ON l.customer_key = k.customer_key
CROSS JOIN ky;
```

## 9. Khách chi nhiều nhất

Hỏi: "Top 10 khách hàng chi tiêu nhiều nhất tháng này?"
Lưu ý: không lấy số điện thoại (bị che ở mức database); nhận diện khách bằng tên và mã hội viên.

```sql
SELECT c.customer_name,
       c.membership_id,
       COUNT(*)                           AS so_bill,
       ROUND(SUM(s.final_amount), 0)      AS tong_chi
FROM dwh.fact_sales s
JOIN dwh.dim_customer c ON c.customer_key = s.customer_key
WHERE s.is_deleted = FALSE
  AND s.customer_key <> -1
  AND s.tran_date >= date_trunc('month', now() AT TIME ZONE 'Asia/Ho_Chi_Minh')::date
  AND s.tran_date <= (now() AT TIME ZONE 'Asia/Ho_Chi_Minh')::date
GROUP BY c.customer_key, c.customer_name, c.membership_id
ORDER BY tong_chi DESC
LIMIT 10;
```

## 10. Doanh thu theo phương thức thanh toán

Hỏi: "Khách trả bằng tiền mặt hay chuyển khoản nhiều hơn?"
Lưu ý: SUM cột `amount` của `fact_sale_payments` (không SUM cột của `fact_sales`). Một bill
có thể có nhiều dòng thanh toán. Xem các giá trị bằng
`SELECT DISTINCT payment_method FROM dwh.fact_sale_payments` trước khi lọc theo tên.

```sql
SELECT p.payment_method,
       COUNT(DISTINCT p.sale_key)  AS so_bill,
       ROUND(SUM(p.amount), 0)     AS so_tien
FROM dwh.fact_sale_payments p
JOIN dwh.fact_sales s ON s.sale_key = p.sale_key
WHERE s.is_deleted = FALSE
  AND s.tran_date >= date_trunc('month', now() AT TIME ZONE 'Asia/Ho_Chi_Minh')::date
  AND s.tran_date <= (now() AT TIME ZONE 'Asia/Ho_Chi_Minh')::date
GROUP BY p.payment_method
ORDER BY so_tien DESC;
```

## 11. Hiệu quả khuyến mãi

Hỏi: "Chương trình khuyến mãi nào được dùng nhiều nhất?"
Lưu ý: `promotion_key = -1` là bill không có khuyến mãi, loại ra.

```sql
SELECT pr.promotion_name,
       COUNT(*)                              AS so_bill,
       ROUND(SUM(s.final_amount), 0)         AS doanh_thu,
       ROUND(SUM(s.amount_discount_price), 0) AS tien_giam
FROM dwh.fact_sales s
JOIN dwh.dim_promotion pr ON pr.promotion_key = s.promotion_key
WHERE s.is_deleted = FALSE
  AND s.promotion_key <> -1
  AND s.tran_date >= date_trunc('month', now() AT TIME ZONE 'Asia/Ho_Chi_Minh')::date
  AND s.tran_date <= (now() AT TIME ZONE 'Asia/Ho_Chi_Minh')::date
GROUP BY pr.promotion_name
ORDER BY so_bill DESC;
```

## 12. Doanh thu theo nhân viên

Hỏi: "Nhân viên nào thu ngân nhiều bill nhất?"

```sql
SELECT e.user_name,
       COUNT(*)                         AS so_bill,
       ROUND(SUM(s.final_amount), 0)    AS doanh_thu
FROM dwh.fact_sales s
JOIN dwh.dim_employee e ON e.employee_key = s.employee_key
WHERE s.is_deleted = FALSE
  AND s.employee_key <> -1
  AND s.tran_date >= date_trunc('month', now() AT TIME ZONE 'Asia/Ho_Chi_Minh')::date
  AND s.tran_date <= (now() AT TIME ZONE 'Asia/Ho_Chi_Minh')::date
GROUP BY e.employee_key, e.user_name
ORDER BY doanh_thu DESC
LIMIT 10;
```

## 13. Đơn online theo trạng thái

Hỏi: "Tuần này bao nhiêu đơn Grab bị huỷ?"
Lưu ý: đây là bảng đơn online (`fact_online_orders`), dùng để xem trạng thái, KHÔNG để tính
doanh thu. Chạy `SELECT DISTINCT order_status FROM dwh.fact_online_orders` để biết đúng tên
trạng thái (ví dụ CONFIRMED) trước khi nói đơn nào là "huỷ".

```sql
SELECT o.order_status,
       COUNT(*) AS so_don
FROM dwh.fact_online_orders o
WHERE (o.created_ts AT TIME ZONE 'Asia/Ho_Chi_Minh')::date
        >= date_trunc('week', now() AT TIME ZONE 'Asia/Ho_Chi_Minh')::date
GROUP BY o.order_status
ORDER BY so_don DESC;
```

## 14. Voucher theo chiến dịch

Hỏi: "Chiến dịch voucher nào được dùng nhiều nhất?"
Lưu ý: `status_name` có sẵn: 'Đã dùng', 'Hết hạn', 'Khả dụng'. Bảng này nguồn event 7 chưa
có dữ liệu mẫu nên kiểm tra có dòng nào chưa trước khi trả lời.

```sql
SELECT vc.campaign_name,
       v.status_name,
       COUNT(*) AS so_voucher
FROM dwh.fact_member_vouchers v
JOIN dwh.dim_voucher_campaign vc ON vc.campaign_key = v.campaign_key
GROUP BY vc.campaign_name, v.status_name
ORDER BY vc.campaign_name, so_voucher DESC;
```

## 15. Món hết hàng gần đây

Hỏi: "Tuần qua món nào hay hết hàng?"

```sql
SELECT d.item_name,
       st.store_name,
       COUNT(*) AS so_lan_het_hang
FROM dwh.fact_item_stock_events e
JOIN dwh.dim_item  d  ON d.item_key  = e.item_key
JOIN dwh.dim_store st ON st.store_key = e.store_key
WHERE e.status = 'OUT_OF_STOCK'
  AND (e.updated_ts AT TIME ZONE 'Asia/Ho_Chi_Minh')::date
        >= (now() AT TIME ZONE 'Asia/Ho_Chi_Minh')::date - 6
GROUP BY d.item_name, st.store_name
ORDER BY so_lan_het_hang DESC
LIMIT 10;
```

## 16. Lên hạng thành viên

Hỏi: "Tháng này có bao nhiêu khách lên hạng?"

```sql
SELECT o.membership_type_name AS hang_cu,
       n.membership_type_name AS hang_moi,
       COUNT(*)               AS so_khach
FROM dwh.fact_membership_type_changes ch
JOIN dwh.dim_membership_type o ON o.membership_type_key = ch.old_membership_type_key
JOIN dwh.dim_membership_type n ON n.membership_type_key = ch.new_membership_type_key
WHERE (ch.change_ts AT TIME ZONE 'Asia/Ho_Chi_Minh')::date
        >= date_trunc('month', now() AT TIME ZONE 'Asia/Ho_Chi_Minh')::date
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
Sửa: tổng doanh thu chỉ query `dwh.fact_sales`. Doanh thu theo món thì SUM `i.amount`.

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
Sửa: doanh thu theo kênh dùng `fact_sales` JOIN `dim_channel` (ví dụ 4).

### Sai 5: đếm khách gồm cả khách vãng lai
```sql
-- SAI: customer_key = -1 là "khách vãng lai", bị đếm thành 1 khách
SELECT COUNT(DISTINCT customer_key) FROM dwh.fact_sales;
```
Sửa: thêm `WHERE customer_key <> -1` và nói rõ đây là số khách thành viên.
