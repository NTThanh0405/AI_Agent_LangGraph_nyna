# app/agent/tools/dashboard_presets.py
# Bố cục mẫu (preset) và bảng màu (theme) của dashboard.
#
# Báo cáo NYNA gồm 5 trang theo mẫu (REPORT_PAGES):
#   tong_quan  - Tổng quan doanh thu NYNA      cua_hang - Cửa hàng
#   nen_tang   - Nền tảng                         san_pham - Sản phẩm
#   hanh_vi    - Hành vi khách hàng
# Thêm 2 trang bổ sung: hoi_vien (khách hàng & hội viên), don_online (đơn online & đối tác).
#
# Cách dùng (tool gọi; LLM chỉ chọn trang + khoảng ngày):
#     payload = build_report("2026-11-02", "2026-11-08", run_query,
#                            logo=dashboard_template.logo_data_uri("nyna.png"),
#                            generated=..., expires=...)
#     html = dashboard_template.render_html(payload)
# run_query(sql) -> (columns, rows). Các trang dùng chung SQL chỉ truy vấn một lần.
# Một trang lẻ: build_dataset_sql(name, d_from, d_to) + build_dashboard(name, cols, rows).
#
# Quy ước dataset: mỗi dòng ở mức chi tiết, số đo phải CỘNG ĐƯỢC.
#  - `ky`     : 'nay' (kỳ này) | 'truoc' (kỳ trước, cùng độ dài, liền trước) | 'cu' (lịch sử cũ hơn,
#               chỉ dùng cho khối scope='all', vd. biểu đồ theo tuần);
#  - `ngay_qd`: ngày đã quy đổi về kỳ này (kỳ trước dịch tới n ngày) -> lọc ngày và trục ngày
#               tự khớp kỳ này / kỳ trước theo từng ngày;
#  - chỉ số không cộng được (AOV, số món/đơn, tỉ lệ...) khai báo agg="ratio" + denom.
# Trình duyệt tự lọc và cộng gộp theo bộ lọc, không gọi lại server.

from __future__ import annotations

import copy
import math
from datetime import date, datetime, timedelta

MAX_PERIOD_DAYS = 92          # kỳ dài nhất (kỳ trước cũng dài bằng)
MAX_DATASET_ROWS = 20_000     # số dòng tối đa của một dataset trang mẫu (create_dashboard cũng dùng)
HISTORY_WEEKS = 4             # biểu đồ "theo tuần" lấy ít nhất 4 tuần gần nhất

HOURS = [f"{h:02d}:00" for h in range(24)]
CA_LIST = ["Sáng (5–10h)", "Ngày (10–22h)", "Đêm (22–5h)"]
CA_HOURS = {"Sáng": 5, "Ngày": 12, "Đêm": 7}   # số giờ của mỗi khung, để tính "trung bình mỗi giờ"

# Màu: [sáng, tối]. number_style: 'en' -> 24.74M, 7,712,054 ; 'vi' -> 24,74 tr, 7.712.054
THEMES = {
    "default": {
        "brand": ["#1F6F6B", "#4FB3AC"],
        "prev": ["#A6A6A6", "#7D8A84"],
        "title": None,
        "heat": None,
        "number_style": "vi",
        "series_colors": {},
    },
    "nyna": {
        "brand": ["#C8202B", "#E5535C"],
        "prev": ["#A6A6A6", "#7D8A84"],
        "title": ["#C8202B", "#E5535C"],
        "heat": ["#C8202B", "#E5535C"],
        "number_style": "en",
        # Khớp theo tiền tố của giá trị cột color_by
        "series_colors": {
            "Sáng": ["#E8A33D", "#F0B95C"],
            "Ngày": ["#5B8DB8", "#7FAAD0"],
            "Đêm": ["#1F3A5F", "#6C8FC4"],
            "Grab": ["#00B14F", "#3DD17F"],
            "Shopee": ["#EE4D2D", "#F57A60"],
            "Xanh": ["#0F9BA8", "#3CC3CF"],
            "Be": ["#F5B800", "#FFD24D"],
            "Mới": ["#1B7F3B", "#5CC27E"],
            "Quay lại": ["#C8202B", "#E5535C"],
            "Vãng lai": ["#A6A6A6", "#7D8A84"],
        },
    },
}

COLUMN_LABELS = {
    "ngay_qd": "Ngày", "thu": "Thứ", "gio": "Giờ", "ca": "Khung giờ", "tuan": "Tuần",
    "cua_hang": "Cửa hàng", "nen_tang": "Nền tảng", "khu_vuc": "Khu vực",
    "doanh_thu": "Doanh thu Net", "so_don": "Số đơn", "so_mon": "Số món",
    "mon": "Món", "nhom_mon": "Nhóm món", "so_luong": "Số lượng", "giam_gia": "Giảm giá", "gia_von": "Giá vốn",
    "nhom_khach": "Nhóm khách", "hang_the": "Hạng thẻ", "tan_suat": "Tần suất mua",
    "so_khach": "Số khách HV", "so_khach_moi": "Khách mới", "so_don_hv": "Đơn hội viên",
    "doanh_thu_hv": "Doanh thu hội viên",
    "so_don_huy": "Đơn hủy", "gmv": "GMV", "km_quan": "KM quán chịu", "km_doi_tac": "KM đối tác chịu",
    "phut_xac_nhan": "Tổng phút xác nhận",
    "so_don_co_xac_nhan": "Đơn có giờ xác nhận",
}

# --------------------------------------------------------------------------------------
# SQL mẫu. Token được thay bằng literal ngày đã kiểm kiểu (không nhận chuỗi tùy ý):
#   __D_FROM__/__D_TO__ kỳ này; __P_FROM__ đầu kỳ trước; __N__ số ngày mỗi kỳ;
#   __H_FROM__ đầu dữ liệu lịch sử (căn đủ tuần, >= HISTORY_WEEKS tuần, phủ cả kỳ trước).
# Giờ quy về Asia/Ho_Chi_Minh.
# --------------------------------------------------------------------------------------
_PERIOD_CTE = """r AS (
  SELECT __D_FROM__ AS d_from, __D_TO__ AS d_to, __P_FROM__ AS p_from, __N__ AS n
)"""

