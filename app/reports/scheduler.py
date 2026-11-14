# app/reports/scheduler.py
# Vòng nền gửi báo cáo sáng cho manager. Cứ mỗi 60 giây kiểm tra "ai đến giờ mà hôm nay chưa
# nhận" (xem due_reports) rồi dựng báo cáo, gửi Telegram, ghi nhận đã gửi. Không dùng LLM để
# quyết định giờ giấc. Sau khi gửi, nội dung báo cáo được ghi vào một thread riêng (loại 'report',
# không tính vào hạn mức phiên) để manager gõ /reply hỏi tiếp trong đúng ngữ cảnh báo cáo.
#
# v2: giờ tính theo timezone trong reports.yaml; số lần thử lại theo retry_on_error; người quá
# max_delay_minutes được ghi nhận "bỏ lỡ" (skipped_late) đúng 1 lần/ngày; mọi kết quả đều ghi audit.
#
# Chỉ chạy đúng với MỘT worker (giống khoá phiên trong sessions.py); chạy nhiều bản sao thì
# cần khoá DB để không gửi đôi.

import asyncio
import logging
from datetime import date, datetime
from typing import Callable, Optional

from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from langchain_core.messages import AIMessage

from app.agent.sessions import drop_session, register_report_thread, report_thread_id
from app.audit import log_turn
from app.reports.builder import build_report, split_text
from app.reports.subscriptions import (
    REPORT_ROLE,
    REPORT_ROLES,
    ConfigStore,
    ReportConfig,
    due_reports,
    late_reports,
    load_states,
    mark_sent,
    mark_skipped,
    now_in_tz,
)
from app.telegram.auth import get_allowed_user

logger = logging.getLogger(__name__)

LOOP_SECONDS = 60
SEND_GAP_SECONDS = 0.5         # nghỉ giữa hai người để không chạm giới hạn tốc độ của Telegram
_GAVE_UP = 10**9               # đánh dấu "đã bỏ cuộc trong ngày" cho bộ đếm lần thử

# {user_id: (ngày, số lần đã thử lỗi)}; mất khi restart là chấp nhận được
_attempts: dict[int, tuple[date, int]] = {}

# Trạng thái lượt chạy gần nhất, cho /health (api/v1/health.py) đọc qua get_status()
_STATUS: dict = {"last_run_at": None, "last_sent": 0, "last_error": None}


def get_status() -> dict:
    """Bản sao trạng thái scheduler: last_run_at (ISO, theo timezone trong yaml), last_sent (số báo cáo
    gửi ở lượt gần nhất), last_error (None nếu lượt gần nhất không lỗi)."""
    return dict(_STATUS)


def _max_attempts(cfg: ReportConfig) -> int:
    """retry_on_error = số lần thử lại, không tính lần đầu."""
    return cfg.retry_on_error + 1


def _attempts_today(uid: int, today: date) -> int:
    day, n = _attempts.get(uid, (today, 0))
    return n if day == today else 0


def _record_failure(uid: int, today: date) -> int:
    n = _attempts_today(uid, today) + 1
    _attempts[uid] = (today, n)
    return n


async def _audit(state_pool, uid: int, role: str, status: str, note: str) -> None:
    """Ghi 1 dòng audit_log (status: ok / error / skipped_late) qua audit.log_turn, hàm này không bao
    giờ raise nên lỗi ghi nhật ký không làm hỏng việc gửi báo cáo."""
    await log_turn(
        state_pool,
        user_id=str(uid),
        role=role,
        question=f"[báo cáo tự động] {note}",
        status=status,
    )


async def _give_up(state_pool, uid: int, role: str, today: date, reason: str) -> None:
    """Bỏ cuộc trong ngày: ghi audit lỗi + đánh dấu bỏ qua trong DB (restart cũng không thử lại)."""
    _attempts[uid] = (today, _GAVE_UP)
    await mark_skipped(state_pool, uid, today)
    await _audit(state_pool, uid, role, "error", f"bỏ cuộc trong ngày: {reason}")


