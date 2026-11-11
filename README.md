# langgraph-service

Trợ lý phân tích dữ liệu kinh doanh chạy trên **Telegram**, xây bằng **LangGraph + Claude + PostgreSQL**.

Sếp (hoặc quản lý) nhắn tin cho bot, ví dụ *"cho tôi dashboard doanh thu tuần này"*, *"tình hình kinh doanh tháng này thế nào?"*. Agent truy vấn database nội bộ (chỉ đọc), phân tích rồi trả lời kèm số liệu và dashboard HTML. Mỗi sáng bot cũng tự gửi **báo cáo sáng** cho từng manager theo cấu hình.

> Ngoài báo cáo sáng của manager (cấu hình trong `config/reports.yaml`), agent chỉ làm việc khi có yêu cầu. Không có cảnh báo tự động nào khác.
>
> Dự án **không dùng n8n**: bot nhận tin trực tiếp bằng long polling.

---

## Tính năng

- **Chat qua Telegram** bằng long polling, không cần mở port hay webhook công khai.
- **Hiểu thời gian đúng**: "hôm qua", "tuần này", "tháng 10/2025", "từ 1/11 đến 5/11"... được đổi thành khoảng ngày bằng code (`app/agent/periods.py`, múi giờ UTC+7), không để LLM tự đoán ngày.
- **Câu hỏi tự do**: agent liệt kê bảng, xem cột rồi viết SQL `SELECT` để trả lời.
- **Dashboard HTML** (Chart.js) nhiều tab, có bộ lọc và KPI, dựng từ template cố định; LLM chỉ mô tả dashboard, không tự viết HTML. Hai preset: `default` (tự do) và `tong_quan` (theo mẫu "Tổng quan hiệu suất").
- **Báo cáo sáng tự động** cho manager: SQL viết sẵn, không qua LLM, gửi đúng giờ, không gửi trùng, gõ `/reply` để hỏi tiếp ngay trong ngữ cảnh báo cáo.
- **Quản lý phiên chat**: mỗi người giữ tối đa 5 cuộc trò chuyện, chuyển/xoá bằng `/sessions`.
- **Phân quyền 3 tầng**: role (`boss`, `manager`, `staff`), cấp quản trị (`owner`, `admin`, thường) và quyền xem bảng cấp riêng cho nhân viên (có thể đặt hạn).
- **An toàn**: chỉ `SELECT`, guard bằng sqlglot, `statement_timeout`, giới hạn số dòng, rate limit, audit log.

---

## Kiến trúc

```
Telegram (boss / manager / staff)
      │  long polling
      ▼
┌──────────────────── container: agent (1 worker) ─────────────────────┐
│ telegram/bot → middleware (chỉ chat riêng, người trong bảng users)    │
│      │  handlers: lệnh cố định (không qua LLM) hoặc tin thường        │
│      │  ratelimit → runner (khoá theo phiên, giới hạn bước + timeout) │
│      │  gửi "Đang phân tích, khoảng 1 phút..." trong lúc chờ          │
│      ▼                                                               │
│ LangGraph: chat ◄──► tools          (vòng lặp ReAct)                  │
│   ├─ list_tables / describe_table   khám phá schema                   │
│   ├─ run_select_query              chạy SELECT qua guard + quyền      │
│   └─ create_dashboard              dashboard HTML + link              │
│      │                                                               │
│      ▼  guard.py + permissions.py                                    │
│ PostgreSQL: schema dwh (role chỉ-đọc agent_readonly)                  │
│ PostgreSQL: database agent_app, schema agent_state                    │
│   (users, chat_sessions, checkpoint, data_grants, audit_log,          │
│    report_state)                                                      │
│                                                                      │
│ Tác vụ nền: bot polling · dọn dashboard hết hạn · dọn phiên/audit cũ  │
│             · gửi báo cáo sáng (kiểm tra mỗi 60 giây)                 │
└──────────────────────────────────────────────────────────────────────┘
   (tuỳ chọn) dashboards-web (nginx) ◄── cloudflared ──► Internet
```

### Nguyên tắc thiết kế

