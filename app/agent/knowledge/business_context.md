# Bối cảnh doanh nghiệp

> Nạp cho agent để hiểu dữ liệu và cách tính số liệu.
> Quy tắc: chỉ khẳng định điều có trong tài liệu này hoặc trong kết quả truy vấn; điều không chắc thì nói rõ là chưa rõ, không đoán.
> Phần nạp cho agent chỉ ghi điều ĐÃ XÁC NHẬN. Điều chưa chắc để ở mục cuối (không nạp), xác nhận xong mới chuyển lên.

## 1. Doanh nghiệp
- Chuỗi đồ uống NYNA Coffee, bán tại cửa hàng và qua app giao đồ ăn.
- Danh sách cửa hàng, kênh bán, món, nhóm món thay đổi theo thời gian: luôn lấy từ `dim_store`, `dim_channel`, `dim_item`, không dựa vào danh sách ghi tay.

## 2. Nguồn dữ liệu
- Dữ liệu bán hàng từ hệ thống iPOS, đã gộp vào kho dữ liệu `dwh` (mô hình sao: bảng `dim_*` và `fact_*`). Agent chỉ đọc (SELECT), không ghi.
- Đơn vị tiền tệ: VND. Số lớn rút gọn (triệu, tỷ) khi trả lời cho dễ đọc, vẫn nêu số đầy đủ khi người dùng cần.
- Dữ liệu đồng bộ về DWH có độ trễ. Nếu ngày gần nhất có số thấp bất thường, nói rằng có thể dữ liệu chưa về đủ, không kết luận là bán kém.

## 3. Bảng chính
| Nhu cầu | Bảng | Ghi chú |
|---|---|---|
| Doanh thu, số bill, khách | `dwh.fact_sales` | Mỗi dòng là một bill; đã gồm cả đơn online và bill HUB |
| Doanh thu theo món | `dwh.fact_sale_items` (+ `dim_item`) | Lọc ngày và bill hợp lệ phải JOIN về `fact_sales`. Tổng tiền theo món KHÁC doanh thu bill (xem glossary) |
| Thanh toán | `dwh.fact_sale_payments` | Một bill có thể có nhiều dòng thanh toán |
| Trạng thái đơn online, đối soát | `dwh.fact_online_orders`, `dwh.fact_hub_bills` | KHÔNG dùng để tính doanh thu; chỉ xem vận hành (huỷ đơn, thời gian xác nhận) |
| Kênh, cửa hàng, nhân viên, khuyến mãi | `dim_channel`, `dim_store`, `dim_employee`, `dim_promotion` | JOIN qua `*_key` |
| Hội viên, voucher, đặt chỗ | `fact_member_vouchers`, `fact_membership_transactions`, `fact_membership_type_changes`, `fact_bookings`, `dim_voucher_campaign` | Voucher và booking chưa có dữ liệu mẫu, kiểm tra có dòng trước khi trả lời |

## 4. Cách tính số liệu
Dùng đúng định nghĩa trong `glossary.md`, không tự đổi công thức. Nếu hai cách tính cho kết quả khác nhau, nói rõ đang dùng cách nào.

## 5. Quy ước thời gian
- Múi giờ: Việt Nam (UTC+7). Lọc và nhóm ngày theo `tran_date`.
- Tuần bắt đầu từ thứ Hai, kết thúc Chủ nhật.
- Khoảng ngày cụ thể ("tuần này", "tháng trước") đã được hệ thống tính sẵn trong bản tóm tắt phân tích; dùng đúng khoảng đó, không tự tính lại.
- Người dùng không nói kỳ nào: dùng tuần trọn vẹn gần nhất (thứ Hai–Chủ nhật) và nói rõ kỳ đã dùng.
- "Hôm nay" thường chưa đủ ngày: số liệu kỳ chưa kết thúc là số tạm thời, phải nói rõ khi trả lời.
- So sánh: kỳ trước = cùng số ngày, liền ngay trước. Kỳ chưa kết thúc (tuần này, tháng này) thì so với kỳ trước cắt cùng độ dài.

## 6. Cách hiểu từ ngữ của người dùng
| Người dùng nói | Hiểu là |
|---|---|
| doanh số, doanh thu, tiền bán được | Doanh thu (glossary) |
| đơn, bill, hoá đơn | Số bill |
| app, nền tảng, kênh, Grab/Shopee | Kênh bán (`dim_channel`) |
| quán, chi nhánh, cơ sở | Cửa hàng (`dim_store`) |
| món, sản phẩm, đồ uống | Món (`dim_item`) |
| khách quen | Khách quay lại |
- Câu hỏi có thể hiểu theo nhiều cách (ví dụ "bán được bao nhiêu" là tiền hay số ly): chọn cách hợp lý nhất, nói rõ cách hiểu, gợi ý cách còn lại.

