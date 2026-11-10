# Bối cảnh doanh nghiệp

> Nạp cho agent để hiểu dữ liệu và cách tính số liệu.
> Quy tắc: chỉ khẳng định điều có trong tài liệu này hoặc trong kết quả truy vấn; điều không chắc thì nói rõ là chưa rõ, không đoán.

## 1. Nguồn dữ liệu
- Dữ liệu bán hàng từ hệ thống iPOS, đã gộp vào kho dữ liệu `dwh` (mô hình sao: bảng `dim_*` và `fact_*`). Agent chỉ đọc (SELECT), không ghi.
- Đơn vị tiền tệ: VND. Số lớn rút gọn (triệu, tỷ) khi trả lời cho dễ đọc, vẫn nêu số đầy đủ khi người dùng cần.

## 2. Bảng chính
| Nhu cầu | Bảng | Ghi chú |
|---|---|---|
| Doanh thu, số bill, khách | `dwh.fact_sales` | Mỗi dòng là một bill; đã gồm cả đơn online và bill HUB |
| Doanh thu theo món | `dwh.fact_sale_items` (+ `dim_item`) | Số lượng và tiền theo từng dòng món; lọc ngày và bill hợp lệ phải JOIN về `fact_sales` |
| Thanh toán | `dwh.fact_sale_payments` | Một bill có thể có nhiều dòng thanh toán |
| Trạng thái đơn online, đối soát | `dwh.fact_online_orders`, `dwh.fact_hub_bills` | KHÔNG dùng để tính doanh thu |
| Kênh, cửa hàng, nhân viên, khuyến mãi | `dim_channel`, `dim_store`, `dim_employee`, `dim_promotion` | JOIN qua `*_key` |
| Hội viên, voucher, đặt chỗ | `fact_member_vouchers`, `fact_membership_transactions`, `fact_membership_type_changes`, `fact_bookings`, `dim_voucher_campaign` | Voucher và booking chưa có dữ liệu mẫu, kiểm tra có dòng trước khi trả lời |

## 3. Cách tính số liệu chính
Dùng đúng định nghĩa trong `glossary.md`, không tự đổi công thức. Nếu hai cách tính cho kết quả khác nhau, nói rõ đang dùng cách nào.

## 4. Quy ước thời gian
- Múi giờ: Việt Nam (UTC+7). Lọc và nhóm ngày theo `tran_date`.
- Tuần bắt đầu từ thứ Hai, kết thúc Chủ nhật.
- Khoảng ngày cụ thể ("tuần này", "tháng trước") đã được hệ thống tính sẵn trong bản tóm tắt phân tích; dùng đúng khoảng đó, không tự tính lại.
- "Hôm nay" thường chưa đủ ngày: số liệu kỳ chưa kết thúc là số tạm thời, phải nói rõ khi trả lời.
- Khi so sánh kỳ chưa kết thúc (tuần này, tháng này), so với kỳ trước cắt cùng độ dài.

## 5. Phân quyền
- `boss` và `manager`: xem toàn bộ bảng trong `dwh`.
- `staff`: chỉ xem bảng được cấp riêng. Hỏi ngoài quyền thì trả lời lịch sự rằng không có quyền, không cố truy vấn lách.
- Số điện thoại, email, địa chỉ, ngày sinh của khách bị che ở mức database; không truy vấn, không tiết lộ.

## 6. Cách đọc biến động
- Trước khi giải thích vì sao tăng/giảm, xem phần "Sự kiện trong kỳ" (nếu có) xem kỳ đó có lễ, khuyến mãi hoặc thay đổi vận hành không.
- Chỉ nêu nguyên nhân có bằng chứng trong dữ liệu (cửa hàng, sản phẩm, kênh nào kéo số liệu). Chưa đủ bằng chứng thì nói là giả thuyết.
- Không tự đặt ngưỡng cảnh báo hay mục tiêu.

## 7. Giới hạn cần nhớ
- Không bịa số khi không có dữ liệu; nói "chưa có số liệu" hoặc đề nghị chọn kỳ khác.
- Không tiết lộ thông tin ngoài quyền của người hỏi.

## Chưa rõ (chủ doanh nghiệp bổ sung; mục này KHÔNG được nạp cho agent)
- Loại hình kinh doanh, số cửa hàng, khu vực hoạt động.
- Nhóm sản phẩm chính và tên gọi nội bộ; sản phẩm có tính mùa vụ.
- `final_amount` đúng là tiền khách thực trả sau giảm giá, đã gồm VAT? Có cần trừ hoàn trả không?
- Cột `status` của `fact_sales`: mã nào là bill huỷ?
- Cách lưu topping trong `fact_sale_items`.
- Độ trễ đồng bộ dữ liệu về DWH.
- Ý nghĩa các bảng hội viên, voucher, đặt chỗ.
- Nguồn mục tiêu doanh thu (bảng trong dwh hay file).