1. **Khoảng thời gian do code tính**, LLM chỉ diễn giải. Báo cáo sáng dùng SQL cố định và cùng định nghĩa "hôm qua" với agent.
2. **Dữ liệu chỉ đọc**: SQL do LLM sinh phải qua `guard.py`, kết nối PostgreSQL ép `read_only`.
3. **Quyền do server quyết định**: role tra từ bảng `agent_state.users`, không tin gì từ nội dung tin nhắn.
4. **Một worker duy nhất**: khoá phiên và rate limit nằm trong RAM, scheduler báo cáo cũng giả định chỉ có một bản chạy.

---

## Lệnh Telegram

| Lệnh | Ai dùng | Tác dụng |
|---|---|---|
| `/start`, `/help` | Mọi người dùng | Giới thiệu và hướng dẫn (hiện thêm lệnh theo quyền) |
| `/myid` | **Mọi người, kể cả người lạ** | Xem Telegram ID để gửi cho admin |
| `/new` | Mọi người dùng | Bắt đầu cuộc trò chuyện mới |
| `/sessions` | Mọi người dùng | Xem, chuyển hoặc xoá các cuộc trò chuyện |
| `/tuan`, `/thang` | boss, manager | Tình hình kinh doanh tuần này / tháng này |
| `/dashboard` | boss, manager | Dashboard doanh thu tuần này |
| `/quyen` | Mọi người dùng | Xem bạn được xem những dữ liệu nào |

Staff không dùng được `/tuan`, `/thang`, `/dashboard`; staff hỏi trực tiếp về các bảng mình được cấp.

**Báo cáo sáng (chỉ role `manager`):**

| Lệnh | Tác dụng |
|---|---|
| `/baocao` | Xem cấu hình báo cáo sáng của mình |
| `/baocao_tat`, `/baocao_bat` | Tắt / bật báo cáo sáng |
| `/baocao_gio 07:30` | Đổi giờ nhận (chỉ khi `manager_can_change_time: true`) |
| `/reply [câu hỏi]` | Chuyển sang cuộc trò chuyện của báo cáo gần nhất và hỏi tiếp |

**Cấp quyền xem bảng cho nhân viên (boss, manager):**

| Lệnh | Tác dụng |
|---|---|
| `/capquyen <id> <schema.table> [số giờ]` | Cấp quyền xem một bảng, tối đa 8760 giờ, bỏ trống là không giới hạn |
| `/thuhoi <id> <schema.table\|all>` | Thu hồi quyền |
| `/quyen <id>` | Xem nhân viên đó đang được xem bảng nào |

**Quản trị (chỉ admin và owner):**

| Lệnh | Tác dụng |
|---|---|
| `/users` | Danh sách người dùng |
| `/adduser <id> <role> [tên]` | Thêm nhân viên hoặc đổi role |
| `/addadmin <id> <role> [tên]` | Thêm hoặc nâng lên admin |
| `/removeadmin <id>` | Gỡ quyền admin |
| `/removeuser <id>` | Xoá người dùng và toàn bộ cuộc trò chuyện của họ |

Phiên hội thoại: mỗi người tối đa **5** cuộc, cuộc nào **7 ngày** không dùng bị xoá, trần tuyệt đối **14 ngày** kể từ lúc tạo. Thread báo cáo sáng không tính vào hạn mức và giữ 3 ngày. Tin nhắn gửi lúc bot đang tắt mà cũ hơn 10 phút thì bị bỏ qua.

---

## Phân quyền

| Tầng | Cách hoạt động |
|---|---|
| Role (`boss`, `manager`, `staff`) | Khai báo ở `ROLE_TABLES` trong `app/agent/permissions.py`. `boss` và `manager` xem mọi bảng trong schema `dwh`; `staff` mặc định không xem bảng nào |
| Cấp riêng cho staff | Lưu ở `agent_state.data_grants` (có `expires_at`), có hiệu lực ngay, không cần restart |
| Cấp quản trị (`level`) | `owner` đúng 1 người; `admin` tối đa 5 người; còn lại là thường. Quản trị người dùng chỉ dành cho admin trở lên |

Người ngoài bảng `users` bị bỏ qua im lặng (chỉ ghi log), riêng `/myid` vẫn trả lời. Bot chỉ nhận chat riêng, không nhận group.