# Dùng chung cho trang Tổng quan, Cửa hàng, Hành vi khách hàng.
_BAN_HANG_SQL = "WITH " + _PERIOD_CTE + """,
b AS (
  SELECT s.sale_key, s.store_key, s.channel_key, s.tran_date, s.final_amount,
         EXTRACT(HOUR FROM s.tran_ts AT TIME ZONE 'Asia/Ho_Chi_Minh')::int AS h
  FROM dwh.fact_sales s CROSS JOIN r
  WHERE NOT s.is_deleted
    AND s.tran_date BETWEEN r.p_from AND r.d_to
),
i AS (
  SELECT it.sale_key, SUM(it.quantity) AS so_mon
  FROM dwh.fact_sale_items it
  JOIN b ON b.sale_key = it.sale_key
  GROUP BY it.sale_key
)
SELECT
  CASE WHEN b.tran_date >= r.d_from THEN 'nay' ELSE 'truoc' END AS ky,
  CASE WHEN b.tran_date >= r.d_from THEN b.tran_date ELSE b.tran_date + r.n END AS ngay_qd,
  EXTRACT(ISODOW FROM b.tran_date)::int AS thu_stt,
  (ARRAY['T2','T3','T4','T5','T6','T7','CN'])[EXTRACT(ISODOW FROM b.tran_date)::int] AS thu,
  CASE WHEN b.h IS NULL THEN 'Không rõ' ELSE lpad(b.h::text, 2, '0') || ':00' END AS gio,
  CASE WHEN b.h IS NULL THEN 'Không rõ giờ'
       WHEN b.h BETWEEN 5 AND 9 THEN 'Sáng (5–10h)'
       WHEN b.h BETWEEN 10 AND 21 THEN 'Ngày (10–22h)'
       ELSE 'Đêm (22–5h)' END AS ca,
  st.store_name AS cua_hang,
  CASE WHEN ch.channel_key = -1 THEN 'Không xác định'
       ELSE COALESCE(NULLIF(ch.source_name, ''), NULLIF(ch.channel, ''), ch.source_id) END AS nen_tang,
  COALESCE(NULLIF(st.pos_city_name, ''), 'Chưa rõ') AS khu_vuc,
  SUM(b.final_amount) AS doanh_thu,
  COUNT(*) AS so_don,
  SUM(COALESCE(i.so_mon, 0)) AS so_mon
FROM b
CROSS JOIN r
JOIN dwh.dim_store st ON st.store_key = b.store_key
JOIN dwh.dim_channel ch ON ch.channel_key = b.channel_key
LEFT JOIN i ON i.sale_key = b.sale_key
GROUP BY 1, 2, 3, 4, 5, 6, 7, 8, 9
ORDER BY 2"""

# Trang Nền tảng: kỳ này + kỳ trước + lịch sử theo tuần (tuần tính lùi từ d_to, đủ 7 ngày).
_NEN_TANG_SQL = "WITH " + _PERIOD_CTE + """,
b AS (
  SELECT s.store_key, s.channel_key, s.tran_date, s.final_amount
  FROM dwh.fact_sales s
  WHERE NOT s.is_deleted
    AND s.tran_date BETWEEN __H_FROM__ AND __D_TO__
)
SELECT
  CASE WHEN b.tran_date >= r.d_from THEN 'nay'
       WHEN b.tran_date >= r.p_from THEN 'truoc'
       ELSE 'cu' END AS ky,
  CASE WHEN b.tran_date >= r.d_from THEN b.tran_date
       WHEN b.tran_date >= r.p_from THEN b.tran_date + r.n END AS ngay_qd,
  -((r.d_to - b.tran_date) / 7) AS tuan_stt,
  to_char(r.d_to - ((r.d_to - b.tran_date) / 7) * 7 - 6, 'DD/MM') || ' – ' ||
    to_char(r.d_to - ((r.d_to - b.tran_date) / 7) * 7, 'DD/MM') AS tuan,
  st.store_name AS cua_hang,
  CASE WHEN ch.channel_key = -1 THEN 'Không xác định'
       ELSE COALESCE(NULLIF(ch.source_name, ''), NULLIF(ch.channel, ''), ch.source_id) END AS nen_tang,
  COALESCE(NULLIF(st.pos_city_name, ''), 'Chưa rõ') AS khu_vuc,
  SUM(b.final_amount) AS doanh_thu,
  COUNT(*) AS so_don
FROM b
CROSS JOIN r
JOIN dwh.dim_store st ON st.store_key = b.store_key
JOIN dwh.dim_channel ch ON ch.channel_key = b.channel_key
GROUP BY 1, 2, 3, 4, 5, 6, 7
ORDER BY 2"""

_SAN_PHAM_SQL = "WITH " + _PERIOD_CTE + """,
s AS (
  SELECT s.sale_key, s.store_key, s.channel_key, s.tran_date
  FROM dwh.fact_sales s CROSS JOIN r
  WHERE NOT s.is_deleted
    AND s.tran_date BETWEEN r.p_from AND r.d_to
)
SELECT
  CASE WHEN s.tran_date >= r.d_from THEN 'nay' ELSE 'truoc' END AS ky,
  CASE WHEN s.tran_date >= r.d_from THEN s.tran_date ELSE s.tran_date + r.n END AS ngay_qd,
  st.store_name AS cua_hang,
  COALESCE(NULLIF(st.pos_city_name, ''), 'Chưa rõ') AS khu_vuc,
  CASE WHEN ch.channel_key = -1 THEN 'Không xác định'
       ELSE COALESCE(NULLIF(ch.source_name, ''), NULLIF(ch.channel, ''), ch.source_id) END AS nen_tang,
  COALESCE(NULLIF(di.item_name, ''), di.item_id) AS mon,
  COALESCE(NULLIF(di.item_type_id, ''), 'Chưa phân loại') AS nhom_mon,  -- đổi sang tên nhóm món khi DWH có
  CASE WHEN COALESCE(it.parent_item_id, '') <> '' THEN 'Món con / topping' ELSE 'Món chính' END AS loai_dong,
  SUM(it.quantity) AS so_luong,
  SUM(it.amount) AS doanh_thu,
  SUM(COALESCE(it.discount_amount, 0)) AS giam_gia
FROM s
CROSS JOIN r
JOIN dwh.fact_sale_items it ON it.sale_key = s.sale_key
JOIN dwh.dim_store st ON st.store_key = s.store_key
JOIN dwh.dim_channel ch ON ch.channel_key = s.channel_key
JOIN dwh.dim_item di ON di.item_key = it.item_key
WHERE it.is_gift IS NOT TRUE
GROUP BY 1, 2, 3, 4, 5, 6, 7, 8"""

