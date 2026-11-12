# Bảng thuật ngữ

> Mỗi chỉ số: tên → công thức → bảng/cột → lưu ý. Bot, thư mục `queries/` và dashboard
> (`dashboard_presets.py`) phải dùng ĐÚNG các định nghĩa này; đổi công thức thì đổi cả ba nơi.

## Quy ước chung
- Tiền tệ: VND. Giá trong `dwh.fact_sales` đã GỒM VAT.
- Ngày: nhóm theo `fact_sales.tran_date` (DATE, đã là ngày Việt Nam). Giờ trong ngày: `tran_ts AT TIME ZONE 'Asia/Ho_Chi_Minh'`.
- Bill hợp lệ: `dwh.fact_sales.is_deleted = FALSE`. Chưa lọc theo `status` vì chưa rõ mã nào là bill huỷ.
- Doanh thu bán hàng lấy từ `dwh.fact_sales`. Đơn online và bill HUB đã nằm trong bảng này nên KHÔNG cộng thêm từ `fact_online_orders` hay `fact_hub_bills`.

## Giá trị "không xác định" (khoá -1)
- Cửa hàng, nhân viên, khách, kênh, món, khuyến mãi không xác định đều có khoá `-1`.
- Khi XẾP HẠNG (top cửa hàng, top món, kênh lớn nhất): loại `-1` ra.
- Khi tính TỔNG (tổng doanh thu, tổng số bill): GIỮ `-1`, nếu không tổng sẽ thấp hơn thực tế. Khi liệt kê theo nhóm, hiện `-1` thành một dòng riêng tên "Không xác định".

## Doanh thu (Revenue) — trên dashboard ghi là "Doanh thu Net"
- Công thức: SUM(`fact_sales.final_amount`) của bill hợp lệ.
- Ý nghĩa: tiền khách thực trả SAU giảm giá, ĐÃ GỒM VAT. Chữ "Net" trên dashboard nghĩa là "sau giảm giá", KHÔNG phải "sau thuế".
- Người dùng nói "doanh số", "doanh thu", "tiền bán được" mà không nói rõ → hiểu là Doanh thu này và nói rõ cách hiểu trong câu trả lời.

## Số đơn / Số bill
- COUNT(*) trên `fact_sales` của bill hợp lệ (mỗi dòng là một bill).

## AOV (Giá trị đơn trung bình)
- Doanh thu / Số bill. Số bill = 0 thì trả "không có dữ liệu", không chia cho 0.

## Số món/đơn
- SUM(`fact_sale_items.quantity`) / Số bill, chỉ tính dòng món thuộc bill hợp lệ.
- Hiện đang tính MỌI dòng món, kể cả topping và món tặng. Chưa chốt có nên bỏ topping hay không (xem mục Topping).

## Doanh thu theo món
- SUM(`fact_sale_items.amount`), JOIN về `fact_sales` để lọc ngày và bill hợp lệ; bỏ món tặng (`is_gift IS NOT TRUE`).
- Tổng doanh thu theo món KHÁC Doanh thu (bill), vì giảm giá cả bill, phí dịch vụ, phí ship nằm ở cấp bill. Không so hay cộng hai con số này với nhau. Hỏi "món X chiếm bao nhiêu % doanh thu" → chia cho tổng doanh thu theo món, và nói rõ đang chia cho cái gì.

## Món bán chạy / Món bán chậm
- Bán chạy: xếp Doanh thu theo món giảm dần (`queries/top_products.sql`).
- Bán chậm: xếp tăng dần, chỉ món chính (bỏ topping/món con), gồm cả món không bán được trong kỳ nhưng có bán trong 28 ngày trước đó (`queries/bottom_products.sql`).

## Topping / món con
- Dòng `fact_sale_items` có `parent_item_id` khác rỗng được coi là topping hoặc món trong combo. (Giả định, chưa xác nhận.)

## Nhóm món
- `dim_item.item_type_id`. Hiện là mã; có tên nhóm (Classic Coffee, Cake...) thì dùng tên.