> Hiện chỉ có **một** tài khoản PostgreSQL chỉ-đọc dùng chung (`agent_readonly`), không tách theo role. Phân quyền theo role do code (`permissions.py` + `guard.py`) đảm nhiệm; cột nhạy cảm của khách nên được che ở phía PostgreSQL.

---

## Báo cáo sáng

- Cấu hình ở `config/reports.yaml`, **tự nạp lại trong vòng 1 phút**, không cần restart. File sai cú pháp thì giữ cấu hình cũ và ghi log lỗi.
- Chỉ role `manager` có tên trong mục `managers` (khoá là Telegram ID) mới nhận. Người đó phải còn tồn tại và có role `manager` trong `/users`.
- Mỗi manager có `send_time`, `sections` và `enabled` riêng; không khai báo thì dùng `default_send_time` và `default_sections`.
- Các mục dùng được: `kpi_summary`, `revenue_by_day`, `revenue_by_channel`, `top_products`, `customers_new_vs_returning`, `target_vs_actual`. Mỗi mục là một file SQL trong `app/agent/queries/` (tham số `%(start)s`, `%(end)s`, `%(end_exclusive)s`).
- Trạng thái đã gửi lưu trong `agent_state.report_state`, nên restart giữa chừng vẫn gửi bù và không gửi trùng trong ngày. Bật lại muộn hơn `max_delay_minutes` thì bỏ qua báo cáo hôm đó.
- Mục nào vượt quyền của manager bị bỏ khỏi báo cáo; một mục lỗi vẫn gửi phần còn lại.
- Lỗi liên tục thì bỏ cuộc sau 5 lần trong ngày.

> ⚠️ Thư mục `app/agent/queries/` hiện **chưa có trong repo**. Cần tự viết các file `*.sql` theo schema thật trước khi báo cáo sáng chạy được.

---

Các thư mục `sql/`, `static/`, `data/` nằm trong `.gitignore` nên **không có trong repo**: bạn cần tự tạo `sql/` (script tạo role/bảng) và `static/chart.umd.min.js` (xem phần Cài đặt).

---

## Yêu cầu

