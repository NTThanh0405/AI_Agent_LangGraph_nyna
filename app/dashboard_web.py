# app/dashboard_web.py
# Web phục vụ file dashboard, thay cho container nginx (dashboards-web) khi chạy không có Docker.
# Chạy thành tiến trình RIÊNG, tách khỏi agent:  python run_windows.py dashboards
# Cloudflare Tunnel trỏ vào đây (http://localhost:8081), KHÔNG trỏ vào agent.
#
# Giữ đúng luật của nginx-dash.conf cũ:
#   - chỉ phục vụ file tên ngẫu nhiên <48 ký tự hex>.html trong DASHBOARD_DIR (chỉ đọc)
#   - /chart.umd.min.js lấy từ thư mục static/
#   - mọi đường dẫn khác trả 404, không liệt kê thư mục
#   - header noindex, no-store, CSP chặn trang gửi dữ liệu ra ngoài (connect-src 'none')
#
# Không import app.config: tiến trình này không cần (và không nên giữ) API key, token bot, mật khẩu DB.
# Chỉ đọc DASHBOARD_DIR và DASHBOARD_STATIC_DIR từ biến môi trường hoặc .env.

import asyncio
import os
import re
from pathlib import Path

from dotenv import dotenv_values
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Route

ROOT = Path(__file__).resolve().parents[1]
_env = dotenv_values(ROOT / ".env")


def _path(key: str, default: str) -> Path:
    p = Path(os.environ.get(key) or _env.get(key) or default)
    return p if p.is_absolute() else ROOT / p


DASHBOARD_DIR = _path("DASHBOARD_DIR", "data/dashboards")
STATIC_DIR = _path("DASHBOARD_STATIC_DIR", "static")

NAME_RE = re.compile(r"[0-9a-f]{48}\.html")

DASHBOARD_HEADERS = {
    "X-Robots-Tag": "noindex, nofollow",
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self' 'unsafe-inline'; style-src 'unsafe-inline'; "
        "img-src data:; connect-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    ),
}
CHART_HEADERS = {
    "Cache-Control": "public, max-age=86400",
    "X-Content-Type-Options": "nosniff",
}


def _not_found() -> Response:
    return PlainTextResponse("Not Found", status_code=404, headers={"X-Robots-Tag": "noindex, nofollow"})


def _read(path: Path) -> bytes | None:
    # Đọc cả file thay vì stream: file có thể bị cleanup xoá đúng lúc đang gửi.
    # Dashboard nhỏ (agent giới hạn số dòng), đọc một lần là đủ.
    try:
        return path.read_bytes()
    except OSError:
        return None


async def dashboard(request: Request) -> Response:
    name = request.path_params["name"]
    if not NAME_RE.fullmatch(name):
        return _not_found()
    data = await asyncio.to_thread(_read, DASHBOARD_DIR / name)
    if data is None:
        return _not_found()
    return Response(data, media_type="text/html; charset=utf-8", headers=DASHBOARD_HEADERS)


async def chart_js(request: Request) -> Response:
    data = await asyncio.to_thread(_read, STATIC_DIR / "chart.umd.min.js")
    if data is None:
        return _not_found()
    return Response(data, media_type="application/javascript; charset=utf-8", headers=CHART_HEADERS)


async def not_found(request: Request, exc: Exception) -> Response:
    return _not_found()


app = Starlette(
    routes=[
        Route("/chart.umd.min.js", chart_js, methods=["GET"]),
        Route("/{name}", dashboard, methods=["GET"]),
    ],
    exception_handlers={404: not_found, 405: not_found},
)
