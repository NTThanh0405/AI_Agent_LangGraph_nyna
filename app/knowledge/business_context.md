# Bối cảnh doanh nghiệp

> File này nạp cho agent (qua context_tools.py) để hiểu dữ liệu và cách tính số liệu.
> Mục có dấu [ĐIỀN] là thông tin chưa biết, cần chủ doanh nghiệp bổ sung.
> Quy tắc: agent chỉ khẳng định điều có trong file này hoặc trong kết quả truy vấn; điều gì không chắc thì nói rõ là chưa rõ, không đoán.

## 1. Doanh nghiệp
- Loại hình: [ĐIỀN: ví dụ chuỗi bán lẻ / F&B / phòng tập / không gian làm việc chung...]
- Số cửa hàng / chi nhánh: [ĐIỀN]
- Khu vực hoạt động: [ĐIỀN]
- Đơn vị tiền tệ: VND. Hiển thị số tiền có dấu phân cách hàng nghìn; số lớn rút gọn (triệu, tỷ) khi trả lời cho dễ đọc.

## 2. Kênh bán
| Kênh | Bảng dữ liệu chính | Ghi chú |
|---|---|---|
| Tại cửa hàng | `dwh.fact_sales`, `dwh.fact_sale_items` | Mỗi dòng `fact_sales` là một giao dịch; chi tiết từng sản phẩm nằm ở `fact_sale_items` |
| Online | `dwh.fact_online_orders` | Đơn đặt qua kênh online |
| [ĐIỀN nếu có kênh khác] | | |

## 3. Nhóm sản phẩm / dịch vụ
- [ĐIỀN: các nhóm sản phẩm chính và tên gọi nội bộ]
- Sản phẩm/dịch vụ có tính mùa vụ: [ĐIỀN]

## 4. Cách tính số liệu chính
Agent phải dùng đúng các định nghĩa dưới đây, không tự đổi công thức. Nếu hai cách tính cho kết quả khác nhau, nói rõ đang dùng cách nào.

| Chỉ số | Định nghĩa | Cột / bảng dùng |
|---|---|---|
| Doanh thu | [ĐIỀN: trước hay sau giảm giá, có gồm VAT không, có trừ hoàn trả không] | [ĐIỀN] |
| Số đơn | [ĐIỀN: đếm giao dịch nào, có loại đơn huỷ không] | [ĐIỀN] |
| AOV (giá trị đơn trung bình) | Doanh thu ÷ Số đơn | |
| Khách mới | [ĐIỀN: khách có giao dịch đầu tiên trong kỳ] | `dwh.dim_customer` |
| Khách quay lại | [ĐIỀN: khách đã có giao dịch trước kỳ] | |
| Tỷ lệ tăng giảm | (Kỳ này − Kỳ trước) ÷ Kỳ trước; kỳ trước bằng 0 thì ghi "không so sánh được", không tính % | |

Chi tiết thuật ngữ xem `glossary.md`.

## 5. Quy ước thời gian
- Múi giờ: Việt Nam (UTC+7). Cột giờ giao dịch (ví dụ `tran_ts`) có thể lưu UTC; phải đổi sang giờ Việt Nam rồi mới lọc theo ngày.
- Tuần bắt đầu từ thứ Hai, kết thúc Chủ nhật.
- "Hôm nay" thường chưa đủ ngày: luôn gọi `get_date_range` để lấy khoảng ngày và cờ "chưa đủ ngày".
- Khi so sánh kỳ chưa kết thúc (tuần này, tháng này), so với kỳ trước cắt cùng độ dài, không so với kỳ trước đủ ngày.
- Độ trễ dữ liệu: [ĐIỀN: dữ liệu được đồng bộ về DWH lúc nào, có thể trễ bao lâu]

## 6. Nguồn dữ liệu và phân quyền
- Schema dữ liệu: `dwh`. Agent chỉ đọc (SELECT), không ghi.
- Vai trò `boss`: xem toàn bộ bảng trong `dwh`.
- Vai trò `manager`: chỉ xem các bảng bán hàng và danh mục: `fact_sales`, `fact_sale_items`, `fact_online_orders`, `dim_store`, `dim_customer`.
- Các bảng hội viên, voucher, đặt chỗ, hóa đơn hub (ví dụ `fact_member_vouchers`, `fact_membership_transactions`, `fact_membership_type_changes`, `fact_hub_bills`, `fact_bookings`, `dim_voucher_campaign`) chỉ `boss` xem được. Nếu `manager` hỏi về nhóm này, trả lời lịch sự rằng không có quyền, không cố truy vấn lách.
- Ý nghĩa các bảng hội viên, voucher, booking: [ĐIỀN]

## 7. Cách đọc biến động
- Trước khi giải thích vì sao tăng/giảm, kiểm tra `calendar.yaml` xem kỳ đó có lễ, khuyến mãi hoặc thay đổi vận hành không.
- Chỉ nêu nguyên nhân có bằng chứng trong dữ liệu (cửa hàng nào, sản phẩm nào, kênh nào kéo số liệu). Chưa đủ bằng chứng thì nói là giả thuyết.
- Ngưỡng cảnh báo lấy từ `config/thresholds.yaml`, mục tiêu lấy từ `config/targets.yaml`. Không tự đặt ngưỡng hay mục tiêu.

## 8. Giới hạn cần nhớ
- Số liệu kỳ chưa kết thúc là số tạm thời, phải nói rõ khi trả lời.
- Không bịa số khi không có dữ liệu; nói "chưa có số liệu" hoặc đề nghị chọn kỳ khác.
- Không tiết lộ thông tin ngoài quyền của người hỏi (xem mục 6).