## 7. Dashboard có sẵn
Khi người dùng xin dashboard / báo cáo, chọn đúng trang, không dựng thêm trang không được hỏi:
| Người dùng hỏi | Trang |
|---|---|
| tổng quan, hiệu suất chung | `tong_quan` |
| cửa hàng, chi nhánh | `cua_hang` |
| nền tảng, kênh, Grab/Shopee | `nen_tang` |
| sản phẩm, món, món bán chạy/bán chậm | `san_pham` |
| giờ cao điểm, hành vi, khung giờ | `hanh_vi` |
| báo cáo đầy đủ / tất cả | cả 5 trang trên |
| hội viên, khách mới/quay lại | `hoi_vien` (bổ sung) |
| đơn app bị huỷ, thời gian xác nhận | `don_online` (bổ sung) |
- Dashboard là ảnh chụp số liệu tại lúc tạo, không tự cập nhật; link xem được bởi BẤT KỲ AI có link cho tới khi hết hạn.

## 8. Phân quyền
- `boss` và `manager`: xem toàn bộ bảng trong `dwh`.
- `staff`: chỉ xem bảng được cấp riêng. Hỏi ngoài quyền thì trả lời lịch sự rằng không có quyền, không cố truy vấn lách.
- Dashboard chỉ chứa dữ liệu trong quyền của người yêu cầu. Vì link chia sẻ được, không tạo dashboard có dữ liệu ngoài quyền của họ.
- Số điện thoại, email, địa chỉ, ngày sinh của khách bị che ở mức database; không truy vấn, không tiết lộ.

## 9. Cách đọc biến động
- Trước khi giải thích vì sao tăng/giảm, xem phần "Sự kiện trong kỳ" (nếu có) xem kỳ đó có lễ, khuyến mãi hoặc thay đổi vận hành không.
- Chỉ nêu nguyên nhân có bằng chứng trong dữ liệu (cửa hàng, sản phẩm, kênh, khung giờ nào kéo số liệu). Chưa đủ bằng chứng thì nói là giả thuyết.
- Tách được thì tách: doanh thu đổi do số bill hay do AOV; do cửa hàng/kênh nào.
- Không tự đặt ngưỡng cảnh báo hay mục tiêu.

## 10. Giới hạn cần nhớ
- Không bịa số khi không có dữ liệu; nói "chưa có số liệu" hoặc đề nghị chọn kỳ khác.
- Không tiết lộ thông tin ngoài quyền của người hỏi.
- Không cộng doanh thu từ `fact_online_orders` / `fact_hub_bills` vào `fact_sales`; không so tổng tiền theo món với doanh thu bill; không so GMV với doanh thu.

## Chưa rõ (chủ doanh nghiệp bổ sung; mục này KHÔNG được nạp cho agent)
Xác nhận xong mục nào thì chuyển câu trả lời lên phần trên (và sửa glossary nếu là công thức).

**Về doanh nghiệp** (gợi ý suy ra từ mẫu dashboard, cần xác nhận):
- Số cửa hàng, khu vực: mẫu có 6 cửa hàng (Nguyên Hồng, Minh Khai, Nguyễn Khắc Cần, Nguyễn Trãi, Trần Hưng Đạo, Nguỵ Như Kon Tum)?
- Kênh bán: tại quầy + GrabFood, ShopeeFood, Xanh SM? Còn kênh nào khác?
- Nhóm món: Classic Coffee, Creative Coffee, Cake, Combo, Fresh & Fruity, Seasonal, Brewed Tea, Matcha, Yuzu Selection, Fresh Milk Tea, Món Mặn? `item_type_id` có phải tên nhóm này không, hay chỉ là mã?
- Giờ mở cửa: mẫu có đơn lúc 0–4 giờ sáng. Có cửa hàng mở 24/7? Khung Sáng/Ngày/Đêm đang chia đúng ý chưa?
- Sản phẩm mùa vụ (ví dụ Seasonal Mùa Thu): mùa nào, để agent không nhầm hết mùa là bán kém.

**Về dữ liệu:**
- `final_amount` đúng là tiền khách thực trả sau giảm giá, đã gồm VAT? Có cần trừ hoàn trả không? Dashboard gọi là "Doanh thu Net" có đúng tên gọi nội bộ không?
- Cột `status` của `fact_sales`: mã nào là bill huỷ?
- Cách lưu topping trong `fact_sale_items`: bảng có cả `parent_item_id` và `topping_item_id`. Glossary, `bottom_products.sql`, `sql_examples.md` và dashboard đang dùng `parent_item_id` có giá trị = topping/món trong combo. Kiểm bằng: `SELECT item_key, parent_item_id, topping_item_id, amount FROM dwh.fact_sale_items WHERE parent_item_id <> '' OR topping_item_id <> '' LIMIT 50`. Số món/đơn có tính topping không?
- Tổng tiền giảm giá của bill là cột nào: `amount_discount_price`, `amount_discount_detail`, `amount_discount_extra` hay tổng các cột?
- Thứ tự các hạng thành viên (để phân biệt lên hạng / xuống hạng).
- `fact_sale_items.amount` đã trừ giảm giá của dòng chưa? `cost_price` là giá vốn mỗi đơn vị hay cả dòng?
- `fact_online_orders.order_status`: danh sách mã trạng thái, mã nào là huỷ?
- Độ trễ đồng bộ dữ liệu về DWH (bao nhiêu phút/giờ)?
- Ý nghĩa các bảng hội viên, voucher, đặt chỗ.
- Nguồn mục tiêu doanh thu (bảng trong dwh hay file).
- Quyền `staff`: được xem những bảng nào, cửa hàng nào?