# Trang bổ sung Hội viên: mỗi khách đếm 1 lần ở lần mua đầu tiên trong kỳ (so_khach cộng được).
_HOI_VIEN_SQL = "WITH " + _PERIOD_CTE + """,
b AS (
  SELECT CASE WHEN s.tran_date >= r.d_from THEN 'nay' ELSE 'truoc' END AS ky,
         CASE WHEN s.tran_date >= r.d_from THEN r.d_from ELSE r.p_from END AS ky_from,
         CASE WHEN s.tran_date >= r.d_from THEN s.tran_date ELSE s.tran_date + r.n END AS ngay_qd,
         s.sale_key, s.customer_key, s.store_key, s.membership_type_key, s.tran_date, s.tran_ts, s.final_amount
  FROM dwh.fact_sales s CROSS JOIN r
  WHERE NOT s.is_deleted
    AND s.tran_date BETWEEN r.p_from AND r.d_to
),
s AS (
  SELECT b.*,
         ROW_NUMBER() OVER (PARTITION BY ky, customer_key
                            ORDER BY tran_date, tran_ts NULLS LAST, sale_key) AS rn,
         COUNT(*) OVER (PARTITION BY ky, customer_key) AS so_don_ky,
         FIRST_VALUE(membership_type_key) OVER (PARTITION BY ky, customer_key
                            ORDER BY tran_date DESC, tran_ts DESC NULLS LAST, sale_key DESC) AS mt_key
  FROM b
),
lan_dau AS (
  SELECT f.customer_key, MIN(f.tran_date) AS ngay_mua_dau
  FROM dwh.fact_sales f
  WHERE NOT f.is_deleted
    AND f.customer_key IN (SELECT DISTINCT customer_key FROM b WHERE customer_key <> -1)
  GROUP BY f.customer_key
)
SELECT
  s.ky,
  s.ngay_qd,
  st.store_name AS cua_hang,
  COALESCE(NULLIF(st.pos_city_name, ''), 'Chưa rõ') AS khu_vuc,
  CASE WHEN s.customer_key = -1 THEN 'Vãng lai'
       WHEN ld.ngay_mua_dau >= s.ky_from THEN 'Mới'
       ELSE 'Quay lại' END AS nhom_khach,
  CASE WHEN s.customer_key = -1 THEN 'Vãng lai'
       WHEN COALESCE(s.mt_key, -1) = -1 THEN 'Chưa có hạng'
       ELSE COALESCE(NULLIF(mt.membership_type_name, ''), mt.membership_type_id) END AS hang_the,
  CASE WHEN s.customer_key = -1 THEN 'Vãng lai'
       WHEN s.so_don_ky = 1 THEN '1 lần'
       WHEN s.so_don_ky = 2 THEN '2 lần'
       WHEN s.so_don_ky <= 4 THEN '3–4 lần'
       ELSE '5+ lần' END AS tan_suat,
  CASE WHEN s.customer_key = -1 THEN 9
       WHEN s.so_don_ky = 1 THEN 1
       WHEN s.so_don_ky = 2 THEN 2
       WHEN s.so_don_ky <= 4 THEN 3
       ELSE 4 END AS tan_suat_stt,
  SUM(CASE WHEN s.customer_key <> -1 AND s.rn = 1 THEN 1 ELSE 0 END) AS so_khach,
  SUM(CASE WHEN s.customer_key <> -1 AND s.rn = 1 AND ld.ngay_mua_dau >= s.ky_from THEN 1 ELSE 0 END) AS so_khach_moi,
  COUNT(*) AS so_don,
  SUM(CASE WHEN s.customer_key = -1 THEN 0 ELSE 1 END) AS so_don_hv,
  SUM(s.final_amount) AS doanh_thu,
  SUM(CASE WHEN s.customer_key = -1 THEN 0 ELSE s.final_amount END) AS doanh_thu_hv
FROM s
LEFT JOIN lan_dau ld ON ld.customer_key = s.customer_key
JOIN dwh.dim_store st ON st.store_key = s.store_key
LEFT JOIN dwh.dim_membership_type mt ON mt.membership_type_key = s.mt_key
GROUP BY 1, 2, 3, 4, 5, 6, 7, 8
ORDER BY 2"""

