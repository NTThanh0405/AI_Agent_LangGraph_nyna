# Bảng thuật ngữ

## Quy ước chung
- Tiền tệ: VND. Giá trong `dwh.fact_sales` đã GỒM VAT.
- Múi giờ báo cáo: Asia/Ho_Chi_Minh (UTC+7). Nhóm theo ngày dùng `tran_date` (kiểu DATE, đã là ngày giờ Việt Nam).
- "Tuần" bắt đầu từ thứ Hai. "Tháng" là tháng dương lịch.
- Bill hợp lệ: `dwh.fact_sales.is_deleted = FALSE`. Chưa lọc theo `status` vì chưa rõ mã nào là bill huỷ.
- Doanh thu bán hàng lấy từ `dwh.fact_sales`. Đơn online và bill HUB đã nằm trong bảng này nên KHÔNG cộng thêm từ `fact_online_orders` hay `fact_hub_bills`.

## Doanh thu (Revenue)
- Công thức: SUM(`fact_sales.final_amount`) của bill hợp lệ (tiền khách thực trả sau giảm giá, đã gồm VAT).
- Khi người dùng nói "doanh số" mà không nói rõ thì hiểu là Doanh thu và nói rõ cách hiểu trong câu trả lời.

## Số đơn / Số bill
- COUNT(*) trên `fact_sales` của bill hợp lệ (mỗi dòng là một bill).

## AOV (Giá trị đơn trung bình)
- Doanh thu / Số bill. Số bill = 0 thì trả "không có dữ liệu", không chia cho 0.

## Khách mới
- Khách thành viên (`customer_key <> -1`) có bill hợp lệ đầu tiên (MIN(`tran_date`)) nằm trong kỳ đang xét.
- Dữ liệu DWH chỉ có từ một mốc nào đó nên khách cũ giai đoạn đầu có thể bị tính là mới.

## Khách quay lại
- Khách thành viên có bill trong kỳ và đã có ít nhất 1 bill hợp lệ TRƯỚC kỳ đó.

## Khách vãng lai
- `customer_key = -1`: khách không có mã hội viên, hoặc khách giả của đối tác. Không đếm vào số khách.

## Kênh bán
- Lấy từ `dwh.dim_channel` qua `fact_sales.channel_key` (cột `channel`, `source_name`). `channel_key = -1` là chưa xác định kênh.
- Tên kênh cụ thể (ví dụ GrabFood) phải xem bằng `SELECT DISTINCT channel, source_name FROM dwh.dim_channel` trước khi lọc, không đoán.

## Giá trị "Unknown" (khoá -1)
- Cửa hàng, nhân viên, khách, kênh, món, khuyến mãi không xác định đều có khoá `-1`. Khi xếp hạng hoặc đếm thì loại `-1` ra.

## Mục tiêu (Target)
- Chưa có nguồn dữ liệu mục tiêu trong hệ thống. Người dùng hỏi về mục tiêu thì nói rõ là chưa có số liệu mục tiêu.
