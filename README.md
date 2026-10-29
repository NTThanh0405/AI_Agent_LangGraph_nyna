# AI Agent LangGraph – Trợ lý phân tích dữ liệu nội bộ (Nyna)

Trợ lý AI trả lời câu hỏi về số liệu kinh doanh ngay trên **Telegram**. Người dùng hỏi bằng tiếng Việt, agent tự đọc cấu trúc database, viết câu `SELECT`, chạy trên **PostgreSQL nội bộ (chỉ-đọc)** rồi trả lời kèm số liệu thật. Mỗi vai trò (`boss`, `manager`...) chỉ được xem những bảng được phân quyền.

## Kiến trúc

```
Telegram ──► Cloudflare Tunnel ──► n8n ──► Agent (FastAPI + LangGraph) ──► Claude (Anthropic)
                                    │                │
                                    │                └──► PostgreSQL nội bộ (role chỉ-đọc)
                                    └─ xác định user_role từ Telegram user id
```

| Thành phần | Vai trò |
|---|---|
| **cloudflared** | Cloudflare Tunnel đưa n8n ra Internet qua HTTPS, không cần mở port. |
| **n8n** | Nhận tin nhắn Telegram, kiểm tra người dùng được phép, ánh xạ Telegram user id → `user_role`, gọi agent và gửi câu trả lời lại. |
| **agent** | Dịch vụ FastAPI chạy graph LangGraph. Không mở port ra ngoài, chỉ n8n gọi được qua `http://agent:8000`. |
| **Claude** | Model suy luận, quyết định gọi tool nào (model mặc định: `claude-sonnet-5-5`, đổi qua `MODEL_NAME`). |
| **PostgreSQL** | Nguồn dữ liệu phân tích (đọc bằng `agent_readonly`) và nơi lưu hội thoại (ghi bằng `agent_checkpoint`). |

### Luồng xử lý của agent (vòng lặp ReAct)

```
START ─► chat ─┬─ có gọi tool ─► tools ─► chat ─► ...
               └─ không ─────────────────────────► END
```

1. Node `chat` gọi Claude với system prompt + vai trò người hỏi + lịch sử hội thoại.
2. Nếu Claude yêu cầu tool, node `tools` chạy tool rồi trả kết quả về `chat`.
3. Lặp tới khi Claude trả lời xong. Tối đa `MAX_GRAPH_STEPS = 12` bước mỗi lượt hỏi để chặn vòng lặp vô hạn.

### Các tool của agent (`app/agent/tools/sql_tools.py`)

| Tool | Chức năng |
|---|---|
| `list_tables` | Liệt kê bảng/view mà vai trò hiện tại được xem. |
| `describe_table` | Xem cột, kiểu dữ liệu, cho phép NULL hay không của một bảng. |
| `run_select_query` | Chạy **một** câu `SELECT`, trả kết quả dạng bảng chữ (tối đa `DB_MAX_ROWS` dòng, ô dài bị cắt ở 200 ký tự). |

Lỗi của tool được trả về dạng chữ để Claude tự sửa câu SQL.

## Các lớp bảo mật

1. **`x-api-key`** (`app/api/deps.py`): mọi endpoint `/agent/*` yêu cầu header khớp `SERVICE_API_KEY` (so sánh bằng `secrets.compare_digest`). Chỉ n8n giữ khoá này.
2. **Role PostgreSQL chỉ-đọc** (`sql/create_readonly_role.sql`) – *lớp bảo vệ chính*: chỉ `GRANT SELECT` trên các bảng được phép, `default_transaction_read_only = on`, `statement_timeout = 15s`.
3. **Ép read-only phía kết nối** (`app/db/pool.py`): mỗi kết nối trong pool đều set `statement_timeout` và `default_transaction_read_only`.
4. **SQL guard** (`app/db/guard.py`, dùng `sqlglot`) – *lớp phụ*: chỉ một câu `SELECT`/`UNION`; chặn lệnh ghi/DDL, hàm nguy hiểm (`pg_*`, `lo_*`, `dblink`, `set_config`, `*_to_xml`...), bảng hệ thống và truy vấn chéo database; tự bọc `LIMIT`.
5. **Phân quyền theo vai trò** (`app/agent/permissions.py`): `ROLE_TABLES` quy định vai trò nào được xem bảng nào (`*`, `schema.*` hoặc `schema.table`). Vai trò lạ/trống không xem được bảng nào.
6. **Tách hội thoại theo người dùng**: thread nội bộ là `"{user_id}:{thread_id}"`, nên đoán được `thread_id` cũng không đọc được hội thoại của người khác.
7. **`user_role` đi qua `config["configurable"]`**, không nằm trong nội dung chat, nên LLM không tự đổi được vai trò.
8. **Production** (`APP_ENV=production`) tắt `/docs` và `/openapi.json`. Container chạy bằng user không phải root.