async def _send(bot, chat_id: int, chunks: list[str]) -> None:
    # parse_mode=None: báo cáo là chữ thuần, tránh lỗi nếu bot đặt sẵn HTML/Markdown mặc định.
    # Nếu lỗi giữa chừng khi gửi nhiều tin thì lần thử sau gửi lại từ đầu (báo cáo thường chỉ 1 tin).
    for chunk in chunks:
        try:
            await bot.send_message(chat_id, chunk, parse_mode=None)
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after)
            await bot.send_message(chat_id, chunk, parse_mode=None)


async def _record_thread(graph, state_pool, uid: int, role: str, text: str, title: str, today: date) -> None:
    """Lưu báo cáo thành một thread loại 'report' để /reply chat tiếp. Lỗi ở đây không ảnh hưởng
    việc đã gửi báo cáo."""
    thread = report_thread_id(uid, today)
    await register_report_thread(state_pool, uid, thread, title)
    config = {"configurable": {"thread_id": thread, "user_id": str(uid), "user_role": role}}
    try:
        # as_node="chat": coi như node chat vừa trả lời, nên lượt hỏi sau nối tiếp bình thường
        await graph.aupdate_state(
            config,
            {"messages": [AIMessage(content=text)], "user_id": str(uid), "user_role": role},
            as_node="chat",
        )
    except Exception:
        await drop_session(graph, state_pool, thread)  # không để lại thread rỗng không có ngữ cảnh
        raise


async def _deliver(bot, graph, state_pool, get_data_pool, cfg: ReportConfig, due, now: datetime) -> bool:
    manager, today = due.manager, now.date()
    uid = manager.user_id

    user = await get_allowed_user(state_pool, uid)
    if user is None or (user.role or "").lower() not in REPORT_ROLES:
        logger.warning("Bỏ qua báo cáo cho %s: không còn trong danh sách hoặc không phải %s", uid, "/".join(sorted(REPORT_ROLES)))
        await _give_up(state_pool, uid, REPORT_ROLE, today, "không còn trong danh sách hoặc không phải manager/boss")
        return False

    commentary_fn = None
    if cfg.commentary_enabled:
        # tuỳ chọn: thiếu module hoặc lỗi import thì gửi báo cáo không nhận xét, không được chặn báo cáo
        try:
            from app.reports.commentary import make_commentary

            commentary_fn = make_commentary
        except Exception:
            logger.warning("commentary_enabled bật nhưng không nạp được commentary.py", exc_info=True)

    try:
        # builder.py kiểm tra quyền từng mục theo role; mục vượt quyền nằm trong built.denied, mục lỗi trong built.failed
        built = await build_report(
            get_data_pool(user.role), user.role, manager.sections, today=today, commentary=commentary_fn
        )
        text = built.text + (f"\n\n{cfg.footer}" if cfg.footer else "")
        await _send(bot, uid, split_text(text))
    except TelegramForbiddenError:
        logger.warning("Không gửi được cho %s (chưa /start hoặc đã chặn bot), thử lại ngày mai", uid)
        await _give_up(state_pool, uid, user.role, today, "chưa /start hoặc đã chặn bot")
        return False
    except Exception as e:
        logger.exception("Gửi báo cáo lỗi (user_id=%s), sẽ thử lại", uid)
        n = _record_failure(uid, today)
        if n >= _max_attempts(cfg):
            await _give_up(state_pool, uid, user.role, today, f"lỗi {n} lần, lỗi cuối: {e!r}")
        return False

    try:
        await mark_sent(state_pool, uid, today)  # ghi ngay sau khi gửi được để không gửi trùng
    except Exception:
        # Báo cáo ĐÃ tới tay người nhận nhưng chưa ghi được vào DB. Nếu để ngoại lệ bay ra ngoài thì
        # lượt sau (60 giây) vẫn thấy "chưa gửi" và gửi lại mỗi phút. Chặn trong RAM cho hết hôm nay.
        logger.exception("Đã gửi báo cáo nhưng không ghi được report_state (user_id=%s)", uid)
        _attempts[uid] = (today, _GAVE_UP)
    logger.info(
        "Đã gửi báo cáo user_id=%s mục=%s lỗi=%s vượt quyền=%s nhận xét=%s",
        uid, built.ok, built.failed, built.denied, built.commentary,
    )
    await _audit(
        state_pool, uid, user.role, "ok",
        f"đã gửi lúc {now:%H:%M}, mục ok={built.ok}, lỗi={built.failed}, "
        f"vượt quyền={built.denied}, nhận xét={'có' if built.commentary else 'không'}",
    )

    try:
        await _record_thread(graph, state_pool, uid, user.role, text, f"Báo cáo {today:%d/%m}", today)
    except Exception:
        logger.exception("Không lưu được thread báo cáo (user_id=%s); /reply sẽ không có ngữ cảnh", uid)
    return True