## Kênh bán / Nền tảng
- "Kênh", "nền tảng", "app" là một. Lấy từ `dwh.dim_channel` qua `fact_sales.channel_key`.
- Tên hiển thị: `source_name`, rỗng thì `channel`, rỗng nữa thì `source_id`; `channel_key = -1` → "Không xác định".
- Tên kênh cụ thể (ví dụ GrabFood) phải xem bằng `SELECT DISTINCT channel, source_name FROM dwh.dim_channel` trước khi lọc, không đoán.

## Cửa hàng / Khu vực
- Cửa hàng: `dim_store.store_name` qua `fact_sales.store_key`.
- Khu vực: `dim_store.pos_city_name`; rỗng → "Chưa rõ".

## Khung giờ (ca)
- Sáng: 5:00–9:59 (5 giờ). Ngày: 10:00–21:59 (12 giờ). Đêm: 22:00–4:59 (7 giờ). Theo giờ của `tran_ts`.
- "Số đơn trung bình mỗi giờ" của một khung = số bill trong khung / số giờ của khung.

## Kỳ trước và % thay đổi
- Kỳ trước = khoảng có CÙNG SỐ NGÀY, liền ngay trước kỳ đang xét (tuần 02–08/11 → kỳ trước 26/10–01/11).
- % thay đổi = (kỳ này − kỳ trước) / kỳ trước. Kỳ trước = 0 hoặc không có → "không có số liệu kỳ trước".
- Chỉ số là tỉ lệ (tỉ trọng, tỉ lệ huỷ): nói chênh lệch bằng "điểm %" (25% → 27% là tăng 2 điểm %).

## Tỉ trọng
- Phần / tổng của cùng chỉ số trong cùng phạm vi. Luôn nói rõ "tỉ trọng trên cái gì" (ví dụ: GrabFood chiếm 81% doanh thu CỦA CỬA HÀNG Nguyên Hồng, khác với 81% doanh thu toàn chuỗi).

## Tuần
- Lịch: bắt đầu thứ Hai, kết thúc Chủ nhật. "Tháng" là tháng dương lịch.
- Biểu đồ "theo tuần" trên dashboard: các khối 7 ngày tính lùi từ ngày cuối kỳ. Trùng với tuần lịch khi kỳ kết thúc vào Chủ nhật.

## Khách
- Số khách: COUNT(DISTINCT `customer_key`) với `customer_key <> -1`.
- Khách mới: khách thành viên có bill hợp lệ đầu tiên (MIN(`tran_date`) trên toàn lịch sử) nằm trong kỳ. DWH chỉ có dữ liệu từ một mốc nên khách cũ giai đoạn đầu có thể bị tính là mới.
- Khách quay lại: khách thành viên có bill trong kỳ và đã có ít nhất 1 bill hợp lệ TRƯỚC kỳ.
- Khách vãng lai: `customer_key = -1` (không có mã hội viên hoặc khách giả của đối tác). Không đếm vào số khách, nhưng doanh thu của họ vẫn nằm trong tổng doanh thu.
- Tần suất mua: số bill của khách thành viên / số khách. Chi tiêu TB mỗi khách: doanh thu của khách thành viên / số khách.

## Đơn online (vận hành, KHÔNG phải doanh thu)
- Nguồn `dwh.fact_online_orders`. Chỉ dùng để xem vận hành đơn app; doanh thu vẫn lấy từ `fact_sales`.
- Tỉ lệ huỷ: số đơn có `order_status` chứa CANCEL/REJECT/FAIL / tổng số đơn. (Giả định theo tên trạng thái, chưa xác nhận danh sách mã.)
- GMV (giá trị đơn): SUM(`total_amount`) của đơn không huỷ. GMV KHÔNG phải doanh thu, không so trực tiếp với doanh thu.
- Thời gian xác nhận TB: trung bình (`time_confirmed_ts` − `created_ts`), tính bằng phút, chỉ tính đơn có giờ xác nhận.

## Mục tiêu (Target)
- Chưa có nguồn dữ liệu mục tiêu trong hệ thống. Người dùng hỏi về mục tiêu thì nói rõ là chưa có số liệu mục tiêu.