# Trang bổ sung Đơn online. Trạng thái hủy suy từ chữ (CANCEL/REJECT/FAIL) — đối chiếu status thật.
_DON_ONLINE_SQL = "WITH " + _PERIOD_CTE + """,
o AS (
  SELECT x.store_key, x.channel_key, x.total_amount, x.merchant_promo_amount,
         x.partner_promo_amount, x.created_ts, x.time_confirmed_ts,
         (x.created_ts AT TIME ZONE 'Asia/Ho_Chi_Minh') AS c_local,
         (upper(COALESCE(x.order_status, '')) ~ '(CANCEL|REJECT|FAIL)') AS huy
  FROM dwh.fact_online_orders x CROSS JOIN r
  WHERE x.created_ts >= (r.p_from::timestamp AT TIME ZONE 'Asia/Ho_Chi_Minh')
    AND x.created_ts <  ((r.d_to + 1)::timestamp AT TIME ZONE 'Asia/Ho_Chi_Minh')
)
SELECT
  CASE WHEN o.c_local::date >= r.d_from THEN 'nay' ELSE 'truoc' END AS ky,
  CASE WHEN o.c_local::date >= r.d_from THEN o.c_local::date ELSE o.c_local::date + r.n END AS ngay_qd,
  lpad(EXTRACT(HOUR FROM o.c_local)::int::text, 2, '0') || ':00' AS gio,
  st.store_name AS cua_hang,
  COALESCE(NULLIF(st.pos_city_name, ''), 'Chưa rõ') AS khu_vuc,
  CASE WHEN ch.channel_key = -1 THEN 'Không xác định'
       ELSE COALESCE(NULLIF(ch.source_name, ''), NULLIF(ch.channel, ''), ch.source_id) END AS nen_tang,
  COUNT(*) AS so_don,
  COUNT(*) FILTER (WHERE o.huy) AS so_don_huy,
  COALESCE(SUM(o.total_amount) FILTER (WHERE NOT o.huy), 0) AS gmv,
  COALESCE(SUM(o.merchant_promo_amount) FILTER (WHERE NOT o.huy), 0) AS km_quan,
  COALESCE(SUM(o.partner_promo_amount) FILTER (WHERE NOT o.huy), 0) AS km_doi_tac,
  COALESCE(SUM(EXTRACT(EPOCH FROM (o.time_confirmed_ts - o.created_ts)) / 60.0)
           FILTER (WHERE o.time_confirmed_ts >= o.created_ts), 0) AS phut_xac_nhan,
  COUNT(*) FILTER (WHERE o.time_confirmed_ts >= o.created_ts) AS so_don_co_xac_nhan
FROM o
CROSS JOIN r
JOIN dwh.dim_store st ON st.store_key = o.store_key
JOIN dwh.dim_channel ch ON ch.channel_key = o.channel_key
GROUP BY 1, 2, 3, 4, 5, 6
ORDER BY 2"""

# --------------------------------------------------------------------------------------
# Khai báo trang.
#  filter : {label, column, type: date|select}
#  kpi    : {label, column, agg: sum|avg|ratio|count, denom?, format, good_when: up|down|neutral, hint?}
#  line/bar/pie/doughnut:
#           {title, type, x, y[], x_order?, x_format: date|weekday, x_values? (trục cố định),
#            agg?, denom?, compare (true | [cột]), color_by? (bar: chồng; line: mỗi nhóm 1 đường),
#            series_limit?, stacked?, sort: asc|desc, sort_by? (cột dùng để xếp), limit? (top N),
#            others?, horizontal?, hide_empty?, x_divisor? {tiền tố: số chia}, format?,
#            begin_at_zero?, labels?, legend?, height?, scope: 'all' (bỏ qua lọc ngày, dùng cả lịch sử),
#            where? {cột: giá trị} (lọc cố định riêng khối), null_as_zero? (khi xếp, nhãn không có số = 0)}
#  table  : {x, x_label?, columns: [{label, column, agg?, denom?, format, delta?, delta_label?,
#            share? (tỉ trọng trên tổng), good_when?}], total?, sort?, sort_by?, limit?}
#  pivot  : {row, col, row_label?, col_label?, values: [{label, column, agg?, denom?, format?,
#            share?: 'row'|'col', heat?}], total?, row_order?, sort?, limit?}
#  heatmap: {row, x, row_label?, row_order?, x_values?|x_order?, value: {column, agg?, denom?, format?}}
#  format : compact | number | decimal | decimal1 | percent | vnd | minutes
#  span 1..12 (lưới 12 cột)
# --------------------------------------------------------------------------------------
_F_NGAY = {"label": "Ngày", "column": "ngay_qd", "type": "date"}
_F_CH = {"label": "Cửa hàng", "column": "cua_hang", "type": "select"}
_F_NT = {"label": "Nền tảng", "column": "nen_tang", "type": "select"}
_F_KV = {"label": "Khu vực", "column": "khu_vuc", "type": "select"}

_AOV = {"agg": "ratio", "denom": "so_don"}

