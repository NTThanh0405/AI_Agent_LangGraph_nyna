# app/agent/tools/dashboard_presets.py
# Bố cục mẫu (preset) và bảng màu (theme) của dashboard.
# Một preset = danh sách bộ lọc + KPI + biểu đồ đã khai báo sẵn. LLM chỉ cần chọn preset
# và viết MỘT câu dataset_sql trả đúng các cột mà preset cần (xem list_dashboard_presets).
# Muốn thêm dashboard mẫu mới: thêm một mục vào PRESETS (và THEMES nếu cần màu riêng),
# KHÔNG phải sửa template hay tool.
#
# Quy ước dataset: mỗi dòng ở mức chi tiết (ngày x giờ x cửa hàng x nền tảng ...).
#  - cột `ky`     : 'nay' (kỳ này) hoặc 'truoc' (kỳ trước, cùng độ dài) để so sánh;
#  - cột `ngay_qd`: ngày đã quy đổi về kỳ này (kỳ trước dịch tới), dùng cho bộ lọc ngày.
# Trình duyệt tự cộng gộp theo bộ lọc, không gọi lại server.

# Màu: [sáng, tối]. Trang tự chọn theo chế độ sáng/tối của máy người xem.
THEMES = {
    "default": {
        "brand": ["#1F6F6B", "#4FB3AC"],
        "prev": ["#A6A6A6", "#7D8A84"],
        "title": None,
        "series_colors": {},
    },
    # Màu đỏ theo mẫu "Tổng quan hiệu suất FoodApp" (không logo)
    "nyna": {
        "brand": ["#C8202B", "#E5535C"],
        "prev": ["#A6A6A6", "#7D8A84"],
        "title": ["#C8202B", "#E5535C"],
        # Khớp theo tiền tố của giá trị cột color_by: "Sáng (5–10h)" khớp "Sáng"
        "series_colors": {
            "Sáng": ["#E8A33D", "#F0B95C"],
            "Ngày": ["#5B8DB8", "#7FAAD0"],
            "Đêm": ["#1F3A5F", "#6C8FC4"],
        },
    },
}

_TONG_QUAN_SQL = r"""SELECT
  CASE WHEN b.tran_date >= r.d_from THEN 'nay' ELSE 'truoc' END AS ky,
  b.tran_date AS ngay,
  CASE WHEN b.tran_date >= r.d_from THEN b.tran_date ELSE b.tran_date + r.n END AS ngay_qd,
  b.dow AS thu_stt,
  (ARRAY['T2','T3','T4','T5','T6','T7','CN'])[b.dow] AS thu,
  lpad(b.h::text, 2, '0') || ':00' AS gio,
  CASE WHEN b.h BETWEEN 5 AND 9 THEN 'Sáng (5–10h)'
       WHEN b.h BETWEEN 10 AND 21 THEN 'Ngày (10–22h)'
       ELSE 'Đêm (22–5h)' END AS ca,
  st.store_name AS cua_hang,
  COALESCE(NULLIF(ch.source_name, ''), ch.channel) AS nen_tang,
  st.pos_city_name AS khu_vuc,
  SUM(b.final_amount) AS doanh_thu,
  COUNT(*) AS so_don,
  SUM(COALESCE(i.so_mon, 0)) AS so_mon
FROM (
  SELECT s.sale_key, s.store_key, s.channel_key, s.tran_date, s.final_amount,
         EXTRACT(ISODOW FROM s.tran_date)::int AS dow,
         EXTRACT(HOUR FROM s.tran_ts AT TIME ZONE 'Asia/Ho_Chi_Minh')::int AS h
  FROM dwh.fact_sales s
  WHERE NOT s.is_deleted
    AND s.tran_date BETWEEN DATE '2026-10-19' AND DATE '2026-11-01'
) b
CROSS JOIN (SELECT DATE '2026-10-26' AS d_from, 7 AS n) r
JOIN dwh.dim_store st ON st.store_key = b.store_key
JOIN dwh.dim_channel ch ON ch.channel_key = b.channel_key
LEFT JOIN (
  SELECT it.sale_key, SUM(it.quantity) AS so_mon
  FROM dwh.fact_sale_items it
  WHERE it.sale_key IN (
    SELECT s2.sale_key FROM dwh.fact_sales s2
    WHERE s2.tran_date BETWEEN DATE '2026-10-19' AND DATE '2026-11-01')
  GROUP BY it.sale_key
) i ON i.sale_key = b.sale_key
GROUP BY 1, 2, 3, 4, 5, 6, 7, 8, 9, 10
ORDER BY 2"""

