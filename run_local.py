# run_windows.py
# Điểm chạy dịch vụ khi KHÔNG dùng Docker (Windows Server 2022, hoặc chạy tay trên Linux).
#
#   python run_windows.py agent        -> bot Telegram + LangGraph + FastAPI (health) ở 127.0.0.1:8080
#   python run_windows.py dashboards   -> web phục vụ file dashboard (thay nginx) ở 127.0.0.1:8081
#
# Vì sao không gọi thẳng "uvicorn app.main:app"?
#   Trên Windows, uvicorn mặc định dùng ProactorEventLoop, mà psycopg (driver PostgreSQL bất đồng bộ)
#   KHÔNG chạy được trên ProactorEventLoop ("Psycopg cannot use the 'ProactorEventLoop'...").
#   File này ép dùng SelectorEventLoop rồi mới chạy uvicorn.
#
# Host/port đọc từ biến môi trường hoặc .env (mặc định chỉ nghe trên 127.0.0.1, không mở ra ngoài):
#   APP_HOST=127.0.0.1  APP_PORT=8080
#   DASHBOARD_WEB_HOST=127.0.0.1  DASHBOARD_WEB_PORT=8081
#
# Chỉ chạy MỘT tiến trình agent (khoá phiên, rate limit, scheduler báo cáo nằm trong RAM).

import asyncio
import os
import sys
from pathlib import Path

import uvicorn
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent

TARGETS = {
    # tên: (ứng dụng ASGI, biến host, host mặc định, biến port, port mặc định)
    "agent": ("app.main:app", "APP_HOST", "127.0.0.1", "APP_PORT", 8080),
    "dashboards": ("app.dashboard_web:app", "DASHBOARD_WEB_HOST", "127.0.0.1", "DASHBOARD_WEB_PORT", 8081),
}


def _setting(env: dict, key: str, default: str) -> str:
    """Ưu tiên biến môi trường thật, sau đó tới file .env, cuối cùng là giá trị mặc định."""
    return os.environ.get(key) or env.get(key) or default


def main() -> None:
    if len(sys.argv) != 2 or sys.argv[1] not in TARGETS:
        print("Cách dùng: python run_windows.py [agent|dashboards]")
        sys.exit(2)

    # Chạy từ thư mục dự án để .env, config/*.yaml, data/ được tìm đúng dù dịch vụ khởi động ở đâu
    os.chdir(ROOT)
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))

    app_path, host_key, host_default, port_key, port_default = TARGETS[sys.argv[1]]
    env = dotenv_values(ROOT / ".env")
    config = uvicorn.Config(
        app_path,
        host=_setting(env, host_key, host_default),
        port=int(_setting(env, port_key, str(port_default))),
        log_level=_setting(env, "LOG_LEVEL", "INFO").lower(),
        workers=1,
        proxy_headers=False,
        server_header=False,
        date_header=False,
    )
    server = uvicorn.Server(config)

    if sys.platform == "win32":
        asyncio.run(server.serve(), loop_factory=asyncio.SelectorEventLoop)
    else:
        server.run()


if __name__ == "__main__":
    main()