PRESETS = {
    "default": {
        "name": "Dashboard", "title": "Dashboard",
        "description": "Tự do: LLM tự khai báo khối, KPI, bộ lọc và tự viết dataset_sql.",
        "theme": "default", "filters": [], "kpis": [], "charts": [], "sql_template": None,
    },

    # ---------------- 1. Tổng quan doanh thu NYNA ----------------
    "tong_quan": {
        "name": "Tổng quan", "title": "Tổng quan doanh thu NYNA",
        "description": "4 KPI (Doanh thu Net, Số đơn, AOV, Số món/đơn) có so sánh kỳ trước; doanh thu theo "
                       "ngày (đường), theo cửa hàng, nền tảng, giờ (cột).",
        "theme": "nyna",
        "filters": [_F_NGAY, _F_CH, _F_NT, _F_KV],
        "kpis": [
            {"label": "Doanh thu Net", "column": "doanh_thu", "agg": "sum", "format": "compact"},
            {"label": "Số đơn", "column": "so_don", "agg": "sum", "format": "number"},
            {"label": "AOV", "column": "doanh_thu", **_AOV, "format": "compact"},
            {"label": "Số món/đơn", "column": "so_mon", **_AOV, "format": "decimal"},
        ],
        "charts": [
            {"title": "Doanh thu Net theo ngày", "type": "line",
             "x": "ngay_qd", "x_format": "weekday", "y": ["doanh_thu"], "compare": True, "span": 7},
            {"title": "Doanh thu Net theo cửa hàng", "type": "bar",
             "x": "cua_hang", "y": ["doanh_thu"], "compare": True, "sort": "desc", "span": 5},
            {"title": "Doanh thu Net theo nền tảng", "type": "bar",
             "x": "nen_tang", "y": ["doanh_thu"], "compare": True, "sort": "desc", "span": 4},
            {"title": "Doanh thu Net theo giờ", "type": "bar", "x": "gio", "x_values": HOURS,
             "y": ["doanh_thu"], "color_by": "ca", "span": 8},
        ],
        "sql_template": _BAN_HANG_SQL,
    },

    # ---------------- 2. Cửa hàng ----------------
    "cua_hang": {
        "name": "Cửa hàng", "title": "Cửa hàng",
        "description": "Số đơn, AOV, Số món/đơn theo cửa hàng (cột, kỳ này vs kỳ trước) + bảng Chỉ số "
                       "cửa hàng có ± kỳ trước và dòng Tổng.",
        "theme": "nyna",
        "filters": [_F_NGAY, _F_CH, _F_KV],
        "kpis": [],
        "charts": [
            {"title": "Số đơn theo cửa hàng", "type": "bar",
             "x": "cua_hang", "y": ["so_don"], "compare": True, "sort": "desc", "sort_by": "doanh_thu",
             "format": "number", "span": 6},
            {"title": "AOV theo cửa hàng", "type": "bar",
             "x": "cua_hang", "y": ["doanh_thu"], **_AOV, "compare": True, "sort": "desc",
             "sort_by": "doanh_thu", "span": 6},
            {"title": "Số món/đơn theo cửa hàng", "type": "bar",
             "x": "cua_hang", "y": ["so_mon"], **_AOV, "compare": True, "sort": "desc",
             "sort_by": "doanh_thu", "format": "decimal", "begin_at_zero": False, "span": 6},
            {"title": "Chỉ số cửa hàng", "type": "table", "x": "cua_hang", "x_label": "Tên cửa hàng",
             "columns": [
                 {"label": "Số đơn", "column": "so_don", "format": "number", "delta": True},
                 {"label": "AOV", "column": "doanh_thu", **_AOV, "format": "number", "delta": True},
                 {"label": "Số món/đơn", "column": "so_mon", **_AOV, "format": "decimal", "delta": True},
             ],
             "total": True, "sort": "desc", "sort_by": "doanh_thu", "span": 6},
        ],
        "sql_template": _BAN_HANG_SQL,
    },

    # ---------------- 3. Nền tảng ----------------
    "nen_tang": {
        "name": "Nền tảng", "title": "Nền tảng",
        "description": "Bảng chỉ số theo nền tảng (doanh thu, % so kỳ trước, tỉ trọng, số đơn, AOV); "
                       "doanh thu theo tuần của từng nền tảng (4 tuần gần nhất); bảng chéo cửa hàng x nền tảng.",
        "theme": "nyna",
        "filters": [_F_NGAY, _F_NT, _F_KV],
        "kpis": [],
        "charts": [
            {"title": "Chỉ số theo nền tảng", "type": "table", "x": "nen_tang", "x_label": "Tên nền tảng",
             "columns": [
                 {"label": "Doanh thu Net", "column": "doanh_thu", "format": "number",
                  "delta": True, "delta_label": "% so với kỳ trước"},
                 {"label": "Tỉ trọng nền tảng", "column": "doanh_thu", "share": True, "format": "percent"},
                 {"label": "Số đơn", "column": "so_don", "format": "number"},
                 {"label": "AOV", "column": "doanh_thu", **_AOV, "format": "number"},
             ],
             "total": True, "sort": "desc", "sort_by": "doanh_thu", "span": 12},
            {"title": "Doanh thu Net theo tuần và nền tảng", "type": "line", "scope": "all",
             "x": "tuan", "x_order": "tuan_stt", "y": ["doanh_thu"], "color_by": "nen_tang",
             "height": 240, "span": 12},
            {"title": "Doanh thu Net theo cửa hàng và nền tảng", "type": "pivot",
             "row": "cua_hang", "col": "nen_tang", "row_label": "Cửa hàng", "col_label": "Nền tảng",
             "values": [
                 {"label": "Doanh thu Net", "column": "doanh_thu", "format": "number"},
                 {"label": "Tỉ trọng nền tảng", "column": "doanh_thu", "share": "row", "heat": True},
             ],
             "span": 12},
        ],
        "sql_template": _NEN_TANG_SQL,
    },

    # ---------------- 4. Sản phẩm ----------------
    "san_pham": {
        "name": "Sản phẩm", "title": "Sản phẩm",
        "description": "Top 10 món bán chạy và bán thấp nhất, doanh thu theo nhóm món (cột ngang, kỳ này vs kỳ trước); bảng chéo "
                       "tỉ trọng nhóm món trong từng nền tảng.",
        "theme": "nyna",
        "filters": [_F_NGAY, _F_CH, _F_KV, {"label": "Nhóm món", "column": "nhom_mon", "type": "select"}],
        "kpis": [],
        "charts": [
            {"title": "Top 10 món theo Doanh thu Net", "type": "bar", "horizontal": True,
             "x": "mon", "y": ["doanh_thu"], "compare": True, "sort": "desc", "limit": 10, "span": 6},
            # Món bán chậm: chỉ món chính (bỏ topping/món con); món kỳ trước có bán mà kỳ này không
            # bán được cái nào vẫn hiện, xếp đầu danh sách (null_as_zero).
            {"title": "Top 10 món bán thấp nhất", "type": "bar", "horizontal": True,
             "x": "mon", "y": ["doanh_thu"], "compare": True, "sort": "asc", "null_as_zero": True,
             "where": {"loai_dong": "Món chính"}, "limit": 10, "span": 6},
            {"title": "Doanh thu Net theo nhóm món", "type": "bar", "horizontal": True,
             "x": "nhom_mon", "y": ["doanh_thu"], "compare": True, "sort": "desc", "limit": 15, "span": 12},
            {"title": "Tỉ trọng nhóm món trong từng nền tảng", "type": "pivot",
             "row": "nhom_mon", "col": "nen_tang", "row_label": "Nhóm món", "col_label": "Nền tảng",
             "values": [
                 {"label": "Doanh thu Net", "column": "doanh_thu", "format": "number"},
                 {"label": "Tỉ trọng nhóm món", "column": "doanh_thu", "share": "col", "heat": True},
             ],
             "total": True, "span": 12},
        ],
        "sql_template": _SAN_PHAM_SQL,
    },

    # ---------------- 5. Hành vi khách hàng ----------------
    "hanh_vi": {
        "name": "Hành vi khách hàng", "title": "Hành vi khách hàng",
        "description": "Heatmap số đơn theo thứ x giờ và cửa hàng x giờ; số đơn trung bình mỗi giờ theo "
                       "khung giờ (Sáng/Ngày/Đêm).",
        "theme": "nyna",
        "filters": [_F_NGAY, _F_CH, _F_KV],
        "kpis": [],
        "charts": [
            {"title": "Số đơn theo thứ và giờ", "type": "heatmap", "row": "thu", "row_order": "thu_stt",
             "row_label": "Thứ", "x": "gio", "x_values": HOURS,
             "value": {"column": "so_don", "format": "number"}, "span": 12},
            {"title": "Số đơn theo cửa hàng và giờ", "type": "heatmap", "row": "cua_hang",
             "row_label": "Tên cửa hàng", "x": "gio", "x_values": HOURS, "sort": "desc",
             "value": {"column": "so_don", "format": "number"}, "span": 9},
            {"title": "Số đơn trung bình mỗi giờ theo khung giờ", "type": "bar",
             "x": "ca", "x_values": CA_LIST, "y": ["so_don"], "color_by": "ca",
             "x_divisor": CA_HOURS, "format": "decimal1", "legend": False, "span": 3},
        ],
        "sql_template": _BAN_HANG_SQL,
    },

    # ---------------- Bổ sung: Khách hàng & hội viên ----------------
    "hoi_vien": {
        "name": "Hội viên", "title": "Khách hàng & hội viên",
        "description": "Khách hội viên, khách mới, tần suất mua, chi tiêu TB/khách, tỉ trọng doanh thu hội viên; "
                       "xu hướng theo ngày theo nhóm khách (Mới/Quay lại/Vãng lai), theo hạng thẻ, tần suất.",
        "theme": "nyna",
        "filters": [_F_NGAY, _F_CH, _F_KV,
                    {"label": "Hạng thẻ", "column": "hang_the", "type": "select"},
                    {"label": "Nhóm khách", "column": "nhom_khach", "type": "select"}],
        "kpis": [
            {"label": "Khách hội viên mua hàng", "column": "so_khach", "agg": "sum", "format": "number"},
            {"label": "Khách mới", "column": "so_khach_moi", "agg": "sum", "format": "number",
             "hint": "Lần mua đầu tiên rơi vào kỳ"},
            {"label": "Tần suất mua", "column": "so_don_hv", "agg": "ratio", "denom": "so_khach",
             "format": "decimal", "hint": "Đơn / khách hội viên"},
            {"label": "Chi tiêu TB / khách", "column": "doanh_thu_hv", "agg": "ratio", "denom": "so_khach",
             "format": "compact"},
            {"label": "Tỉ trọng DT hội viên", "column": "doanh_thu_hv", "agg": "ratio", "denom": "doanh_thu",
             "format": "percent"},
        ],
        "charts": [
            {"title": "Doanh thu Net theo ngày của từng nhóm khách", "type": "line",
             "x": "ngay_qd", "x_format": "weekday", "y": ["doanh_thu"], "color_by": "nhom_khach", "span": 6},
            {"title": "Khách mới theo ngày", "type": "line",
             "x": "ngay_qd", "x_format": "weekday", "y": ["so_khach_moi"], "compare": True,
             "format": "number", "span": 6},
            {"title": "Số khách theo tần suất mua", "type": "bar",
             "x": "tan_suat", "x_order": "tan_suat_stt", "y": ["so_khach"], "compare": True,
             "hide_empty": True, "format": "number", "span": 6},
            {"title": "Chi tiêu TB / khách theo hạng thẻ", "type": "bar",
             "x": "hang_the", "y": ["doanh_thu_hv"], "agg": "ratio", "denom": "so_khach",
             "compare": True, "sort": "desc", "hide_empty": True, "span": 6},
            {"title": "Chỉ số hội viên theo cửa hàng", "type": "table", "x": "cua_hang", "x_label": "Tên cửa hàng",
             "columns": [
                 {"label": "Khách HV", "column": "so_khach", "format": "number", "delta": True},
                 {"label": "Khách mới", "column": "so_khach_moi", "format": "number"},
                 {"label": "Chi tiêu TB/khách", "column": "doanh_thu_hv", "agg": "ratio", "denom": "so_khach",
                  "format": "number", "delta": True},
                 {"label": "Tỉ trọng DT HV", "column": "doanh_thu_hv", "agg": "ratio", "denom": "doanh_thu",
                  "format": "percent"},
             ],
             "total": True, "sort": "desc", "sort_by": "doanh_thu", "span": 12},
        ],
        "sql_template": _HOI_VIEN_SQL,
    },

    # ---------------- Bổ sung: Đơn online & đối tác ----------------
    "don_online": {
        "name": "Đơn online", "title": "Đơn online & đối tác giao hàng",
        "description": "Số đơn, GMV, tỉ lệ hủy, thời gian xác nhận theo ngày và nền tảng.",
        "theme": "nyna",
        "filters": [_F_NGAY, _F_CH, _F_NT, _F_KV],
        "kpis": [
            {"label": "Đơn online", "column": "so_don", "agg": "sum", "format": "number"},
            {"label": "GMV", "column": "gmv", "agg": "sum", "format": "compact", "hint": "Đơn không hủy"},
            {"label": "Tỉ lệ hủy", "column": "so_don_huy", "agg": "ratio", "denom": "so_don",
             "format": "percent", "good_when": "down"},
            {"label": "TG xác nhận TB", "column": "phut_xac_nhan", "agg": "ratio", "denom": "so_don_co_xac_nhan",
             "format": "minutes", "good_when": "down"},
        ],
        "charts": [
            {"title": "Đơn online theo ngày", "type": "line",
             "x": "ngay_qd", "x_format": "weekday", "y": ["so_don"], "compare": True, "format": "number", "span": 6},
            {"title": "Tỉ lệ hủy theo ngày", "type": "line",
             "x": "ngay_qd", "x_format": "weekday", "y": ["so_don_huy"], "agg": "ratio", "denom": "so_don",
             "compare": True, "format": "percent", "span": 6},
            {"title": "GMV theo nền tảng", "type": "bar",
             "x": "nen_tang", "y": ["gmv"], "compare": True, "sort": "desc", "span": 6},
            {"title": "Khuyến mãi: quán chịu vs đối tác chịu", "type": "bar",
             "x": "nen_tang", "y": ["km_quan", "km_doi_tac"], "stacked": True, "sort": "desc", "span": 6},
            {"title": "Đơn online theo giờ đặt", "type": "bar", "x": "gio", "x_values": HOURS,
             "y": ["so_don"], "compare": True, "format": "number", "span": 12},
            {"title": "Chỉ số theo cửa hàng", "type": "table", "x": "cua_hang", "x_label": "Tên cửa hàng",
             "columns": [
                 {"label": "Số đơn", "column": "so_don", "format": "number", "delta": True},
                 {"label": "Tỉ lệ hủy", "column": "so_don_huy", "agg": "ratio", "denom": "so_don",
                  "format": "percent"},
                 {"label": "GMV", "column": "gmv", "format": "number", "delta": True},
             ],
             "total": True, "sort": "desc", "sort_by": "gmv", "span": 12},
        ],
        "sql_template": _DON_ONLINE_SQL,
    },
}

