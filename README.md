# langgraph-service

Trợ lý phân tích dữ liệu kinh doanh chạy trên **Telegram**, xây bằng **LangGraph + Claude + PostgreSQL**.

Sếp (hoặc quản lý) nhắn tin cho bot, ví dụ *"cho tôi dashboard doanh thu tuần này"*, *"tình hình kinh doanh tháng này thế nào?"*, *"có đề xuất gì không?"*. Agent truy vấn database nội bộ (chỉ đọc), phân tích như một data analyst, rồi trả lời kèm số liệu, biểu đồ, dashboard HTML và đề xuất.

> Agent **chỉ làm việc khi có yêu cầu**. Không có báo cáo hay cảnh báo tự động theo lịch.

---

## Tính năng

- **Chat qua Telegram** bằng long polling, không cần mở port hay webhook công khai.
- **Hiểu thời gian đúng**: "tuần này", "tháng này", "7 ngày qua" được tính bằng code (`periods.py`), không để LLM tự đoán ngày.
- **Số liệu ổn định**: các chỉ số quen thuộc (doanh thu, số đơn, giá trị đơn trung bình, khách mới/quay lại, tỉ lệ huỷ, top sản phẩm, theo kênh) dùng SQL viết sẵn, so với kỳ trước và cùng kỳ. LLM chỉ diễn giải.
- **Câu hỏi tự do**: agent tự khám phá schema và viết SQL `SELECT` cho câu hỏi ngoài các mẫu có sẵn.
- **Dashboard HTML** (Chart.js) với bố cục cố định, gửi dạng file hoặc link.
- **Đề xuất bám số liệu**: kết hợp snapshot, mục tiêu, lịch khuyến mãi và ngưỡng cảnh báo.
- **Phân quyền theo vai trò**: Telegram ID tra allowlist ra role, mỗi role có tài khoản PostgreSQL riêng.
- **An toàn**: chỉ `SELECT`, guard bằng sqlglot, `statement_timeout`, giới hạn số dòng, rate limit, audit log.

---

## Kiến trúc

```
Telegram (sếp / quản lý)
      │  long polling
      ▼
┌──────────────────── container: agent (1 worker) ────────────────────┐
│ telegram/bot → auth (allowlist → role) → ratelimit → lock theo chat │
│      │  gửi ngay "Đang phân tích, khoảng 1 phút..."                 │
│      ▼                                                              │
│ LangGraph: chat ◄──► tools                                          │
│   ├─ resolve_period       tính khoảng thời gian bằng code           │
│   ├─ business_snapshot    KPI cố định + % thay đổi + cờ vượt ngưỡng │
│   ├─ create_dashboard     preset bố cục cố định                     │
│   ├─ list_tables / describe_table / run_select_query                │
│   ├─ get_sample_rows / profile_column                               │
│   ├─ analysis / chart                                               │
│   └─ get_business_context mục tiêu, khuyến mãi, ngưỡng              │
│      │                                                              │
│      ▼  guard + permissions + pool theo role                        │
│ PostgreSQL (schema dwh, role chỉ-đọc riêng cho từng vai trò)        │
│      ▼                                                              │
│ formatter → text + ảnh + file/link dashboard + tóm tắt 3-4 dòng     │
└─────────────────────────────────────────────────────────────────────┘
```

### Nguyên tắc thiết kế

1. **Số liệu do code tính, LLM chỉ diễn giải.** Khoảng thời gian, KPI, so sánh kỳ trước đều do code và SQL cố định tính.
2. **Câu hỏi quen thuộc đi đường cố định, câu hỏi lạ đi đường tự do.**
3. **Không hỏi ngược.** Agent chọn cách hiểu hợp lý nhất và nêu giả định ở cuối câu trả lời.
4. **Quyền do server quyết định**, không tin gì từ nội dung tin nhắn.

---

## Ví dụ sử dụng

| Nhắn cho bot | Agent làm |
|---|---|
| "Dashboard doanh thu tuần này" | `resolve_period` → `create_dashboard(revenue_weekly)` → gửi dashboard kèm tóm tắt 3-4 dòng |
| "Tình hình kinh doanh tháng này?" | `resolve_period` → `business_snapshot` → drill-down nếu có cờ ⚠️ → trả lời theo khuôn analyst |
| "Có đề xuất gì?" | Dựa trên snapshot + `business_context.md` + `calendar.yaml`, mỗi đề xuất có bằng chứng số liệu và mức ưu tiên |

**Khuôn trả lời:** Kết luận → Số liệu chính → Nguyên nhân khả dĩ → Đề xuất → Giả định và giới hạn.