> **Lưu ý:** `user_role` do n8n gửi lên, nên n8n phải tự xác định vai trò từ Telegram user id (danh sách cho phép), không lấy từ nội dung người dùng gõ.

## Cấu trúc thư mục

```
.
├── app/
│   ├── main.py                 # Khởi động FastAPI, mở pool + checkpointer, compile graph
│   ├── config.py               # Cấu hình từ biến môi trường / .env
│   ├── agent/
│   │   ├── graph.py            # Dựng graph chat <-> tools
│   │   ├── state.py            # AgentState: messages, user_id, user_role
│   │   ├── permissions.py      # Phân quyền bảng theo vai trò
│   │   ├── nodes/chat.py       # Node gọi Claude + system prompt
│   │   └── tools/              # list_tables, describe_table, run_select_query
│   ├── api/
│   │   ├── deps.py             # Xác thực x-api-key
│   │   └── v1/                 # router, agent_route (invoke), health
│   ├── db/
│   │   ├── pool.py             # Connection pool async (read-only)
│   │   └── guard.py            # Kiểm tra SQL do LLM sinh ra
│   └── schemas/                # request.py, response.py
├── sql/create_readonly_role.sql  # Tạo role agent_readonly + agent_checkpoint
├── Dockerfile
├── docker-compose.yml          # agent + n8n + cloudflared
└── requirements.txt
```

## API

Tiền tố `/api/v1`.

| Method | Đường dẫn | Cần `x-api-key` | Mô tả |
|---|---|---|---|
| `GET` | `/health` | Không | App còn sống (dùng cho Docker HEALTHCHECK). |
| `GET` | `/health/db` | Không | Kiểm tra kết nối PostgreSQL (`SELECT 1`). |
| `POST` | `/agent/invoke` | Có | Gửi câu hỏi cho agent. |

**Request `POST /api/v1/agent/invoke`**

```json
{
  "message": "Doanh thu tuần này theo sản phẩm?",
  "thread_id": "optional-conversation-id",
  "user_id": "123456789",
  "user_role": "manager"
}
```

| Trường | Bắt buộc | Mô tả |
|---|---|---|
| `message` | Có | Câu hỏi (1–4096 ký tự). |
| `thread_id` | Không | ID hội thoại. Bỏ trống để tạo hội thoại mới. |
| `user_id` | Không\* | Telegram user id, dùng cho log và tách hội thoại. |
| `user_role` | Không\* | `boss`, `manager`... Thiếu hoặc lạ thì không xem được bảng nào. |

\* Nên luôn gửi cả hai từ n8n.

**Response**

```json
{
  "answer": "Tuần này doanh thu ...",
  "thread_id": "uuid-hoặc-thread_id-bạn-gửi"
}
```

Lỗi: `401` sai/thiếu API key; `500` agent lỗi hoặc câu hỏi quá phức tạp (vượt giới hạn bước).

## Cài đặt và chạy

### 1. Chuẩn bị PostgreSQL

Chạy `sql/create_readonly_role.sql` bằng tài khoản admin, sau khi sửa:

- `your_db` → tên database thật
- `DOI_MAT_KHAU_1`, `DOI_MAT_KHAU_2` → mật khẩu mạnh
- danh sách bảng trong `GRANT SELECT ...` → đúng những bảng agent được đọc

Script tạo hai tài khoản: `agent_readonly` (chỉ đọc dữ liệu phân tích) và `agent_checkpoint` (chỉ ghi trong schema `agent_state` để lưu hội thoại).

### 2. Phân quyền theo vai trò

Sửa `ROLE_TABLES` trong `app/agent/permissions.py` cho khớp database thật. **Danh sách này phải đồng bộ với các `GRANT SELECT` ở bước 1**: app lọc theo `ROLE_TABLES`, còn DB chỉ cho đọc những bảng đã `GRANT`.

### 3. Tạo file `.env` (không commit)

```env
# Bắt buộc
ANTHROPIC_API_KEY=sk-ant-...
SERVICE_API_KEY=chuoi-bi-mat-dai-ngau-nhien

# PostgreSQL (tài khoản chỉ-đọc)
DB_HOST=host.docker.internal      # hoặc IP/hostname máy chứa DB
DB_PORT=5432
DB_NAME=your_db
DB_USER=agent_readonly
DB_PASSWORD=DOI_MAT_KHAU_1

# Lưu hội thoại vào Postgres (bỏ trống -> lưu RAM, mất khi restart)
CHECKPOINT_DATABASE_URL=postgresql://agent_checkpoint:DOI_MAT_KHAU_2@host.docker.internal:5432/your_db

# Cloudflare Tunnel
CLOUDFLARE_TUNNEL_TOKEN=...

# Tuỳ chọn (giá trị mặc định)
APP_ENV=production                # production sẽ tắt /docs
MODEL_NAME=claude-sonnet-5-5
TEMPERATURE=0
MAX_TOKENS=2048
LLM_TIMEOUT=60
DB_POOL_MIN=1
DB_POOL_MAX=5
DB_STATEMENT_TIMEOUT_MS=15000
DB_MAX_ROWS=200
LOG_LEVEL=INFO
```

