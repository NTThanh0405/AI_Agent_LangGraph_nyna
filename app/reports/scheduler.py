# app/reports/scheduler.py
# Vòng nền gửi báo cáo sáng cho manager. Cứ mỗi 60 giây kiểm tra "ai đến giờ mà hôm nay chưa
# nhận" (xem due_reports) rồi dựng báo cáo, gửi Telegram, ghi nhận đã gửi. Không dùng LLM để
# quyết định giờ giấc. Sau khi gửi, nội dung báo cáo được ghi vào một thread riêng (loại 'report',
# không tính vào hạn mức phiên) để manager gõ /reply hỏi tiếp trong đúng ngữ cảnh báo cáo.
#
# Chỉ chạy đúng với MỘT worker (giống khoá phiên trong sessions.py); chạy nhiều bản sao thì
# cần khoá DB để không gửi đôi.

import asyncio
import logging
from datetime import date, datetime
from typing import Callable, Optional

from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from langchain_core.messages import AIMessage

from app.agent.periods import now_local
from app.agent.sessions import drop_session, register_report_thread, report_thread_id
from app.reports.builder import build_report, split_text
from app.reports.subscriptions import REPORT_ROLE, ConfigStore, due_reports, load_states, mark_sent
from app.telegram.auth import get_allowed_user

logger = logging.getLogger(__name__)

LOOP_SECONDS = 60
SEND_GAP_SECONDS = 0.5         # nghỉ giữa hai người để không chạm giới hạn tốc độ của Telegram
MAX_ATTEMPTS_PER_DAY = 5       # lỗi liên tục thì bỏ cuộc trong ngày, không thử mỗi phút suốt buổi sáng

# {user_id: (ngày, số lần đã thử)}; mất khi restart là chấp nhận được
_attempts: dict[int, tuple[date, int]] = {}


def _attempts_today(uid: int, today: date) -> int:
    day, n = _attempts.get(uid, (today, 0))
    return n if day == today else 0


def _record_failure(uid: int, today: date) -> None:
    _attempts[uid] = (today, _attempts_today(uid, today) + 1)


def _skip_today(uid: int, today: date) -> None:
    _attempts[uid] = (today, MAX_ATTEMPTS_PER_DAY)


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


async def _deliver(bot, graph, state_pool, get_data_pool, cfg, due, now: datetime) -> bool:
    manager, today = due.manager, now.date()
    uid = manager.user_id

    user = await get_allowed_user(state_pool, uid)
    if user is None or user.role != REPORT_ROLE:
        logger.warning("Bỏ qua báo cáo cho %s: không còn trong danh sách hoặc không phải %s", uid, REPORT_ROLE)
        _skip_today(uid, today)
        return False

    try:
        built = await build_report(get_data_pool(user.role), user.role, manager.sections, today=today)
        text = built.text + (f"\n\n{cfg.footer}" if cfg.footer else "")
        await _send(bot, uid, split_text(text))
    except TelegramForbiddenError:
        logger.warning("Không gửi được cho %s (chưa /start hoặc đã chặn bot), thử lại ngày mai", uid)
        _skip_today(uid, today)
        return False
    except Exception:
        logger.exception("Gửi báo cáo lỗi (user_id=%s), sẽ thử lại", uid)
        _record_failure(uid, today)
        return False

    await mark_sent(state_pool, uid, today)  # ghi ngay sau khi gửi được để không gửi trùng
    logger.info("Đã gửi báo cáo user_id=%s mục=%s lỗi=%s", uid, built.ok, built.failed)

    try:
        await _record_thread(graph, state_pool, uid, user.role, text, f"Báo cáo {today:%d/%m}", today)
    except Exception:
        logger.exception("Không lưu được thread báo cáo (user_id=%s); /reply sẽ không có ngữ cảnh", uid)
    return True


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
    """
    cfg = store.get()
    if cfg is None:
        return 0
    now = now or now_local()
    states = await load_states(state_pool)

    sent = 0
    for due in due_reports(cfg, states, now):
        uid = due.manager.user_id
        if _attempts_today(uid, now.date()) >= MAX_ATTEMPTS_PER_DAY:
            continue
        if await _deliver(bot, graph, state_pool, get_data_pool, cfg, due, now):
            sent += 1
        await asyncio.sleep(SEND_GAP_SECONDS)
    return sent


async def report_loop(
    bot, graph, state_pool, get_data_pool, store: ConfigStore, interval: float = LOOP_SECONDS
) -> None:
    """Chạy mãi cho tới khi bị huỷ (app tắt). Lỗi một lượt chỉ ghi log, vòng vẫn tiếp tục."""
    logger.info("Vòng gửi báo cáo tự động đã chạy (kiểm tra mỗi %ss)", interval)
    while True:
        try:
            await run_once(bot, graph, state_pool, get_data_pool, store)
        except Exception:
            logger.exception("report_loop lỗi")
        await asyncio.sleep(interval)