async def _skip_late(state_pool, cfg: ReportConfig, states, now: datetime) -> int:
    """Ghi nhận những manager đã quá max_delay_minutes mà chưa nhận. Mỗi người đúng 1 lần/ngày
    nhờ cột last_skipped_on. Trả về số người bị bỏ lỡ."""
    n = 0
    for manager in late_reports(cfg, states, now):
        await mark_skipped(state_pool, manager.user_id, now.date())
        await _audit(
            state_pool, manager.user_id, REPORT_ROLES, "skipped_late",
            f"quá {cfg.max_delay_minutes} phút sau giờ hẹn nên bỏ qua báo cáo ngày {now:%d/%m}",
        )
        logger.warning("Bỏ lỡ báo cáo của user_id=%s (quá %s phút sau giờ hẹn)", manager.user_id, cfg.max_delay_minutes)
        n += 1
    return n


async def run_once(
    bot,
    graph,
    state_pool,
    get_data_pool: Callable[[str], object],
    store: ConfigStore,
    now: Optional[datetime] = None,
) -> int:
    """Một lượt kiểm tra. Trả về số báo cáo đã gửi.

    get_data_pool(role) trả về pool tới database dữ liệu kinh doanh (tài khoản chỉ-đọc) của role đó.
    `now` phải có múi giờ (dùng cho test); mặc định lấy giờ theo timezone trong reports.yaml.
    """
    cfg = store.get()
    if cfg is None:
        _STATUS["last_error"] = "chưa nạp được config/reports.yaml"
        return 0
    now = now or now_in_tz(cfg)
    states = await load_states(state_pool)

    sent = 0
    for due in due_reports(cfg, states, now):
        uid = due.manager.user_id
        if _attempts_today(uid, now.date()) >= _max_attempts(cfg):
            continue
        try:
            if await _deliver(bot, graph, state_pool, get_data_pool, cfg, due, now):
                sent += 1
        except Exception:
            # lỗi bất ngờ của 1 người (vd mất kết nối DB) không được chặn những người sau
            logger.exception("Lỗi khi xử lý báo cáo của user_id=%s", uid)
        await asyncio.sleep(SEND_GAP_SECONDS)

    try:
        await _skip_late(state_pool, cfg, states, now)
    except Exception:
        logger.exception("Lỗi khi ghi nhận báo cáo bị bỏ lỡ")
    _STATUS.update(last_run_at=now.isoformat(timespec="seconds"), last_sent=sent, last_error=None)
    return sent


async def report_loop(
    bot, graph, state_pool, get_data_pool, store: ConfigStore, interval: float = LOOP_SECONDS
) -> None:
    """Chạy mãi cho tới khi bị huỷ (app tắt). Lỗi một lượt chỉ ghi log, vòng vẫn tiếp tục."""
    logger.info("Vòng gửi báo cáo tự động đã chạy (kiểm tra mỗi %ss)", interval)
    while True:
        try:
            await run_once(bot, graph, state_pool, get_data_pool, store)
        except Exception as e:
            _STATUS["last_error"] = repr(e)
            logger.exception("report_loop lỗi")
        await asyncio.sleep(interval)