> Nếu mật khẩu trong `CHECKPOINT_DATABASE_URL` có ký tự đặc biệt (`@`, `:`, `/`), hãy mã hoá URL ký tự đó.

### 4. Cấu hình `docker-compose.yml`

Đổi `N8N_HOST` và `WEBHOOK_URL` từ `n8n.tenmiencuaban.com` thành tên miền thật. Trong Cloudflare Zero Trust, tạo Tunnel và trỏ hostname công khai tới `http://n8n:5678`, rồi lấy token đặt vào `CLOUDFLARE_TUNNEL_TOKEN`.

### 5. Chạy

```bash
docker compose up -d --build
docker compose logs -f agent
```

Kiểm tra từ trong mạng docker (agent không mở port ra ngoài):

```bash
docker compose exec agent python -c "import urllib.request; print(urllib.request.urlopen('http://localhost:8000/api/v1/health/db').read())"
```

Khi khởi động, log sẽ báo hội thoại đang lưu trong PostgreSQL hay trong RAM.

### 6. Dựng workflow n8n

- **Tác giả:** [NTThanh0405](https://github.com/NTThanh0405)
- **Project:** AI Agent LangGraph NYNA
=======
Mở n8n qua tên miền đã cấu hình và tạo workflow:

1. **Telegram Trigger** nhận tin nhắn.
2. Kiểm tra Telegram user id có trong danh sách cho phép; nếu không thì từ chối.
3. Ánh xạ user id → `user_role` (ví dụ `boss`, `manager`).
4. **HTTP Request** `POST http://agent:8000/api/v1/agent/invoke`, header `x-api-key: <SERVICE_API_KEY>`, body gồm `message`, `thread_id` (ví dụ Telegram chat id), `user_id`, `user_role`.
5. Gửi `answer` về lại Telegram.

## Cấu hình chính

| Biến | Mặc định | Ý nghĩa |
|---|---|---|
| `ANTHROPIC_API_KEY` | – | Khoá gọi Claude (bắt buộc). |
| `SERVICE_API_KEY` | – | Khoá n8n gửi kèm trong `x-api-key` (bắt buộc). |
| `APP_ENV` | `development` | `production` tắt trang `/docs`. |
| `MODEL_NAME` | `claude-sonnet-5-5` | Model Claude sử dụng. |
| `TEMPERATURE` | `0` | Giữ 0 để số liệu ổn định. |
| `MAX_TOKENS` | `2048` | Giới hạn độ dài câu trả lời. |
| `DB_HOST` / `DB_PORT` / `DB_NAME` | `localhost` / `5432` / `postgres` | Kết nối DB phân tích. |
| `DB_USER` / `DB_PASSWORD` | `agent_readonly` / rỗng | Tài khoản chỉ-đọc. |
| `DB_POOL_MIN` / `DB_POOL_MAX` | `1` / `5` | Kích thước connection pool. |
| `DB_STATEMENT_TIMEOUT_MS` | `15000` | Tự huỷ câu SQL chạy quá lâu. |
| `DB_MAX_ROWS` | `200` | Số dòng tối đa trả về cho Claude. |
| `CHECKPOINT_DATABASE_URL` | trống | Chuỗi kết nối lưu hội thoại. Trống = dùng RAM. |
| `LOG_LEVEL` | `INFO` | Mức log. |

## Chạy local để phát triển

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env             # hoặc tự tạo .env như trên, DB_HOST=localhost
uvicorn app.main:app --reload --port 8000
```

Ở chế độ `development`, xem tài liệu API tại `http://localhost:8000/docs`.

## Hạn chế đã biết / việc cần làm

- Chưa có test tự động (nên ưu tiên test cho `app/db/guard.py` và `app/agent/permissions.py`).
- Lịch sử hội thoại chưa được cắt gọn, hội thoại dài sẽ tốn token.
- Mọi vai trò dùng chung một tài khoản DB, nên chưa che cột nhạy cảm theo vai trò (muốn che thì dùng view hoặc `GRANT SELECT (cột...)`).
- Workflow n8n chưa được lưu trong repo.
- Chưa có giới hạn tần suất (rate limit) theo `user_id`.