# Thứ tự trang của báo cáo NYNA (theo mẫu)
REPORT_PAGES = ["tong_quan", "cua_hang", "nen_tang", "san_pham", "hanh_vi"]
# Mọi trang dựng sẵn được (có SQL mẫu): dùng cho create_dashboard(pages=...) và AnalysisBrief.dashboard_pages
PAGE_NAMES = [n for n, p in PRESETS.items() if p.get("sql_template")]

_CHART_TYPES = {"line", "bar", "pie", "doughnut", "table", "pivot", "heatmap"}
_AGGS = {"sum", "avg", "ratio", "count"}
_FORMATS = {"compact", "number", "decimal", "decimal1", "percent", "vnd", "minutes"}
_FILTER_TYPES = {"date", "select"}
_TOKENS = ("__D_FROM__", "__D_TO__", "__P_FROM__", "__N__")


# --------------------------------------------------------------------------------------
# Hàm tiện ích
# --------------------------------------------------------------------------------------
def preset_names() -> list[str]:
    return list(PRESETS)


def get_preset(name: str) -> dict:
    return PRESETS[name]


def get_theme(name: str) -> dict:
    return THEMES[name]


def _as_date(v) -> date:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if isinstance(v, str):
        return date.fromisoformat(v.strip()[:10])
    raise TypeError(f"Ngày không hợp lệ: {v!r}")