**Quy ước thời gian:**
- *Tuần này*: từ thứ Hai đến hôm nay, so với cùng số ngày của tuần trước.
- *Tháng này*: từ ngày 1 đến hôm nay, so với cùng số ngày của tháng trước.

### Lệnh Telegram

| Lệnh | Tác dụng |
|---|---|
| `/start`, `/help` | Giới thiệu và hướng dẫn |
| `/new` | Bắt đầu hội thoại mới (xoá ngữ cảnh) |
| `/tuan` | Tình hình kinh doanh tuần này |
| `/thang` | Tình hình kinh doanh tháng này |
| `/dashboard` | Dashboard doanh thu mặc định |

Phiên hội thoại tự hết hạn sau 60 phút im lặng.

---

## Cấu trúc thư mục

```
langgraph-service/
├── app/
│   ├── main.py                  # lifespan: pool theo role, checkpointer, graph, bot polling, dọn nền
│   ├── config.py                # cấu hình từ .env
│   ├── audit.py                 # ghi log: user, câu hỏi, tool, SQL, số dòng, thời gian, token
│   ├── api/                     # FastAPI: chỉ còn health check
│   │   ├── deps.py
│   │   └── v1/ (router.py, health.py)
│   ├── telegram/
│   │   ├── bot.py               # long polling, chống trùng update_id
│   │   ├── handlers.py          # lệnh và tin nhắn thường
│   │   ├── progress.py          # thông báo "đang phân tích..."
│   │   ├── auth.py              # Telegram ID → role
│   │   ├── ratelimit.py
│   │   └── formatter.py         # cắt tin 4096 ký tự, escape, gửi ảnh/file
│   ├── agent/
│   │   ├── state.py
│   │   ├── graph.py             # vòng lặp chat ↔ tools
│   │   ├── sessions.py          # phiên 60 phút, dọn nền, reset
│   │   ├── permissions.py       # vai trò → bảng/cột
│   │   ├── prompts.py           # prompt analyst, quy ước thời gian, khuôn trả lời
│   │   ├── periods.py           # "tuần này" → (từ, đến, kỳ trước, cùng kỳ năm trước)
│   │   ├── knowledge/           # glossary.md, sql_examples.md, business_context.md, calendar.yaml
│   │   ├── queries/             # SQL cố định, tham số hoá (kpi, theo ngày, theo kênh, top sản phẩm...)
│   │   ├── nodes/chat.py        # gọi Claude
│   │   └── tools/               # sql, explore, period, snapshot, context, analysis, chart, dashboard
│   ├── db/
│   │   ├── pool.py              # mỗi role một connection pool
│   │   └── guard.py             # chỉ cho một câu SELECT, chặn hàm/bảng nguy hiểm
│   └── schemas/                 # request.py, response.py
├── config/
│   ├── allowlist.yaml           # telegram_id → role, tên hiển thị
│   ├── thresholds.yaml          # ngưỡng gắn cờ trong snapshot
│   └── targets.yaml             # mục tiêu doanh thu tuần/tháng
├── sql/create_roles.sql         # tạo role PostgreSQL theo vai trò
├── tests/                       # unit test + tests/eval (bộ câu hỏi hồi quy)
├── nginx-dash.conf              # chỉ dùng khi gửi dashboard bằng link
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── .env                         # giá trị thật, KHÔNG commit
```

---

## Yêu cầu