- Docker và Docker Compose
- PostgreSQL (trên máy host hoặc máy khác) gồm database kinh doanh (schema `dwh`) và database `agent_app` (schema `agent_state`)
- Telegram Bot Token (tạo qua [@BotFather](https://t.me/BotFather))
- Anthropic API key
- Cloudflare Tunnel token và một tên miền (để đưa dashboard ra ngoài bằng link)

---

## Cài đặt

### 1. Clone và tạo file `.env`

```bash
git clone https://github.com/NTThanh0405/AI_Agent_LangGraph_nyna.git
cd AI_Agent_LangGraph_nyna
# tạo .env theo mục "Biến môi trường" bên dưới
```

### 2. Chuẩn bị PostgreSQL

- **Database kinh doanh**: tạo role chỉ-đọc `agent_readonly` với quyền `SELECT` trên schema `dwh`, che cột cá nhân của khách (SĐT, email, địa chỉ, ngày sinh).
- **Database `agent_app`**: tạo role `agent_checkpoint` có quyền tạo bảng trong schema `agent_state`. Khi khởi động, app tự tạo bảng checkpoint, `users`, `chat_sessions`, `data_grants`, `audit_log`, `report_state`.

Script tạo role đặt trong `sql/` (thư mục này không được commit). Phải đổi mật khẩu mẫu trước khi chạy.

> Phân quyền được thực thi ở hai lớp: `permissions.py` (trong code) và `GRANT` (trong Postgres). Hai nơi phải khớp nhau.

### 3. Khai báo người dùng ban đầu

`config/users_seed.yaml` **chỉ được nạp khi bảng `agent_state.users` còn trống**. Sau đó hãy thêm/sửa người bằng lệnh Telegram (`/adduser`, `/addadmin`...).

```yaml
users:
  - telegram_id: 123456789
    name: "Anh A"
    role: boss          # boss | manager | staff
    level: owner        # owner (đúng 1 người) | admin | staff
  - telegram_id: 987654321
    name: "Chị B"
    role: manager
    level: admin
```

Lấy Telegram ID bằng cách nhắn `/myid` cho chính bot này.

### 4. Cấu hình báo cáo sáng và tri thức nghiệp vụ

| File | Nội dung |
|---|---|
| `config/reports.yaml` | Manager nào nhận mục nào, lúc mấy giờ |
| `app/agent/queries/*.sql` | SQL cố định cho từng mục báo cáo (**cần tự tạo**) |
| `app/knowledge/glossary.md` | Thuật ngữ, cách tính doanh thu, múi giờ, tiền tệ |
| `app/knowledge/sql_examples.md` | Cặp "câu hỏi → SQL chuẩn" |
| `app/knowledge/business_context.md` | Bối cảnh doanh nghiệp, mục tiêu, mùa vụ |
| `app/knowledge/calendar.yaml` | Khuyến mãi, ngày lễ, sự kiện |

> Lưu ý: các file trong `app/knowledge/` đã có nội dung nhưng **chưa được nạp vào prompt** của agent (`nodes/chat.py` hiện dùng system prompt viết thẳng trong code). `config/thresholds.yaml` và `config/targets.yaml` đang để trống.

### 5. Chart.js cho dashboard

Đặt file `chart.umd.min.js` vào thư mục `static/` (nginx phục vụ tại `/chart.umd.min.js`):

```bash
mkdir -p static
# tải chart.umd.min.js bản đã ghim phiên bản vào static/
```

### 6. Cloudflare Tunnel

Trong Cloudflare Zero Trust → Networks → Tunnels → Public Hostname, trỏ tên miền của bạn tới `HTTP` → `dashboards-web:80`, rồi lấy token đặt vào `CLOUDFLARE_TUNNEL_TOKEN`.

### 7. Chạy

```bash
docker compose up -d --build
docker compose logs -f agent
```

Kiểm tra: nhắn `/start` cho bot từ tài khoản có trong bảng `users`.

---

## Biến môi trường

```env
# LLM
ANTHROPIC_API_KEY=                  # bắt buộc
MODEL_NAME=claude-sonnet-5-5
MAX_TOKENS=4096
LLM_TIMEOUT=60
GRAPH_MAX_STEPS=12                  # số bước tối đa của vòng chat <-> tools mỗi lượt
INVOKE_TIMEOUT_SECONDS=120          # tổng thời gian tối đa mỗi lượt hỏi

# Telegram
TELEGRAM_BOT_TOKEN=                 # bắt buộc

# PostgreSQL kinh doanh (một role chỉ-đọc dùng chung)
DB_HOST=host.docker.internal
DB_PORT=5432
DB_NAME=NynaGroup
DB_USER=agent_readonly
DB_PASSWORD=                        # bắt buộc
DB_SCHEMA=dwh
DB_POOL_MIN=1
DB_POOL_MAX=5
DB_STATEMENT_TIMEOUT_MS=15000
DB_MAX_ROWS=200

# Database agent_app: người dùng, phiên, hội thoại, audit (BẮT BUỘC, thiếu thì app không khởi động)
CHECKPOINT_DATABASE_URL=

# Phiên chat và lưu trữ
MAX_SESSIONS_PER_USER=5
SESSION_IDLE_DAYS=7
SESSION_MAX_DAYS=14
AUDIT_RETENTION_DAYS=30

# Vận hành
TIMEZONE=Asia/Ho_Chi_Minh
RATE_LIMIT_PER_MIN=5
RATE_LIMIT_PER_DAY=100
APP_ENV=production                  # development thì bật /docs
LOG_LEVEL=INFO
REPORTS_CONFIG=                     # tuỳ chọn: đường dẫn khác cho reports.yaml

# Dashboard (gửi bằng link qua nginx + Cloudflare)
DELIVERY_MODE=link
DASHBOARD_BASE_URL=https://dashboard.example.com
DASHBOARD_DIR=/data/dashboards
DASHBOARD_TTL_HOURS=24
DASHBOARD_MAX_CHARTS=6
CLOUDFLARE_TUNNEL_TOKEN=
CLOUDFLARED_IMAGE_TAG=latest        # nên ghim phiên bản đang chạy ổn

# Tuỳ chọn: chỉ cần nếu mở lại endpoint nội bộ có x-api-key (tối thiểu 32 ký tự)
SERVICE_API_KEY=
```

---

## Cách gửi dashboard

Dashboard hiện luôn được **ghi thành file HTML tên ngẫu nhiên** vào `DASHBOARD_DIR` rồi gửi **link**:

1. `agent` ghi file `<48 ký tự hex>.html` (`secrets.token_hex(24)`) vào volume `dashboards`.
2. `dashboards-web` (nginx) phục vụ thư mục đó ở chế độ chỉ đọc.
3. `cloudflared` đưa nginx ra Internet qua HTTPS.
4. File tự xoá sau `DASHBOARD_TTL_HOURS` giờ (kiểm tra mỗi 5 phút); giới hạn 500 file và 200 MB.

Bảo mật: ai có link đều xem được nên đặt hạn ngắn. nginx tắt `autoindex`, chỉ phục vụ đúng tên file hex và `chart.umd.min.js`, thêm `X-Robots-Tag: noindex` và CSP chặn trang gửi dữ liệu ra ngoài (`connect-src 'none'`).

Giới hạn mỗi link: tối đa 6 dashboard (tab), 10.000 dòng dữ liệu mỗi tab, 30.000 dòng cả link.

> `DELIVERY_MODE=file` (gửi file qua Telegram) có trong cấu hình nhưng **chưa được nối vào `create_dashboard`**, nên chỉ chế độ `link` dùng được thực tế. Đặt `DELIVERY_MODE=link` và `DASHBOARD_BASE_URL`.

---

## Bảo mật

| Lớp | Biện pháp |
|---|---|
| Danh tính | Allowlist Telegram ID trong bảng `agent_state.users`; role không bao giờ lấy từ nội dung tin nhắn; chỉ chat riêng |
| Phân quyền | `permissions.py` (role + quyền cấp riêng cho staff) và tài khoản PostgreSQL chỉ-đọc |
| SQL | `guard.py` chỉ cho một câu `SELECT`, chặn hàm nguy hiểm và bảng hệ thống, tự gán schema `dwh`, bọc `LIMIT`; kết nối `read_only`; `statement_timeout`, `lock_timeout`, giới hạn số dòng |
| Dữ liệu là dữ liệu | Prompt dặn bỏ qua câu giống chỉ dẫn nằm trong kết quả truy vấn (chống prompt injection) và không truy vấn SĐT, email, địa chỉ, ngày sinh của khách |
| Dashboard | Tên file ngẫu nhiên 192 bit, TTL, nginx CSP, dữ liệu dashboard cũng đi qua `check_select` và kiểm tra quyền |
| Chi phí | Rate limit theo phút/ngày, giới hạn bước và timeout mỗi lượt, cắt lịch sử còn 30 tin, audit theo user |
| Triển khai | Container `agent` không mở port ra ngoài, chạy bằng user không phải root, `cap_drop: ALL`, `no-new-privileges` |
| Truy vết | `audit_log` ghi từng lượt hỏi và thay đổi quyền, giữ 30 ngày |

**Lưu ý vận hành:**
- Chạy **1 worker** vì khoá phiên, rate limit và scheduler báo cáo nằm trong RAM. Không thêm `--workers`.
- Không commit `.env`. Đổi toàn bộ mật khẩu mẫu trước khi chạy.
- Nếu bot dừng vì lỗi không phục hồi (ví dụ token sai), service tự tắt để Docker khởi động lại.

---

## Kiểm thử

```bash
pip install -r requirements-dev.txt
pytest
```

| Test | Kiểm tra |
|---|---|
| `test_guard.py` | Các câu SQL tấn công và schema mặc định |
| `test_permissions.py` | Quyền theo role và quyền cấp riêng cho staff |
| `test_periods.py` | "Tuần này/tháng này" ở các ngày biên |
| `test_reports.py` | Đọc cấu hình, đúng giờ/không gửi trùng, dựng nội dung, vòng gửi, thread báo cáo |
| `test_report_commands.py` | Lệnh `/baocao*`, `/reply` |
| `check_report_permissions.py` | Script kiểm tra mục báo cáo nào bị chặn quyền với role manager |

---

## Phát triển cục bộ

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
# tạo .env (cần cả DB kinh doanh và CHECKPOINT_DATABASE_URL)
python -m app.main               # hoặc: uvicorn app.main:app --port 8080
```

Khi `APP_ENV=development`, trang `/docs` của FastAPI được bật; ở `production` thì tắt.

---

## Xử lý sự cố

| Triệu chứng | Nguyên nhân thường gặp |
|---|---|
| App không khởi động, báo thiếu `CHECKPOINT_DATABASE_URL` | Biến này bắt buộc vì người dùng và phiên lưu trong database `agent_app` |
| Bot không phản hồi | Sai `TELEGRAM_BOT_TOKEN`, hoặc Telegram ID chưa có trong bảng `users` (nhắn `/myid` để lấy ID rồi nhờ admin thêm) |
| Polling bị Telegram từ chối (409) | Bot còn webhook cũ; app đã tự gọi `delete_webhook` khi khởi động, kiểm tra không có tiến trình khác dùng cùng token |
| Container agent unhealthy | Xem `docker compose logs agent`; thường do không kết nối được DB, hoặc healthcheck gọi sai cổng/đường dẫn (app lắng nghe cổng **8080**, đường dẫn `/api/v1/health`) |
| Không kết nối được Postgres trên máy host | Đặt `DB_HOST=host.docker.internal`, kiểm tra `listen_addresses` và `pg_hba.conf` |
| "Không có quyền truy cập các bảng..." | Bảng nằm ngoài `dwh`, hoặc staff chưa được `/capquyen` |
| Truy vấn bị huỷ vì quá lâu | Thu hẹp khoảng thời gian, hoặc tăng `DB_STATEMENT_TIMEOUT_MS` có cân nhắc |
| Báo cáo sáng không gửi / thiếu mục | Chưa có `app/agent/queries/*.sql`, manager không có role `manager`, hoặc quá `max_delay_minutes`; chạy `check_report_permissions.py` để xem mục nào bị chặn |
| Báo cáo sáng đổi cấu hình không có tác dụng | `reports.yaml` sai cú pháp nên service giữ bản cũ; xem log. Trong Docker cần mount thư mục `config/` (hiện `docker-compose.yml` và `Dockerfile` chưa mount/copy `config/`) |
| Dashboard không mở được | `DASHBOARD_BASE_URL` sai, thiếu `static/chart.umd.min.js`, hoặc hostname Cloudflare chưa trỏ tới `dashboards-web:80` |
| Link dashboard 404 | Link đã hết hạn (`DASHBOARD_TTL_HOURS`) |
| Số liệu lệch so với báo cáo cũ | Kiểm tra định nghĩa doanh thu trong `app/knowledge/glossary.md` và múi giờ dữ liệu |
| Câu trả lời bị cắt ngang | Tăng `MAX_TOKENS` |
| "Câu hỏi quá phức tạp" / "xử lý quá lâu" | Vượt `GRAPH_MAX_STEPS` hoặc `INVOKE_TIMEOUT_SECONDS`; chia nhỏ câu hỏi |

---

## Trạng thái triển khai

Đã có trong repo:
- [x] Bot Telegram (polling, allowlist trong DB, `/new`, `/sessions`, cắt tin, báo tiến độ)
- [x] Phân quyền 3 tầng và quản trị người dùng bằng lệnh Telegram
- [x] `periods.py` + test, tool SQL/khám phá schema, `guard.py` + test
- [x] Dashboard nhiều tab bằng preset, gửi link qua nginx + Cloudflare Tunnel
- [x] Báo cáo sáng cho manager, `/reply`, `/baocao*`
- [x] Audit log, rate limit, dọn dữ liệu nền

Còn thiếu hoặc chưa nối:
- [ ] `app/agent/queries/*.sql` (báo cáo sáng cần)
- [ ] Nạp `app/knowledge/*` vào prompt; `thresholds.yaml`, `targets.yaml` còn trống
- [ ] Đăng ký `get_date_range`, `get_sample_rows`, `list_dashboard_presets` vào `ALL_TOOLS` (đã viết nhưng agent chưa gọi được)
- [ ] `snapshot_tools.py`, `context_tools.py`, `analysis_tools.py` (file còn trống)
- [ ] Chế độ gửi dashboard `file`
- [ ] Tách tài khoản PostgreSQL theo role, bộ eval hồi quy

---

## Công nghệ

Python 3.14 · FastAPI · LangGraph · LangChain · Claude (Anthropic) · aiogram · psycopg 3 · sqlglot · PostgreSQL · nginx · Cloudflare Tunnel · Docker
