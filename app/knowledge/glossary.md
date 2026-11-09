# Bảng thuật ngữ

## Quy ước chung
- Tiền tệ: VND, số nguyên, không có phần thập phân.
- Múi giờ báo cáo: Asia/Ho_Chi_Minh (UTC+7). Cột `created_at` lưu UTC,
  luôn đổi múi giờ trước khi nhóm theo ngày.
- "Tuần" bắt đầu từ thứ Hai. "Tháng" là tháng dương lịch.
- Đơn hợp lệ: `orders.status IN ('completed','paid')`.
  Không tính 'cancelled', 'refunded', 'draft'.

## GMV (Gross Merchandise Value)
- Nghĩa: tổng giá trị hàng bán ra, TRƯỚC giảm giá, TRƯỚC hoàn trả.
- Công thức: SUM(orders.total_amount + orders.discount_amount)
- Lọc: đơn hợp lệ.
- Lưu ý: khác "Doanh thu". Khi người dùng nói "doanh số" mà không nói rõ
  thì hiểu là Doanh thu và nói rõ cách hiểu trong câu trả lời.

## Doanh thu (Revenue)
- Công thức: SUM(orders.total_amount) của đơn hợp lệ, đã trừ giảm giá, chưa trừ hoàn trả.
- Ghi chú: đã gồm / chưa gồm VAT (điền đúng thực tế).

## Số đơn
- COUNT(DISTINCT orders.id) của đơn hợp lệ.

## AOV (Giá trị đơn trung bình)
- Công thức: Doanh thu / Số đơn. Nếu Số đơn = 0 thì trả "không có dữ liệu", không chia cho 0.

## Khách mới
- Khách có đơn hợp lệ đầu tiên (MIN(created_at)) nằm trong kỳ đang xét.

## Khách quay lại
- Khách có đơn trong kỳ và đã có ít nhất 1 đơn hợp lệ TRƯỚC kỳ đó.

## Kênh bán
- Cột: orders.channel. Giá trị: 'pos' = tại quầy, 'app' = ứng dụng,
  'grab' = GrabFood, 'shopee' = ShopeeFood. (điền đúng thực tế)

## Mục tiêu (Target)
- Không nằm trong database, lấy từ config/targets.yaml qua tool.