def period(d_from, d_to) -> dict:
    """Kỳ này [d_from, d_to], kỳ trước cùng độ dài liền trước, và mốc lịch sử theo tuần."""
    a, b = _as_date(d_from), _as_date(d_to)
    if b < a:
        raise ValueError("d_to phải >= d_from")
    n = (b - a).days + 1
    if n > MAX_PERIOD_DAYS:
        raise ValueError(f"Kỳ dài {n} ngày, tối đa {MAX_PERIOD_DAYS} ngày")
    weeks = max(HISTORY_WEEKS, math.ceil(2 * n / 7))
    return {"d_from": a, "d_to": b, "p_from": a - timedelta(days=n), "p_to": a - timedelta(days=1),
            "n": n, "h_from": b - timedelta(days=7 * weeks - 1)}


def last_full_week(today: date) -> tuple[date, date]:
    """Kỳ mặc định khi người dùng không nói kỳ: tuần trọn vẹn gần nhất (thứ Hai–Chủ nhật đã qua)."""
    end = today - timedelta(days=today.isoweekday())   # Chủ nhật gần nhất trước hôm nay
    return end - timedelta(days=6), end


def build_dataset_sql(name: str, d_from, d_to) -> str:
    """SQL dataset của trang cho kỳ [d_from, d_to] (gồm kỳ trước, và lịch sử nếu trang cần).
    Ngày được ép về kiểu date rồi mới ghép, nên không thể chèn SQL qua tham số này."""
    tpl = PRESETS[name].get("sql_template")
    if not tpl:
        raise ValueError(f"Preset '{name}' không có SQL dựng sẵn, cần tự viết dataset_sql")
    p = period(d_from, d_to)
    sql = tpl
    for token, value in (
        ("__D_FROM__", f"DATE '{p['d_from'].isoformat()}'"),
        ("__D_TO__", f"DATE '{p['d_to'].isoformat()}'"),
        ("__P_FROM__", f"DATE '{p['p_from'].isoformat()}'"),
        ("__H_FROM__", f"DATE '{p['h_from'].isoformat()}'"),
        ("__N__", str(int(p["n"]))),
    ):
        sql = sql.replace(token, value)
    return sql


def _spec_cols(s: dict | None) -> list:
    return [s.get("column"), s.get("denom")] if s else []


def required_columns(name: str) -> list[str]:
    """Các cột mà dataset_sql phải có để trang chạy được."""
    p = PRESETS[name]
    cols: list[str] = []

    def add(*names):
        for n in names:
            if n and n not in cols:
                cols.append(n)

    if p["kpis"] or any(c.get("compare") or c.get("type") == "table" for c in p["charts"]):
        add("ky")
    for f in p["filters"]:
        add(f["column"])
    for k in p["kpis"]:
        add(*_spec_cols(k))
    for c in p["charts"]:
        add(*(c.get("where") or {}).keys())
        add(c.get("x"), c.get("x_order"), c.get("color_by"), c.get("denom"), c.get("sort_by"),
            c.get("row"), c.get("col"), c.get("row_order"), *c.get("y", []))
        for s in c.get("columns", []) + c.get("values", []) + ([c["value"]] if c.get("value") else []):
            add(*_spec_cols(s))
    return cols


