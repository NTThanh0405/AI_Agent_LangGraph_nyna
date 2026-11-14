# Nyna AI Agent — trợ lý phân tích dữ liệu kinh doanh trên Telegram

Bot Telegram trả lời câu hỏi kinh doanh của chuỗi Nyna bằng số liệu thật trong kho dữ liệu iPOS,
xây bằng **LangGraph + Claude + PostgreSQL**, chạy 24/7 trên Windows Server (hoặc Docker).

Người dùng nhắn tự nhiên, ví dụ *"doanh thu tuần này so với tuần trước?"*, *"món nào bán chậm tháng 10?"*,
*"cho tôi dashboard tổng quan"*. Bot hiểu câu hỏi, tự viết và chạy câu `SELECT` (chỉ đọc), rồi trả lời
kèm số liệu hoặc gửi link dashboard. Mỗi sáng bot tự gửi **báo cáo sáng** cho các boss và manager đã đăng ký.

> - Không dùng n8n: bot nhận tin trực tiếp từ Telegram bằng long polling, không cần mở port hay webhook.
> - Ngoài báo cáo sáng, bot chỉ làm việc khi có người hỏi; không có cảnh báo tự động nào khác.

---

## Mục lục

1. [Tính năng](#tính-năng)
2. [Kiến trúc](#kiến-trúc)
3. [Cấu trúc thư mục](#cấu-trúc-thư-mục)
4. [Lệnh Telegram](#lệnh-telegram)
5. [Phân quyền](#phân-quyền)
6. [Dashboard](#dashboard)
7. [Báo cáo sáng](#báo-cáo-sáng)
8. [Tri thức nghiệp vụ và prompt](#tri-thức-nghiệp-vụ-và-prompt)
9. [Cài đặt](#cài-đặt)
10. [Biến môi trường](#biến-môi-trường)
11. [Vận hành](#vận-hành)
12. [Kiểm thử và đánh giá](#kiểm-thử-và-đánh-giá)
13. [Xử lý sự cố](#xử-lý-sự-cố)
14. [Trạng thái và việc còn dở](#trạng-thái-và-việc-còn-dở)

---

## Tính năng

- **Hỏi đáp bằng tiếng Việt tự nhiên** qua Telegram, chỉ chat riêng, chỉ người có trong danh sách.
- **Nhiều agent chuyên trách** (LangGraph): Analyst hiểu câu hỏi, Coder viết và chạy SQL, Designer dựng dashboard,
  bước cuối soạn câu trả lời. Mỗi agent có model, số bước và timeout riêng (`config/agents.yaml`).
- **Thời gian do code tính, không để LLM đoán**: "hôm qua", "tuần này", "tháng 10/2026", "từ 1/11 đến 5/11"...
  được đổi ra ngày bằng `app/agent/periods.py` (giờ Việt Nam, tuần bắt đầu thứ Hai).
- **Dashboard HTML** dựng từ template cố định, gửi bằng link (Cloudflare Tunnel): 5 trang theo mẫu
  (Tổng quan, Cửa hàng, Nền tảng, Sản phẩm, Hành vi khách hàng) + 2 trang bổ sung; có bộ lọc, so sánh kỳ trước,
  heatmap, bảng chéo, tải CSV; sáng/tối theo máy người xem.
- **Báo cáo sáng** cho boss và manager: SQL viết sẵn, không qua LLM; đúng giờ, không gửi trùng, gửi bù khi restart;
  so với mục tiêu tháng khai báo trong `config/targets.yaml`; gõ `/reply` để hỏi tiếp trong ngữ cảnh báo cáo.
- **Phân quyền 3 tầng**: role (`boss`, `manager`, `staff`), cấp quản trị (`owner`, `admin`, thường) và
  **khối dữ liệu** cấp riêng cho nhân viên (có hạn dùng).
- **An toàn**: chỉ `SELECT` (guard bằng sqlglot), kết nối PostgreSQL chỉ-đọc, che cột cá nhân của khách,
  `statement_timeout`, giới hạn số dòng, rate limit, audit log.

---

## Kiến trúc

```
Telegram (boss / manager / staff)
      │ long polling
      ▼
┌────────────────────────── tiến trình agent (1 worker) ──────────────────────────┐
│ telegram/bot ─► middleware: chỉ chat riêng, chỉ người trong agent_state.users    │
│      │          lệnh cố định (/new, /sessions, /capquyen...) xử lý bằng code     │
│      ▼                                                                          │
│ runner: khoá theo phiên, rate limit, giới hạn bước + timeout, báo "đang phân tích"│
│      ▼                                                                          │
│ LangGraph (graph v2)                                                            │
│   START ─► supervisor ─┬─► analyst ─────────────────────► supervisor            │
│                        ├─► coder ◄──► coder_tools ───────► supervisor            │
│                        ├─► designer ◄──► designer_tools ─► supervisor            │
│                        ├─► chat ─► END        (soạn câu trả lời cuối)            │
│                        └─► END                (đường tắt "báo cáo hôm nay")      │
│      │                                                                          │
│      ▼  guard.py (chỉ SELECT) + permissions.py (role + khối dữ liệu)             │
│ PostgreSQL kinh doanh, schema dwh (role agent_readonly, chỉ đọc)                │
│ PostgreSQL agent_app, schema agent_state                                        │
│   users · chat_sessions · checkpoint · scope_grants · audit_log · report_state  │
│                                                                                 │
│ Tác vụ nền: bot polling · dọn dashboard hết hạn (5 phút) · dọn phiên/audit cũ    │
│             (mỗi giờ) · gửi báo cáo sáng (kiểm tra mỗi 60 giây)                  │
│ FastAPI: chỉ còn /api/v1/health*, nghe 127.0.0.1:8080                            │
└─────────────────────────────────────────────────────────────────────────────────┘
        │ ghi file <48 hex>.html vào DASHBOARD_DIR
        ▼
 web dashboard (nginx hoặc app/dashboard_web.py, 127.0.0.1:8081) ◄── Cloudflare Tunnel ──► Internet
```

### Một câu hỏi đi qua graph thế nào

| Bước | Node | Việc làm | Model mặc định |
|---|---|---|---|
| 1 | `supervisor` | Điều phối bằng luật, **không gọi LLM**. Câu chào hỏi đi thẳng tới `chat`; "báo cáo hôm nay" dựng ngay báo cáo như bản 7h sáng | — |
| 2 | `analyst` | Đổi câu hỏi thành `AnalysisBrief` (intent, chỉ số, chiều, bộ lọc, cụm thời gian). Cụm thời gian được `periods.py` đổi ra ngày | Haiku |
| 3 | `coder` | Lấy số liệu: ưu tiên tool phân tích dùng SQL viết sẵn (`business_snapshot`, `compare_periods`, `analyze_trend`, `explain_change`), không đủ thì tự viết `SELECT` | Sonnet |
| 4 | `designer` | Chỉ chạy khi cần biểu đồ/dashboard: gọi `create_dashboard`, trả link | Haiku |
| 5 | `chat` | Soạn câu trả lời cuối bằng tiếng Việt từ dữ liệu đã thu thập (prompt `supervisor.md`) | Sonnet |

Chống lặp: tổng số lần chuyển giữa các agent tối đa `max_handoffs` (8); mỗi worker tối đa `max_steps` vòng tool.
Lượt lỗi, quá giờ hoặc quá bước thì phiên bị xoá để không để lại hội thoại dở dang.

### Reviewer (tuỳ chọn, mặc định tắt)

`app/agent/nodes/reviewer.py` soát câu trả lời cuối trước khi gửi: `... → chat → reviewer → END`.
Bật bằng `config/agents.yaml` → `agents.reviewer.enabled: true` rồi restart; tắt thì graph không có node này.

| Lớp | Làm gì | Chi phí |
|---|---|---|
| 1. Kiểm tra số bằng code | Mọi con số trong câu trả lời phải đối chiếu được với dữ liệu đã truy vấn (hiểu số rút gọn "41,5 triệu", %, chênh lệch, tỉ lệ, AOV). Bỏ qua ngày, giờ, link, số ≤ 10 | Không tốn |
| 2. LLM soát lại (`prompts/reviewer.md`) | Chỉ chạy khi lớp 1 thấy số lạ, khi intent nằm trong `llm_intents` (mặc định `explain_change`), hoặc `mode: always`. Bản sửa chỉ được dùng nếu qua được lớp 1 | 1 lượt gọi Sonnet |

Vẫn còn số chưa đối chiếu được thì giữ câu trả lời gốc và thêm dòng "⚠️ Lưu ý" (tắt bằng `add_note: false`).
Reviewer lỗi hoặc quá giờ thì không chặn câu trả lời.

### Tool của từng agent

| Agent | Tool | Ghi chú |
|---|---|---|
| coder | `business_snapshot`, `compare_periods`, `analyze_trend`, `explain_change` | Dùng SQL trong `app/agent/queries/` nên số liệu khớp báo cáo sáng |
| coder | `get_period_events`, `get_business_targets`, `get_date_range`, `get_business_knowledge` | Tri thức: sự kiện trong kỳ, mục tiêu, đổi cụm thời gian, tài liệu nghiệp vụ |
| coder | `list_tables`, `describe_table`, `get_sample_rows`, `run_select_query` | Tự viết `SELECT` khi tool trên không trả lời được |
| designer | `create_dashboard`, `list_dashboard_presets`, `list_tables`, `describe_table` | Dựng dashboard |

Tool đọc database đều kiểm tra quyền của người hỏi. Mỗi lượt, system prompt ghi rõ vai trò và các khối dữ liệu
người hỏi được xem ("Khối dữ liệu được xem: coffee (Nyna Coffee (iPOS))."). Cụm "cùng kỳ năm ngoái/tháng trước"
không được tự đoán: tool báo lỗi để agent truyền khoảng ngày cụ thể.

### Nguyên tắc thiết kế

1. **Khoảng thời gian do code tính**, LLM chỉ chép lại cụm từ người dùng nói.
2. **Dữ liệu chỉ đọc**: SQL do LLM viết phải qua `app/db/guard.py`; kết nối PostgreSQL ép `read_only`.
3. **Quyền do server quyết định**: role tra từ `agent_state.users`, không tin gì trong nội dung tin nhắn.
4. **Số liệu cố định dùng SQL viết sẵn**: báo cáo sáng, đường tắt "báo cáo hôm nay", trang dashboard mẫu.
5. **Một worker duy nhất**: khoá phiên, rate limit và lịch báo cáo nằm trong RAM. Không chạy nhiều bản.

---

Không có trong repo (nằm trong `.gitignore`, phải tự tạo): `.env`, `config/users_seed.yaml`, `config/reports.local.yaml`, `sql/` (script tạo role),
`static/chart.umd.min.js`, `data/`.

---

## Lệnh Telegram

| Lệnh | Ai dùng | Tác dụng |
|---|---|---|
| `/start`, `/help` | Mọi người dùng | Giới thiệu, hướng dẫn (hiện thêm lệnh theo quyền) |
| `/myid` | **Mọi người, kể cả người lạ** | Xem Telegram ID để gửi cho admin |
| `/new` | Mọi người dùng | Bắt đầu cuộc trò chuyện mới |
| `/sessions` | Mọi người dùng | Xem, chuyển hoặc xoá các cuộc trò chuyện |
| `/tuan`, `/thang` | Mọi người dùng | Tình hình kinh doanh tuần này / tháng này (trong khối được xem) |
| `/dashboard` | Mọi người dùng có khối dữ liệu trong database | Dashboard doanh thu tuần này |
| `/quyen` | Mọi người dùng | Xem mình được xem những khối dữ liệu nào |

**Báo cáo sáng** (role `boss` và `manager`):

| Lệnh | Tác dụng |
|---|---|
| `/baocao` | Xem cấu hình báo cáo sáng của mình |
| `/baocao_tat`, `/baocao_bat` | Tắt / bật báo cáo sáng |
| `/baocao_gio 07:30` | Đổi giờ nhận (chỉ khi `manager_can_change_time: true`) |
| `/reply [câu hỏi]` | Chuyển sang cuộc trò chuyện của báo cáo gần nhất và hỏi tiếp |

**Cấp khối dữ liệu cho nhân viên** (boss, manager):

| Lệnh | Tác dụng |
|---|---|
| `/capquyen <id> <khối> [số giờ]` | Cấp quyền xem một khối (`coffee`...), bỏ trống số giờ là không giới hạn |
| `/thuhoi <id> <khối\|all>` | Thu hồi quyền |
| `/quyen <id>` | Xem nhân viên đang được xem những khối nào |

**Quản trị người dùng** (admin, owner):

| Lệnh | Tác dụng |
|---|---|
| `/users` | Danh sách người dùng |
| `/adduser <id> <role> [tên]` | Thêm người hoặc đổi role |
| `/addadmin <id> <role> [tên]` | Thêm hoặc nâng lên admin |
| `/removeadmin <id>` | Gỡ quyền admin |
| `/removeuser <id>` | Xoá người dùng và toàn bộ cuộc trò chuyện của họ |

Ngoài lệnh, mọi tin nhắn thường đi qua agent. Các câu chào hỏi và câu "báo cáo hôm nay / hôm qua / sáng" được
xử lý bằng đường tắt, không tốn lượt gọi LLM.

Phiên hội thoại: mỗi người tối đa **5** cuộc; cuộc nào **7 ngày** không dùng bị xoá; trần **14 ngày** kể từ lúc tạo.
Thread báo cáo sáng không tính vào hạn mức và giữ 3 ngày. Tin gửi lúc bot đang tắt mà cũ hơn 10 phút bị bỏ qua.

---

## Phân quyền

| Tầng | Cách hoạt động | Nơi khai báo |
|---|---|---|
| Role | `boss`, `manager`: xem mọi bảng trong `dwh`. `staff`: mặc định không xem gì | `ROLE_TABLES` trong `app/agent/permissions.py`, `role_scopes` trong `config/scopes.yaml` |
| Khối dữ liệu | Dữ liệu chia theo khối. Hiện chỉ có `coffee` (iPOS, `dwh.*`); `house`, `misa`, `base` (qua API) để sẵn mẫu | `config/scopes.yaml` |
| Cấp riêng cho staff | Boss/manager cấp khối bằng `/capquyen`, có thể đặt hạn; hiệu lực ngay | bảng `agent_state.scope_grants` |
| Cấp quản trị | `owner` đúng 1 người; `admin` tối đa 5 người; còn lại là thường | cột `admin_level` trong `agent_state.users` |

- Người ngoài bảng `users` bị bỏ qua im lặng (chỉ ghi log), riêng `/myid` vẫn trả lời. Bot không nhận tin trong group.
- PostgreSQL chỉ có **một** role chỉ-đọc dùng chung (`agent_readonly`); phân quyền theo role do code đảm nhiệm.
  Số điện thoại, email, địa chỉ, ngày sinh của khách phải được **che ở mức database** (role không có quyền đọc các cột này).
- Dashboard gửi bằng link, **ai có link đều xem được** cho tới khi hết hạn.

---

## Dashboard

### Các trang có sẵn

Khai báo trong `app/agent/tools/dashboard_presets.py`. Mỗi trang = bộ lọc + KPI + khối biểu đồ + một mẫu SQL có tham số ngày.

| Trang (preset) | Nội dung |
|---|---|
| `tong_quan` — Tổng quan doanh thu NYNA | 4 KPI (Doanh thu Net, Số đơn, AOV, Số món/đơn) so với kỳ trước; doanh thu theo ngày, cửa hàng, nền tảng, giờ |
| `cua_hang` — Cửa hàng | Số đơn, AOV, Số món/đơn theo cửa hàng; bảng chỉ số có ± kỳ trước và dòng Tổng |
| `nen_tang` — Nền tảng | Bảng chỉ số theo nền tảng (tỉ trọng); doanh thu theo tuần của từng nền tảng (4 tuần); bảng chéo cửa hàng × nền tảng |
| `san_pham` — Sản phẩm | Top 10 món bán chạy và bán thấp nhất; doanh thu theo nhóm món; bảng chéo nhóm món × nền tảng |
| `hanh_vi` — Hành vi khách hàng | Heatmap số đơn theo thứ × giờ và cửa hàng × giờ; đơn trung bình mỗi giờ theo khung Sáng/Ngày/Đêm |
| `hoi_vien` (bổ sung) | Khách hội viên, khách mới, tần suất mua, chi tiêu TB, theo hạng thẻ |
| `don_online` (bổ sung) | Đơn online: số đơn, GMV, tỉ lệ huỷ, thời gian xác nhận theo ngày và nền tảng |
| `default` | Tự do: agent tự viết SQL và khai báo khối |

Quy ước dataset: mỗi dòng ở mức chi tiết, số đo cộng được; cột `ky` (`nay` / `truoc` / `cu`) và `ngay_qd`
(ngày đã dịch về kỳ này) để so sánh kỳ trước; chỉ số tỉ lệ khai báo `agg: "ratio"`. Trình duyệt tự lọc và
cộng gộp, không gọi lại server. Kỳ dài tối đa 92 ngày.

Dùng trong code:

```python
from app.agent.tools.dashboard_presets import build_report
from app.agent.tools.dashboard_template import render_html, logo_data_uri

payload = build_report("2026-11-02", "2026-11-08", run_query,          # run_query(sql) -> (cols, rows)
                       pages=["tong_quan"], logo=logo_data_uri("nyna.png"))
html = render_html(payload)   # luôn dùng render_html: nó thoát </script> và đổi Decimal/date/NaN
```

Agent gọi tool `create_dashboard` theo một trong hai cách:

| Cách | Tham số | Khi nào |
|---|---|---|
| Trang có sẵn | `pages=["tong_quan", ...]`, `period_start`, `period_end` | Mọi yêu cầu khớp một trang mẫu. Không viết SQL; `pages` rỗng = 5 trang chính; bỏ trống kỳ = tuần trọn vẹn gần nhất. Trang dùng chung SQL chỉ truy vấn một lần |
| Tự khai báo | `dashboards=[{preset: "default", dataset_sql, filters, kpis, charts}]` | Yêu cầu không có trang sẵn (Analyst ghi `dashboard_pages: ["custom"]`) |

Analyst chọn trang vào trường `dashboard_pages` của `AnalysisBrief`; lệnh `/dashboard` dựng trang Tổng quan tuần này.
Mọi câu SQL (kể cả SQL mẫu) đều qua `guard` và kiểm tra quyền của người hỏi; staff chưa được cấp khối bị từ chối.

### Cách gửi dashboard

1. Agent ghi file `<48 ký tự hex>.html` (192 bit ngẫu nhiên) vào `DASHBOARD_DIR`.
2. Web dashboard phục vụ thư mục đó chỉ-đọc: nginx (`nginx-dash.conf`, Docker) hoặc `app/dashboard_web.py` (Windows).
   Chỉ phục vụ đúng tên file hex và `/chart.umd.min.js`; mọi đường dẫn khác trả 404.
3. Cloudflare Tunnel đưa web dashboard ra Internet qua HTTPS; bot gửi link `DASHBOARD_BASE_URL/<tên>.html`.
4. File tự xoá sau `DASHBOARD_TTL_HOURS` (kiểm tra mỗi 5 phút); thư mục giới hạn 500 file / 200 MB.

Giới hạn: trang có sẵn tối đa 20.000 dòng mỗi dataset; tự khai báo tối đa 6 tab, 10.000 dòng mỗi tab, 30.000 dòng cả link. Trang có CSP chặn gửi dữ liệu ra ngoài
(`connect-src 'none'`), `noindex`, `no-store`. Thiếu `chart.umd.min.js` thì biểu đồ tự chuyển thành bảng số.

---

## Báo cáo sáng

- Cấu hình thật ở `config/reports.local.yaml` (chứa Telegram ID, **không commit**, có trong `.gitignore`). `config/reports.yaml`
  trong git chỉ là mẫu (`managers: {}`): trên máy chủ chép thành `reports.local.yaml` rồi điền ID thật (phải là **số**).
  Có file `.local` thì bot đọc file đó (cần restart lần đầu tạo file); biến môi trường `REPORTS_CONFIG` (nếu đặt) được ưu tiên hơn cả.
- **Tự nạp lại trong vòng 1 phút** khi sửa. File sai thì giữ cấu hình cũ và ghi log.
- Chỉ người có tên trong mục `managers` (khoá là Telegram ID) **và** đang có role `boss` hoặc `manager` mới nhận.
  Tên mục vẫn là `managers` nhưng boss khai báo chung trong đó. Role được phép nhận nằm ở `REPORT_ROLES` trong
  `app/reports/subscriptions.py`. Người nhận phải đã `/start` bot.
- Mỗi người nhận có `send_time`, `sections`, `enabled` riêng; không khai báo thì dùng `default_send_time`, `default_sections`,
  `enabled` mặc định là `true`.

  ```yaml
  # config/reports.local.yaml
  managers:
    123456789:                 # Telegram ID (số, lấy bằng /myid)
      name: "Boss A"
      send_time: "07:00"
      sections: [kpi_summary, revenue_by_channel, target_vs_actual]
  ```

- Mục dùng được (mỗi mục là một file trong `app/agent/queries/`):

  | Mục | Nội dung |
  |---|---|
  | `kpi_summary` | Doanh thu, số đơn, AOV, số khách |
  | `revenue_by_day` | Doanh thu theo ngày |
  | `revenue_by_channel` | Doanh thu theo kênh |
  | `top_products` | Món bán chạy |
  | `bottom_products` | Món bán chậm (gồm món không bán được) |
  | `customers_new_vs_returning` | Khách mới / quay lại |
  | `target_vs_actual` | Thực tế so với mục tiêu: hôm qua + luỹ kế tháng. Mục tiêu khai báo trong `config/targets.yaml` (không cần bảng SQL) |

- Trạng thái đã gửi lưu ở `agent_state.report_state`: restart vẫn gửi bù, không gửi trùng trong ngày; bật lại muộn
  quá `max_delay_minutes` thì bỏ qua hôm đó. Mục vượt quyền bị bỏ khỏi báo cáo; một mục lỗi vẫn gửi phần còn lại.
- Nhận xét 2–3 câu do LLM viết (`commentary_enabled: true`, model `COMMENTARY_MODEL`, mặc định Haiku); nhận xét chứa
  số không có trong báo cáo sẽ bị bỏ.
- Sửa file trong `app/agent/queries/` thì phải **restart** (SQL được nạp một lần).

### Mục tiêu kinh doanh (`config/targets.yaml`)

Mục tiêu **không** lưu trong database: khai báo trong `config/targets.yaml`, sửa là tự nạp lại, không cần restart.
Cùng một file được dùng cho mục `target_vs_actual` của báo cáo sáng và cho câu hỏi về mục tiêu khi chat
(`app/agent/business_config.py`), nên số trong chat và báo cáo luôn khớp.

```yaml
defaults:
  month:                    # mục tiêu MỖI THÁNG (VND, đã gồm VAT); null = chưa đặt
    revenue: 300000000
    orders: 1500
    aov: null
    new_customers: null
    customers: null
overrides:                  # tháng có mục tiêu riêng, khoá "YYYY-MM" (để trong ngoặc kép)
  "2026-12":
    revenue: 500000000
```

- Mục tiêu tháng được chia đều theo ngày (`revenue`, `orders`, `new_customers`); `aov` giữ nguyên giá trị;
  `customers` chỉ có mục tiêu khi kỳ là đúng một tháng đầy đủ.
- Mục `target_vs_actual` hiện mỗi chỉ số `revenue`, `orders`, `aov` theo 2 mốc: **hôm qua** và **luỹ kế từ đầu tháng**,
  kèm phần còn thiếu so với mục tiêu cả tháng. Chỉ số chưa đặt mục tiêu thì không hiện; chưa đặt chỉ số nào thì
  mục này báo "Chưa đặt mục tiêu".
- Cờ `[CẢNH BÁO]` / `[NGHIÊM TRỌNG]` tính trên luỹ kế tháng, theo `achievement_vs_target` trong `config/thresholds.yaml`.
- Số thực tế lấy từ `app/agent/queries/target_vs_actual.sql`; tên cột phải trùng tên chỉ số trong `targets.yaml`.

---

## Tri thức nghiệp vụ và prompt

Prompt nằm ở `app/agent/prompts/`, tri thức ở `app/agent/knowledge/`. Tài liệu được ghép vào system prompt
(phần cố định được cache để rẻ hơn):

| Agent | Prompt | Tài liệu được nạp |
|---|---|---|
| analyst | `analyst.md` | `business_context.md`, `glossary.md` |
| coder | `coder.md` | `glossary.md`, `sql_examples.md` |
| designer | `designer.md` | `glossary.md`, phần "Quy tắc bắt buộc" của `sql_examples.md` |
| chat (trả lời cuối) | `supervisor.md` | `business_context.md`, `glossary.md`, sự kiện trong kỳ (`calendar.yaml`) |
| nhận xét báo cáo | `report_commentary.md` | nội dung báo cáo |

- `glossary.md`: định nghĩa chỉ số (doanh thu = `final_amount` đã gồm VAT, khoá `-1`, kỳ trước...). Bot, `queries/`
  và dashboard phải cùng định nghĩa; đổi công thức thì đổi cả ba nơi.
- `business_context.md`: phần cuối "Chưa rõ" **không** được nạp cho agent (dùng để chủ doanh nghiệp xác nhận dần).
- `sql_examples.md`: mọi ví dụ phải qua `guard` và đúng quyền manager (`tests/test_knowledge.py` kiểm tra).
- `calendar.yaml`: ngày lễ, khuyến mãi, sự kiện để agent đọc biến động.

---

## Cài đặt

### Yêu cầu chung

- PostgreSQL có **2 database**:
  - database kinh doanh (mặc định `NynaGroup`), schema `dwh`, role chỉ-đọc `agent_readonly` (`SELECT` trên `dwh`, che cột cá nhân khách);
  - database `agent_app`, schema `agent_state`, role có quyền tạo bảng (ví dụ `agent_checkpoint`). App tự tạo các bảng
    `users`, `chat_sessions`, checkpoint, `scope_grants`, `audit_log`, `report_state` khi khởi động.
- Telegram Bot Token ([@BotFather](https://t.me/BotFather)), Anthropic API key.
- Tên miền trên Cloudflare + Cloudflare Tunnel (để gửi link dashboard).
- `static/chart.umd.min.js` (Chart.js v4, bản đã ghim phiên bản).
- `config/users_seed.yaml` cho người dùng đầu tiên (chỉ nạp khi bảng `users` còn trống):

  ```yaml
  users:
    - telegram_id: 123456789     # lấy bằng /myid
      name: "Anh A"
      role: boss                 # boss | manager | staff
      level: owner               # owner (đúng 1 người) | admin | staff
  ```

### Chạy trên Windows Server (cách đang dùng)

Mỗi thành phần chạy thành một Windows Service bằng **WinSW**:

| Service | Lệnh | Địa chỉ |
|---|---|---|
| `NynaAgent` | `python run_windows.py agent` | `127.0.0.1:8080` (không mở ra ngoài) |
| `NynaDashboards` | `python run_windows.py dashboards` | `127.0.0.1:8081` |
| `Cloudflared` | bản cài Windows chính thức | hostname dashboard → `http://localhost:8081` |

> Phải chạy bằng `run_windows.py`, không gọi thẳng `uvicorn app.main:app`: trên Windows uvicorn dùng
> `ProactorEventLoop`, psycopg async không chạy được; `run_windows.py` ép `SelectorEventLoop`.
> Chạy `python run_windows.py` không kèm tham số chỉ in hướng dẫn rồi thoát, không phải lỗi.

**1. Cài phần mềm** (PowerShell Admin): Python 3.14 64-bit (*Install for all users*, *Add to PATH*), Git,
WinSW 2.12 (`WinSW-x64.exe`, ví dụ `C:\tools\WinSW-x64.exe`), cloudflared (`.msi`).

**2. Lấy code và môi trường**

```powershell
cd C:\
git clone https://github.com/NTThanh0405/AI_Agent_LangGraph_nyna.git nyna
cd C:\nyna
py -3.14 -m venv .venv
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -r requirements.txt
mkdir static, data\dashboards, logs -Force
# chép chart.umd.min.js vào static\ ; tạo .env (UTF-8) và config\users_seed.yaml
notepad .env
```

Trên Windows: `DB_HOST=localhost`, `CHECKPOINT_DATABASE_URL` trỏ `localhost`, `DASHBOARD_DIR=data/dashboards`,
`DELIVERY_MODE=link`, không cần `CLOUDFLARE_TUNNEL_TOKEN` trong `.env`.

**3. Chạy thử bằng tay** (2 cửa sổ PowerShell, cả hai đã kích hoạt `.venv`)

```powershell
.venv\Scripts\python.exe run_windows.py agent         # cửa sổ 1, Ctrl+C để dừng
.venv\Scripts\python.exe run_windows.py dashboards    # cửa sổ 2
Invoke-RestMethod http://127.0.0.1:8080/api/v1/health
```

Nhắn `/start` cho bot từ tài khoản có trong `users_seed.yaml`. Chạy ổn rồi mới cài service.

**4. Cài Windows Service** (tự bật khi khởi động máy, tự khởi động lại khi chết)

```powershell
powershell -ExecutionPolicy Bypass -File deploy\windows\install-services.ps1 -WinSW C:\tools\WinSW-x64.exe
```

Cấu hình service ở `deploy\windows\winsw\NynaAgent.xml`, `NynaDashboards.xml`: chạy bằng `LocalService`,
khởi động lại sau 10/30/60 giây, log ở `logs\NynaAgent.out.log` / `.err.log` (xoay vòng > 10 MB).
PostgreSQL cùng máy thì bỏ comment dòng `<depend>` trong `NynaAgent.xml` trước khi cài.

**5. Cloudflare Tunnel**

```powershell
cloudflared.exe service install <TUNNEL_TOKEN>
```

Cloudflare Zero Trust → Networks → Tunnels → Public Hostname: hostname dashboard → `HTTP` → `localhost:8081`.
Hostname phải khớp `DASHBOARD_BASE_URL`.

**6. Healthcheck** (khởi động lại khi tiến trình còn sống nhưng treo)

```powershell
$a = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -File `"C:\nyna\deploy\windows\healthcheck.ps1`""
$t = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 5)
Register-ScheduledTask -TaskName "NynaAgentHealth" -Action $a -Trigger $t -User "SYSTEM" -RunLevel Highest
```

Không mở port 8080/8081 trên Windows Firewall (Cloudflare Tunnel kết nối ra ngoài). Tắt ngủ/hibernate và đặt
Windows Update khởi động lại ngoài giờ báo cáo sáng.

### Chạy bằng Docker (Linux)

`docker-compose.yml` gồm 3 container: `agent` (không mở port), `dashboards-web` (nginx), `cloudflared`.
Thư mục `config/` được mount chỉ-đọc, `static/` chứa `chart.umd.min.js`.

```bash
docker compose up -d --build
docker compose logs -f agent
```

> **Repo hiện chưa có `Dockerfile`** mà `docker-compose.yml` gọi `build: .`, nên cách này chưa chạy được cho tới khi
> thêm `Dockerfile` (chạy `uvicorn app.main:app --host 0.0.0.0 --port 8080`, user không phải root, thư mục làm việc `/code`).
> Với Docker: `DB_HOST=host.docker.internal`, `DASHBOARD_DIR=/data/dashboards`, cần `CLOUDFLARE_TUNNEL_TOKEN`,
> hostname Cloudflare trỏ `dashboards-web:80`.

---

## Biến môi trường

File `.env` ở thư mục gốc (UTF-8, không commit).

```env
# --- Bắt buộc ---
ANTHROPIC_API_KEY=
TELEGRAM_BOT_TOKEN=
DB_PASSWORD=                         # mật khẩu role agent_readonly
CHECKPOINT_DATABASE_URL=postgresql://agent_checkpoint:<mật khẩu>@localhost:5432/agent_app
                                     # thiếu thì app không khởi động (người dùng và phiên lưu ở đây)

# --- PostgreSQL kinh doanh ---
DB_HOST=localhost                    # Docker: host.docker.internal
DB_PORT=5432
DB_NAME=NynaGroup
DB_USER=agent_readonly
DB_SCHEMA=dwh
DB_POOL_MIN=1
DB_POOL_MAX=5
DB_STATEMENT_TIMEOUT_MS=15000
DB_MAX_ROWS=200

# --- LLM ---
# Model từng agent đặt trong config/agents.yaml; MODEL_NAME chỉ là mặc định chung.
MODEL_NAME=claude-sonnet-5-5
MAX_TOKENS=4096
LLM_TIMEOUT=60
GRAPH_MAX_STEPS=12
INVOKE_TIMEOUT_SECONDS=120           # tổng thời gian tối đa mỗi lượt hỏi
COMMENTARY_MODEL=claude-haiku-5-5    # nhận xét báo cáo sáng (đọc từ biến môi trường hệ thống)

# --- Dashboard ---
DELIVERY_MODE=link                   # BẮT BUỘC đặt link (mặc định "file" chưa được hỗ trợ)
DASHBOARD_BASE_URL=https://dashboard.example.com
DASHBOARD_DIR=data/dashboards        # Docker: /data/dashboards
DASHBOARD_STATIC_DIR=static          # chỉ dùng cho app/dashboard_web.py
DASHBOARD_TTL_HOURS=24
DASHBOARD_MAX_CHARTS=6
DASHBOARD_LOGO=                      # tuỳ chọn: đường dẫn file logo (png/jpg/webp/svg) hiện ở góc trang

# --- Phiên chat, giới hạn, vận hành ---
MAX_SESSIONS_PER_USER=5
SESSION_IDLE_DAYS=7
SESSION_MAX_DAYS=14
AUDIT_RETENTION_DAYS=30
RATE_LIMIT_PER_MIN=5
RATE_LIMIT_PER_DAY=100
TIMEZONE=Asia/Ho_Chi_Minh
APP_ENV=production                   # development: bật /docs
LOG_LEVEL=INFO

# --- Chạy không Docker (run_windows.py) ---
APP_HOST=127.0.0.1
APP_PORT=8080
DASHBOARD_WEB_HOST=127.0.0.1
DASHBOARD_WEB_PORT=8081

# --- Đường dẫn cấu hình (tuỳ chọn, mặc định trong config/) ---
# AGENTS_CONFIG= SCOPES_CONFIG= REPORTS_CONFIG= TARGETS_CONFIG= THRESHOLDS_CONFIG=

# --- Chỉ dùng với Docker ---
CLOUDFLARE_TUNNEL_TOKEN=
CLOUDFLARED_IMAGE_TAG=latest         # nên ghim phiên bản đang chạy ổn
```

### File cấu hình

| File | Nội dung | Khi sửa |
|---|---|---|
| `config/agents.yaml` | Model, `max_steps`, timeout, `max_tokens` từng agent; `max_handoffs` | Restart |
| `config/scopes.yaml` | Khối dữ liệu, quyền mặc định theo role, connector API (mẫu) | Restart |
| `config/reports.yaml` | Mẫu báo cáo sáng trong git (`managers: {}`) | — |
| `config/reports.local.yaml` | Báo cáo sáng thật: ai nhận (Telegram ID), mục nào, mấy giờ (không commit) | Tự nạp trong 1 phút (tạo lần đầu: restart) |
| `config/targets.yaml` | Mục tiêu kinh doanh theo tháng, dùng cho báo cáo sáng và chat (đang để `null`) | Tự nạp |
| `config/thresholds.yaml` | Ngưỡng gắn cờ biến động (đang để `null`) | Tự nạp |
| `config/users_seed.yaml` | Người dùng đầu tiên (không commit) | Chỉ nạp khi bảng `users` trống |
| `app/agent/queries/*.sql` | SQL báo cáo sáng | Restart |
| `app/agent/knowledge/*`, `app/agent/prompts/*` | Tri thức và prompt | Restart |

---

## Vận hành

| Việc | Windows | Docker |
|---|---|---|
| Trạng thái | `Get-Service Nyna*, Cloudflared` | `docker compose ps` |
| Xem log | `Get-Content C:\nyna\logs\NynaAgent.err.log -Wait -Tail 50` | `docker compose logs -f agent` |
| Khởi động lại | `Restart-Service NynaAgent` | `docker compose restart agent` |
| Cập nhật code | `git pull` → `pip install -r requirements.txt` → `Restart-Service NynaAgent, NynaDashboards` | `git pull && docker compose up -d --build` |
| Gỡ service | `deploy\windows\uninstall-services.ps1` | `docker compose down` |

Health check: `GET /api/v1/health` (app sống), `/api/v1/health/db` (kết nối DB), `/api/v1/health/reports`
(vòng báo cáo còn chạy đều). Bot dừng vì lỗi không phục hồi (ví dụ token sai) thì cả tiến trình thoát để
WinSW/Docker khởi động lại.

---

## Kiểm thử và đánh giá

```bash
pip install -r requirements-dev.txt
pytest   # test nạp app.config cần ANTHROPIC_API_KEY, TELEGRAM_BOT_TOKEN, DB_PASSWORD (giá trị giả cũng được)
```

| Test | Kiểm tra |
|---|---|
| `test_guard.py` | SQL tấn công, schema mặc định, chỉ cho SELECT |
| `test_permissions.py`, `test_scopes.py` | Quyền theo role và khối dữ liệu |
| `test_boss_manager_commands.py` | Lệnh quản trị, `/capquyen`, `/thuhoi`, `/quyen` |
| `test_periods.py` | "Tuần này/tháng này" ở các ngày biên |
| `test_graph_flow.py` | Đường đi trong graph (LLM giả) |
| `test_knowledge.py` | Tài liệu nạp đúng; mọi ví dụ SQL qua guard và đúng quyền |
| `test_reports.py`, `test_report_commands.py`, `test_report_queries.py` | Báo cáo sáng: cấu hình, đúng giờ, không trùng, SQL hợp lệ, boss/manager nhận báo cáo, mục tiêu từ `targets.yaml` |
| `test_dashboard_web.py` | Web dashboard chỉ phục vụ đúng file |
| `test_business_config.py`, `test_analysis_tools.py` | Mục tiêu, ngưỡng, tool phân tích |
| `check_report_permissions.py` | Script (không phải test): mục báo cáo nào bị chặn quyền với một role (mặc định `manager`, ví dụ `... boss`) |

**Đánh giá chất lượng trả lời** (gọi Claude và DB thật, **tốn chi phí**, không chạy trong `pytest`):

```bash
python tests/eval/run_eval.py --dry-run        # chỉ kiểm tra questions.yaml, không tốn tiền
python tests/eval/run_eval.py --tag smoke      # nhóm câu nhanh
python tests/eval/run_eval.py --compare --judge
```

Chấm đúng/sai so với số liệu từ `queries/`, đúng đường đi trong graph, chi phí token và độ trễ.

---

## Xử lý sự cố

| Triệu chứng | Nguyên nhân / cách xử lý |
|---|---|
| `Cách dùng: python run_windows.py [agent\|dashboards]` | Không phải lỗi: chạy kèm tham số `agent` hoặc `dashboards` |
| App không khởi động, thiếu `CHECKPOINT_DATABASE_URL` | Biến bắt buộc: người dùng và phiên lưu trong `agent_app` |
| `DELIVERY_MODE=link cần đặt DASHBOARD_BASE_URL` | Đặt `DASHBOARD_BASE_URL` khớp hostname Cloudflare |
| Bot không phản hồi | Sai `TELEGRAM_BOT_TOKEN`, hoặc Telegram ID chưa có trong `users` (nhắn `/myid`, nhờ admin `/adduser`) |
| Telegram báo 409 khi polling | Có tiến trình khác dùng cùng token (chạy 2 bản agent, hoặc còn bản Docker cũ) |
| psycopg lỗi về event loop trên Windows | Đang chạy thẳng `uvicorn`; phải chạy `python run_windows.py agent` |
| "Không có quyền truy cập..." | Bảng ngoài `dwh`, hoặc staff chưa được `/capquyen <id> coffee` |
| Truy vấn bị huỷ vì quá lâu | Thu hẹp kỳ; hoặc tăng `DB_STATEMENT_TIMEOUT_MS` có cân nhắc. Nên có index trên `fact_sales(tran_date)` |
| Log báo `managers.xx: khoá phải là Telegram ID dạng số` | Khoá manager phải là số. Đừng che ID trong `reports.yaml`; ghi ID thật vào `config/reports.local.yaml` (không commit) |
| Báo cáo sáng không gửi / thiếu mục | Người nhận chưa có trong `reports.local.yaml`, không có role `boss`/`manager`, hoặc chưa `/start` bot; quá `max_delay_minutes`; mục `target_vs_actual` báo "chưa đặt mục tiêu" nếu `targets.yaml` còn trống. Xem `/api/v1/health/reports` |
| Dashboard mở ra không có biểu đồ (chỉ bảng số) | Thiếu `static/chart.umd.min.js` |
| Link dashboard 404 | Hết hạn (`DASHBOARD_TTL_HOURS`), hoặc hostname Cloudflare chưa trỏ đúng web dashboard |
| VS Code báo "unusual line terminators" ở `dashboard_template.py` | Bấm **Ignore**, không bấm Remove (bản hiện tại đã dùng `\u2028` dạng ASCII) |
| "Câu hỏi quá phức tạp" / "xử lý quá lâu" | Vượt `max_handoffs`/`max_steps` hoặc `INVOKE_TIMEOUT_SECONDS`; chia nhỏ câu hỏi |

---

## Trạng thái và việc còn dở

Đã chạy:

- [x] Bot Telegram: polling, allowlist trong DB, phiên chat, lệnh quản trị, rate limit, audit
- [x] Graph v2: supervisor (luật) + analyst + coder + designer + chat, cấu hình model từng agent
- [x] Phân quyền role + khối dữ liệu (`coffee`), cấp quyền cho staff bằng lệnh
- [x] Báo cáo sáng cho boss và manager + `/reply`, đường tắt "báo cáo hôm nay"; so với mục tiêu từ `targets.yaml`
- [x] Dashboard: template + 7 preset, gửi link qua Cloudflare Tunnel; chạy 24/7 bằng Windows Service
- [x] Tri thức nghiệp vụ và prompt nạp vào từng agent; bộ eval chất lượng

Còn dở hoặc chưa nối:

- [ ] `app/agent/tools/api_tools.py` (gọi API khối house/misa/base) chưa có; `tests/test_api_tools.py` và một test
      trong `test_graph_flow.py` đang lỗi vì thiếu file này.
- [ ] `targets.yaml`, `thresholds.yaml` còn để trống (chủ doanh nghiệp điền mục tiêu tháng).
- [ ] `DELIVERY_MODE=file` (gửi file HTML qua Telegram) chưa được nối; dùng `link`.
- [ ] `Dockerfile` chưa có trong repo.
- [ ] Các giả định dữ liệu cần chủ doanh nghiệp xác nhận: mục "Chưa rõ" cuối `app/agent/knowledge/business_context.md`.

---

## Công nghệ

Python 3.14 · FastAPI · LangGraph · LangChain · Claude (Anthropic) · aiogram 3 · psycopg 3 · sqlglot · PostgreSQL ·
Chart.js · Cloudflare Tunnel · Windows Service (WinSW) hoặc Docker (nginx)