PRESETS = {
    # Không có gì sẵn: LLM tự khai báo charts (và kpis/filters nếu muốn).
    "default": {
        "description": "Tự do: LLM tự khai báo biểu đồ, KPI và bộ lọc.",
        "theme": "default",
        "filters": [],
        "kpis": [],
        "charts": [],
        "sql_example": None,
    },
    # Giống ảnh mẫu "Tổng quan hiệu suất FoodApp".
    "tong_quan": {
        "description": (
            "Tổng quan hiệu suất: 4 KPI (Doanh thu Net, Số đơn, AOV, Số món/đơn) có so sánh kỳ trước; "
            "biểu đồ theo ngày, theo cửa hàng, theo nền tảng, theo giờ; "
            "bộ lọc Ngày, Cửa hàng, Nền tảng, Khu vực. Kỳ này và kỳ trước dài bằng nhau."
        ),
        "theme": "nyna",
        "filters": [
            {"label": "Ngày", "column": "ngay_qd", "type": "date"},
            {"label": "Cửa hàng", "column": "cua_hang", "type": "select"},
            {"label": "Nền tảng", "column": "nen_tang", "type": "select"},
            {"label": "Khu vực", "column": "khu_vuc", "type": "select"},
        ],
        "kpis": [
            {"label": "Doanh thu Net", "column": "doanh_thu", "agg": "sum", "format": "compact"},
            {"label": "Số đơn", "column": "so_don", "agg": "sum", "format": "number"},
            {"label": "AOV", "column": "doanh_thu", "agg": "ratio", "denom": "so_don", "format": "compact"},
            {"label": "Số món/đơn", "column": "so_mon", "agg": "ratio", "denom": "so_don", "format": "decimal"},
        ],
        "charts": [
            {"title": "Doanh thu Net theo ngày · kỳ này và kỳ trước", "type": "line",
             "x": "thu", "x_order": "thu_stt", "y": ["doanh_thu"], "compare": True, "span": 7},
            {"title": "Doanh thu Net theo cửa hàng · kỳ này và kỳ trước", "type": "bar",
             "x": "cua_hang", "y": ["doanh_thu"], "compare": True, "sort": "desc", "span": 5},
            {"title": "Doanh thu Net theo nền tảng · kỳ này và kỳ trước", "type": "bar",
             "x": "nen_tang", "y": ["doanh_thu"], "compare": True, "sort": "desc", "span": 4},
            {"title": "Doanh thu Net theo giờ", "type": "bar",
             "x": "gio", "y": ["doanh_thu"], "color_by": "ca", "span": 8},
        ],
        "sql_example": _TONG_QUAN_SQL,
    },
}


def preset_names() -> list[str]:
    return list(PRESETS)


def get_preset(name: str) -> dict:
    """Trả preset theo tên; KeyError nếu không có."""
    return PRESETS[name]


def get_theme(name: str) -> dict:
    """Trả theme theo tên; KeyError nếu không có."""
    return THEMES[name]


def required_columns(name: str) -> list[str]:
    """Các cột mà dataset_sql phải có để preset chạy được."""
    p = PRESETS[name]
    cols: list[str] = []

    def add(*names):
        for n in names:
            if n and n not in cols:
                cols.append(n)

    if any(c.get("compare") for c in p["charts"]):
        add("ky")
    for f in p["filters"]:
        add(f["column"])
    for k in p["kpis"]:
        add(k["column"], k.get("denom"))
    for c in p["charts"]:
        add(c.get("x"), c.get("x_order"), c.get("color_by"), c.get("denom"), *c.get("y", []))
    return cols


def describe_presets() -> str:
    """Mô tả các preset (cột bắt buộc + SQL mẫu) để LLM viết dataset_sql đúng."""
    parts = []
    for name, p in PRESETS.items():
        lines = [f"## preset '{name}': {p['description']}"]
        cols = required_columns(name)
        if cols:
            lines.append("Cột dataset_sql bắt buộc: " + ", ".join(cols))
        if p.get("sql_example"):
            lines.append(
                "SQL mẫu (đổi ngày: kỳ này = d_from..d_to, kỳ trước = n ngày ngay trước đó, "
                "n = số ngày của kỳ này):\n" + p["sql_example"]
            )
        parts.append("\n".join(lines))
    return "\n\n".join(parts)