- Docker và Docker Compose
- PostgreSQL (có thể nằm trên máy host hoặc máy khác)
- Telegram Bot Token (tạo qua [@BotFather](https://t.me/BotFather))
- Anthropic API key
- (Chỉ khi gửi dashboard bằng link) Cloudflare Tunnel token và một tên miền

---

## Cài đặt

### 1. Clone và tạo file `.env`

```bash
git clone <repo-url>
cd langgraph-service
cp .env.example .env     # rồi điền giá trị thật
```

### 2. Tạo role PostgreSQL

Chỉnh tên database, mật khẩu và danh sách bảng trong `sql/create_roles.sql`, rồi chạy bằng tài khoản admin:

```bash
psql -h <host> -U postgres -d <your_db> -f sql/create_roles.sql
```

Script tạo:

| Role | Quyền |
|---|---|
| `agent_boss_ro` | Chỉ đọc, các bảng dành cho vai trò boss |
| `agent_manager_ro` | Chỉ đọc, giới hạn bảng/cột/view cho manager |
| `agent_checkpoint` | Chỉ ghi trong schema `agent_state` (lưu hội thoại, nếu dùng PostgresSaver) |

> Phân quyền được thực thi **ở cả hai lớp**: `permissions.py` (trong code) và `GRANT` (trong Postgres). Hai nơi phải khớp nhau. Cột nhạy cảm (giá vốn, lương, SĐT) nên được che bằng view hoặc cấp quyền theo cột.

### 3. Khai báo người dùng

`config/allowlist.yaml`:

```yaml
users:
  - telegram_id: 123456789
    role: boss
    name: Anh A
  - telegram_id: 987654321
    role: manager
    name: Chị B
```

Telegram ID không có trong danh sách sẽ bị bỏ qua. Bot chỉ nhận chat riêng, không nhận group.

Lấy Telegram ID bằng cách nhắn cho [@userinfobot](https://t.me/userinfobot).

### 4. Điền tri thức nghiệp vụ

Chất lượng phân tích phụ thuộc phần này nhiều nhất:

| File | Nội dung |
|---|---|
| `app/agent/knowledge/glossary.md` | Bảng, cột, grain, khoá join. Doanh thu tính trước hay sau giảm giá, có trừ đơn huỷ không, múi giờ, tiền tệ |
| `app/agent/knowledge/sql_examples.md` | Cặp "câu hỏi → SQL chuẩn" cho các chỉ số quan trọng |
| `app/agent/knowledge/business_context.md` | Mục tiêu, mùa vụ, quy tắc đề xuất |
| `app/agent/knowledge/calendar.yaml` | Khuyến mãi, ngày lễ, sự kiện (để so sánh công bằng) |
| `config/thresholds.yaml` | Ngưỡng cảnh báo, ví dụ doanh thu giảm trên 10% so với kỳ trước |
| `config/targets.yaml` | Mục tiêu doanh thu tuần/tháng (VND) |
| `app/agent/queries/*.sql` | SQL cố định cho KPI, viết theo schema thật của bạn |

### 5. Chạy

```bash
docker compose up -d --build
docker compose logs -f agent
```

Kiểm tra: nhắn `/start` cho bot từ tài khoản có trong allowlist.

---

## Biến môi trường

```env
# LLM
ANTHROPIC_API_KEY=
MODEL_NAME=claude-sonnet-5-5
MAX_TOKENS=4096

# Telegram
TELEGRAM_BOT_TOKEN=

# PostgreSQL (mỗi vai trò một role riêng)
DB_HOST=host.docker.internal
DB_PORT=5432
DB_NAME=
DB_BOSS_USER=agent_boss_ro
DB_BOSS_PASSWORD=
DB_MANAGER_USER=agent_manager_ro
DB_MANAGER_PASSWORD=
DB_STATEMENT_TIMEOUT_MS=15000
DB_MAX_ROWS=200

# Lưu hội thoại và audit (bỏ trống CHECKPOINT_DATABASE_URL thì dùng RAM, mất khi restart)
CHECKPOINT_DATABASE_URL=
AUDIT_DATABASE_URL=

# Vận hành
TIMEZONE=Asia/Ho_Chi_Minh
RATE_LIMIT_PER_MIN=5
RATE_LIMIT_PER_DAY=100
SESSION_TTL_MINUTES=60
APP_ENV=production
LOG_LEVEL=INFO

# Gửi dashboard: file (gửi qua Telegram) hoặc link (nginx + Cloudflare)
DELIVERY_MODE=file
DASHBOARD_BASE_URL=
DASHBOARD_TTL_HOURS=24
CLOUDFLARE_TUNNEL_TOKEN=
```

---

## Cách gửi dashboard

| | `file` (mặc định) | `link` |
|---|---|---|
| Cách hoạt động | Bot gửi file HTML qua Telegram (Chart.js nhúng inline, mở offline được) | Ghi file tên ngẫu nhiên, nginx phục vụ, Cloudflare Tunnel đưa ra HTTPS |
| Service cần chạy | `agent` | `agent` + `dashboards-web` + `cloudflared` |
| Bảo mật | Không có URL công khai | Ai có link đều xem được, nên đặt hạn ngắn |
| Tiện dùng | Phải tải về rồi mở | Bấm là xem |

Với chế độ `link`: tên file dùng `secrets.token_urlsafe(32)`, tự xoá sau `DASHBOARD_TTL_HOURS`, nginx tắt `autoindex`, thêm `X-Robots-Tag: noindex` và CSP (xem `nginx-dash.conf`).

---

## Bảo mật

| Lớp | Biện pháp |
|---|---|
| Danh tính | Allowlist Telegram ID ở server; role không bao giờ lấy từ nội dung tin nhắn |
| Phân quyền | `permissions.py` + role PostgreSQL riêng cho từng vai trò + view che cột nhạy cảm |
| SQL | `guard.py` chỉ cho một câu `SELECT`, chặn hàm nguy hiểm (`pg_*`, `set_config`...) và bảng hệ thống; kết nối `read_only`; `statement_timeout`; giới hạn số dòng |
| Dữ liệu là dữ liệu | Nội dung ô trong DB không được coi là chỉ thị (chống prompt injection) |
| Dashboard | Escape dữ liệu nhúng, Chart.js pin phiên bản hoặc inline, preset cũng đi qua cùng guard và quyền |
| Chi phí | Rate limit theo phút/ngày, `recursion_limit`, trim lịch sử, audit token theo user |
| Triển khai | Agent không mở port ra ngoài, container chạy bằng user không phải root |

**Lưu ý vận hành:**
- Chạy **1 worker** vì phiên hội thoại nằm trong RAM. Nếu cần nhiều worker, đặt `CHECKPOINT_DATABASE_URL` để dùng `AsyncPostgresSaver`.
- Không commit `.env`. Đổi toàn bộ mật khẩu mẫu trong `sql/create_roles.sql` trước khi chạy.

---

## Kiểm thử

```bash
pip install -r requirements.txt pytest
pytest tests/
```

| Test | Kiểm tra |
|---|---|
| `test_guard.py` | Các câu SQL tấn công: nhiều câu lệnh, `pg_sleep`, `SELECT INTO`, CTE ghi, truy vấn chéo database |
| `test_permissions.py` | Vai trò nào xem được bảng nào |
| `test_periods.py` | "Tuần này/tháng này" ở các ngày biên, đổi tháng, đổi năm |
| `test_snapshot.py` | SQL cố định chạy đúng trên dữ liệu mẫu |
| `test_auth_ratelimit.py` | Allowlist và giới hạn tần suất |
| `test_formatter.py` | Cắt tin, escape |

**Eval hồi quy:** `tests/eval/questions.yaml` gồm 20-30 câu hỏi mẫu kèm đáp án kỳ vọng. Chạy lại mỗi khi sửa prompt hoặc tool:

```bash
python tests/eval/run_eval.py
```

---

## Phát triển cục bộ

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
python -m app.main
```

Khi `APP_ENV=development`, trang `/docs` của FastAPI được bật. Ở `production` thì tắt.

---

## Xử lý sự cố

| Triệu chứng | Nguyên nhân thường gặp |
|---|---|
| Bot không phản hồi | Sai `TELEGRAM_BOT_TOKEN`, hoặc Telegram ID chưa có trong `allowlist.yaml` |
| Container agent unhealthy | Xem `docker compose logs agent`; thường do không kết nối được DB (`DB_HOST`, mật khẩu, `pg_hba.conf`) |
| Không kết nối được Postgres trên máy host | Đặt `DB_HOST=host.docker.internal` và kiểm tra `listen_addresses` cùng `pg_hba.conf` |
| "Không có quyền truy cập các bảng..." | `permissions.py` và `GRANT` trong Postgres chưa khớp |
| Truy vấn bị huỷ vì quá lâu | Thu hẹp khoảng thời gian, hoặc tăng `DB_STATEMENT_TIMEOUT_MS` có cân nhắc |
| Mất hội thoại sau khi restart | Chưa đặt `CHECKPOINT_DATABASE_URL` nên đang dùng RAM |
| Số liệu lệch so với báo cáo cũ | Kiểm tra định nghĩa doanh thu trong `glossary.md` và múi giờ của dữ liệu |
| Câu trả lời bị cắt ngang | Tăng `MAX_TOKENS` |

---

## Trạng thái triển khai

- [ ] **Giai đoạn 1:** `telegram/` (polling, allowlist, `/new`, cắt tin, progress); 
- [ ] **Giai đoạn 2:** `periods.py` + test, `glossary.md`, `prompts.py`, `explore_tools`
- [ ] **Giai đoạn 3:** `queries/*.sql`, `business_snapshot`, `business_context.md`, `thresholds/targets.yaml`
- [ ] **Giai đoạn 4:** `dashboard_presets.py`, chọn cách gửi `file` hay `link`
- [ ] **Giai đoạn 5:** tách role PostgreSQL, `audit.py`, test, eval
- [ ] **Giai đoạn 6 (tuỳ chọn):** `analysis_tools` (cohort, RFM, anomaly), `chart_tools`, trim lịch sử, prompt caching

---

## Công nghệ

Python 3.11 · FastAPI · LangGraph · LangChain · Claude (Anthropic) · aiogram · psycopg 3 · sqlglot · pandas · matplotlib · Chart.js · Docker

---