def build_dashboard(name: str, columns, rows, tab_name: str | None = None) -> dict:
    """Ghép một trang (đúng cấu trúc template cần) từ preset + kết quả truy vấn."""
    p = PRESETS[name]
    columns = list(columns)
    missing = [c for c in required_columns(name) if c not in columns]
    if missing:
        raise ValueError(f"dataset_sql thiếu cột cho preset '{name}': {', '.join(missing)}")
    rows = [list(r) for r in rows]
    if len(rows) > MAX_DATASET_ROWS:
        raise ValueError(
            f"Dataset có {len(rows)} dòng (> {MAX_DATASET_ROWS}). Thu hẹp kỳ, lọc bớt cửa hàng "
            "hoặc bỏ bớt chiều chi tiết (giờ, món...)."
        )
    return {
        "name": tab_name or p.get("name") or name,
        "title": p.get("title") or name,
        "theme": THEMES[p["theme"]],
        "labels": dict(COLUMN_LABELS),
        "filters": copy.deepcopy(p["filters"]),
        "kpis": copy.deepcopy(p["kpis"]),
        "charts": copy.deepcopy(p["charts"]),
        "dataset": {"columns": columns, "rows": rows},
    }


def build_report(d_from, d_to, run_query, pages=None, title: str = "Báo cáo hiệu suất NYNA",
                 logo: str | None = None, generated: str | None = None, expires: str | None = None) -> dict:
    """Dựng payload nhiều trang. run_query(sql) -> (columns, rows); trang dùng chung SQL chỉ chạy 1 lần."""
    pages = list(pages or REPORT_PAGES)
    cache: dict[str, tuple] = {}
    tabs = []
    for name in pages:
        sql = build_dataset_sql(name, d_from, d_to)
        if sql not in cache:
            cols, rows = run_query(sql)
            cache[sql] = (list(cols), list(rows))
        cols, rows = cache[sql]
        tabs.append(build_dashboard(name, cols, rows))
    return {"title": title, "logo": logo, "generated": generated, "expires": expires, "dashboards": tabs}


def describe_presets() -> str:
    """Mô tả các trang (cột bắt buộc + SQL mẫu) để LLM chọn trang hoặc viết dataset_sql đúng."""
    parts = [
        "Ưu tiên: dùng báo cáo dựng sẵn (trang " + ", ".join(REPORT_PAGES) + ") và chỉ đưa d_from/d_to "
        f"(YYYY-MM-DD, tối đa {MAX_PERIOD_DAYS} ngày); kỳ trước tự lấy cùng độ dài ngay trước đó. "
        "Chỉ tự viết dataset_sql khi cần biến thể; khi đó giữ đúng tên cột bắt buộc, số đo phải cộng "
        f"được, dưới {MAX_DATASET_ROWS} dòng."
    ]
    for name, p in PRESETS.items():
        lines = [f"## preset '{name}' — {p.get('title', name)}: {p['description']}"]
        cols = required_columns(name)
        if cols:
            lines.append("Cột dataset_sql bắt buộc: " + ", ".join(cols))
        if p.get("sql_template"):
            lines.append(
                "SQL mẫu (token __D_FROM__/__D_TO__ = kỳ này, __P_FROM__ = đầu kỳ trước, __N__ = số ngày "
                "mỗi kỳ, __H_FROM__ = đầu dữ liệu lịch sử theo tuần):\n" + p["sql_template"]
            )
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


def validate_presets() -> None:
    """Kiểm khai báo preset; ném ValueError liệt kê mọi lỗi."""
    errs: list[str] = []

    def chk_spec(where, s, need_label=True):
        if need_label and not s.get("label"):
            errs.append(f"{where}: thiếu label")
        if not s.get("column"):
            errs.append(f"{where}: thiếu column")
        if s.get("agg", "sum") not in _AGGS:
            errs.append(f"{where}: agg sai")
        if s.get("agg") == "ratio" and not s.get("denom"):
            errs.append(f"{where}: ratio thiếu denom")
        if s.get("format") and s["format"] not in _FORMATS:
            errs.append(f"{where}: format sai")

    for name, p in PRESETS.items():
        where = f"preset '{name}'"
        if p.get("theme") not in THEMES:
            errs.append(f"{where}: theme '{p.get('theme')}' không tồn tại")
        for f in p["filters"]:
            if f.get("type") not in _FILTER_TYPES:
                errs.append(f"{where}: filter '{f.get('label')}' type sai")
        for k in p["kpis"]:
            chk_spec(f"{where} KPI '{k.get('label')}'", k)
        for c in p["charts"]:
            w = f"{where} khối '{c.get('title', '?')}'"
            t = c.get("type")
            if t not in _CHART_TYPES:
                errs.append(f"{w}: type sai")
            elif t in ("line", "bar", "pie", "doughnut"):
                if not c.get("x") or not c.get("y"):
                    errs.append(f"{w}: thiếu x hoặc y")
                if c.get("agg") == "ratio" and not c.get("denom"):
                    errs.append(f"{w}: ratio thiếu denom")
            elif t == "table":
                for s in c.get("columns", []):
                    chk_spec(w, s)
            elif t == "pivot":
                if not c.get("row") or not c.get("col") or not c.get("values"):
                    errs.append(f"{w}: pivot cần row, col, values")
                for s in c.get("values", []):
                    chk_spec(w, s)
                    if s.get("share") not in (None, "row", "col"):
                        errs.append(f"{w}: share phải là 'row' hoặc 'col'")
            elif t == "heatmap":
                if not c.get("row") or not c.get("x") or not c.get("value"):
                    errs.append(f"{w}: heatmap cần row, x, value")
                else:
                    chk_spec(w, c["value"], need_label=False)
            if c.get("format") and c["format"] not in _FORMATS:
                errs.append(f"{w}: format sai")
            if not 1 <= int(c.get("span", 6)) <= 12:
                errs.append(f"{w}: span phải 1..12")
        tpl = p.get("sql_template")
        if tpl:
            for tok in _TOKENS:
                if tok not in tpl:
                    errs.append(f"{where}: sql_template thiếu {tok}")
    for n in REPORT_PAGES:
        if n not in PRESETS:
            errs.append(f"REPORT_PAGES có trang không tồn tại: {n}")
    if errs:
        raise ValueError("Preset khai báo sai:\n- " + "\n- ".join(errs))


validate_presets()

# Tương thích ngược: code cũ đọc p["sql_example"] (SQL đã có ngày cụ thể).
for _name, _p in PRESETS.items():
    _p["sql_example"] = (
        build_dataset_sql(_name, "2026-10-26", "2026-11-01") if _p.get("sql_template") else None
    )