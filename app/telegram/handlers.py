# app/telegram/handlers.py
# Các handler của bot Telegram:
#   /start /help /new /sessions /tuan /thang /dashboard /myid, tin nhắn thường, phản hồi tin lạ,
#   báo cáo sáng của manager (/reply /baocao /baocao_tat /baocao_bat /baocao_gio),
#   nút bấm trong /sessions (chuyển/xoá phiên), và lệnh quản trị người dùng
#   (/adduser /addadmin /removeadmin /removeuser /users), và quyền theo KHỐI dữ liệu (/capquyen /thuhoi /quyen).
# Quyền của staff tính theo KHỐI dữ liệu (config/scopes.yaml; hiện có coffee, sau thêm house, misa, base):
# boss/manager cấp/thu hồi bằng /capquyen, /thuhoi (theo khối, không cấp từng bảng). Có khối nào mới xem
# được dữ liệu khối đó (coffee: database iPOS; các khối API: lấy qua API của từng bên khi đã cấu hình).
# /tuan /thang /dashboard: mọi role dùng được, số liệu giới hạn trong khối người hỏi được xem.
# Mọi tin nhắn và nút bấm đi qua middleware trước: chỉ chat riêng, chỉ người có trong bảng
# agent_state.users (PostgreSQL), tin nhắn quá cũ bị bỏ qua. Người lạ bị im lặng bỏ qua (chỉ ghi
# log), trừ lệnh /myid để họ lấy được Telegram ID gửi cho quản trị viên.
# Lệnh quản trị và quản lý phiên là code cố định, không đi qua LLM.

import logging
import re
import time
from datetime import datetime, timezone
from datetime import time as dtime
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

from aiogram import BaseMiddleware, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.agent.grants import add_grant, list_grants, revoke_grant
from app.agent.permissions import ROLE_TABLES
from app.agent.runner import MSG_BUSY, run_turn
from app.agent.scopes import describe_scopes, load_scopes_config, user_scopes
from app.agent.sessions import (
    REPORT_RETENTION_DAYS,
    create_session,
    delete_own_session,
    is_busy,
    latest_report_thread,
    list_sessions,
    peek_active,
    purge_user_sessions,
    reuse_empty_active,
    session_expires_at,
    switch_session,
)
from app.audit import log_turn
from app.config import get_settings
from app.reports.builder import SECTIONS
from app.reports.subscriptions import (
    REPORT_ROLE,
    effective_send_time,
    get_store,
    load_states,
    set_paused,
    set_send_time,
)
from app.telegram.auth import (
    ADMIN,
    OWNER,
    STAFF,
    AllowedUser,
    check_manage,
    demote_admin,
    get_allowed_user,
    get_user,
    list_users,
    log_change,
    remove_user,
    upsert_user,
    valid_role,
)
from app.telegram.formatter import send_text
from app.telegram.progress import progress
from app.telegram.ratelimit import get_limiter

logger = logging.getLogger(__name__)

# Tin nhắn gửi lúc bot đang tắt: quá cũ thì bỏ qua thay vì trả lời muộn hàng giờ.
STALE_AFTER_SECONDS = 600

_cfg = get_settings()

HELP_TEXT = (
    "Mình là trợ lý phân tích dữ liệu kinh doanh. Bạn cứ hỏi tự nhiên, ví dụ:\n"
    "• Doanh thu tuần này thế nào?\n"
    "• So sánh doanh thu tháng này với tháng trước\n"
    "• Sản phẩm nào đang kéo doanh thu xuống?\n"
    "• Cho tôi dashboard doanh thu tuần này\n\n"
    "Lệnh nhanh:\n"
    "/tuan - tình hình kinh doanh tuần này\n"
    "/thang - tình hình kinh doanh tháng này\n"
    "/dashboard - dashboard doanh thu tuần này\n"
    "/new - bắt đầu cuộc trò chuyện mới\n"
    "/sessions - xem, chuyển hoặc xoá các cuộc trò chuyện\n"
    "/quyen - xem bạn được xem những khối dữ liệu nào\n"
    "/help - xem hướng dẫn\n\n"
    f"Lưu ý: mỗi người giữ tối đa {_cfg.max_sessions_per_user} cuộc trò chuyện; cuộc nào "
    f"{_cfg.session_idle_days} ngày không dùng sẽ bị xoá."
)

ADMIN_HELP_TEXT = (
    "\n\nLệnh quản trị:\n"
    "/users - danh sách người dùng\n"
    "/adduser <id> <role> [tên] - thêm nhân viên hoặc đổi role\n"
    "/addadmin <id> <role> [tên] - thêm hoặc nâng lên admin\n"
    "/removeadmin <id> - gỡ quyền admin\n"
    "/removeuser <id> - xóa người dùng (và toàn bộ cuộc trò chuyện của họ)"
)

def _grant_help() -> str:
    return (
        f"\n\nCấp quyền dữ liệu cho nhân viên (role staff), theo khối ({_scope_list()}):\n"
        "/capquyen <id> <khối> [số giờ] - cấp quyền xem một khối, bỏ trống số giờ là không giới hạn\n"
        "/thuhoi <id> <khối|all> - thu hồi quyền\n"
        "/quyen <id> - xem nhân viên đang được xem những khối nào"
    )


def _scope_list() -> str:
    return ", ".join(load_scopes_config().names())


MSG_NO_SCOPE = (
    "Bạn chưa được cấp quyền xem khối dữ liệu nào ({scopes}). "
    "Liên hệ quản lý để được cấp, gõ /quyen để kiểm tra lại."
)
MSG_DASHBOARD_NEEDS_DB = (
    "Dashboard hiện chỉ dựng được từ dữ liệu trong database (khối {db}), mà bạn chưa được cấp khối này. "
    "Gõ /tuan hoặc /thang để xem số liệu dạng chữ của khối bạn được xem."
)


PROMPT_WEEK = (
    "Tình hình kinh doanh tuần này thế nào? Cho tôi các số liệu chính, so với tuần trước, "
    "và đề xuất."
)
PROMPT_MONTH = (
    "Tình hình kinh doanh tháng này thế nào? Cho tôi các số liệu chính, so với tháng trước, "
    "và đề xuất."
)
PROMPT_DASHBOARD = "Tạo dashboard doanh thu tuần này."

_TAG = {OWNER: " (chủ)", ADMIN: " (admin)", STAFF: ""}

MSG_NOT_MANAGER = "Lệnh này dành cho tài khoản nhận báo cáo tự động hằng ngày."
MSG_NOT_REGISTERED = "Bạn chưa được đăng ký nhận báo cáo tự động. Liên hệ admin để được thêm."


def _is_report_user(user: AllowedUser) -> bool:
    return (user.role or "").lower() == REPORT_ROLE


GRANTOR_ROLES = {"boss", "manager"}  # role được cấp/thu hồi quyền xem bảng cho staff


def _can_grant(user: AllowedUser) -> bool:
    return (user.role or "").lower() in GRANTOR_ROLES


def _manager_help(user: AllowedUser) -> str:
    """Phần hướng dẫn báo cáo sáng, chỉ hiện cho manager."""
    if not _is_report_user(user):
        return ""
    cfg = get_store().get()
    lines = [
        "\n\nBáo cáo tự động hằng ngày:",
        "/baocao - xem cấu hình báo cáo sáng của bạn",
        "/baocao_tat, /baocao_bat - tắt hoặc bật báo cáo sáng",
        "/reply [câu hỏi] - hỏi tiếp về báo cáo vừa nhận",
    ]
    if cfg is not None and cfg.manager_can_change_time:
        lines.insert(4, "/baocao_gio 07:30 - đổi giờ nhận báo cáo")
    return "\n".join(lines)


def _is_myid(event: Message) -> bool:
    text = (event.text or "").strip().lower()
    return text == "/myid" or text.startswith("/myid ") or text.startswith("/myid@")


class AllowlistMiddleware(BaseMiddleware):
    """Chỉ cho qua tin nhắn chat riêng, còn mới, của người có trong bảng users.

    Ngoại lệ: /myid cho mọi người (kể cả người lạ) để lấy Telegram ID.
    """

    async def __call__(
        self,
        handler: Callable[[Message, dict[str, Any]], Awaitable[Any]],
        event: Message,
        data: dict[str, Any],
    ) -> Any:
        if event.chat.type != ChatType.PRIVATE or event.from_user is None:
            return None  # bỏ qua group/channel

        age = (datetime.now(timezone.utc) - event.date).total_seconds()
        if age > STALE_AFTER_SECONDS:
            logger.info("Bỏ qua tin nhắn cũ %.0fs (telegram_id=%s)", age, event.from_user.id)
            return None

        if _is_myid(event):
            data["user"] = None
            return await handler(event, data)

        pool = data["pool"]  # dp["pool"] = cp_pool, đặt ở app/main.py
        try:
            user = await get_allowed_user(pool, event.from_user.id)
        except Exception:
            logger.exception("Không tra được người dùng trong database")
            return None

        if user is None:
            logger.info("Bỏ qua người dùng ngoài danh sách (telegram_id=%s)", event.from_user.id)
            return None

        data["user"] = user
        return await handler(event, data)


class CallbackAllowlistMiddleware(BaseMiddleware):
    """Như AllowlistMiddleware nhưng cho nút bấm: chỉ chat riêng, chỉ người có trong bảng users."""

    async def __call__(
        self,
        handler: Callable[[CallbackQuery, dict[str, Any]], Awaitable[Any]],
        event: CallbackQuery,
        data: dict[str, Any],
    ) -> Any:
        if event.message is None or event.message.chat.type != ChatType.PRIVATE:
            return None
        try:
            user = await get_allowed_user(data["pool"], event.from_user.id)
        except Exception:
            logger.exception("Không tra được người dùng trong database")
            return None
        if user is None:
            return None
        data["user"] = user
        return await handler(event, data)


# ---------------------------------------------------------------- hỏi đáp với agent

async def _ask(message: Message, user: AllowedUser, graph, pool, text: str) -> None:
    """Gửi một câu hỏi cho agent, ghi audit_log và trả lời người dùng."""
    uid = str(user.telegram_id)

    active = await peek_active(pool, uid)
    if active and is_busy(active):
        await message.answer(MSG_BUSY)
        return

    blocked = get_limiter().hit(uid)
    if blocked:
        await message.answer(blocked)
        return

    started = time.monotonic()
    async with progress(message):
        result = await run_turn(graph, pool, user_id=uid, role=user.role, message=text)

    await log_turn(
        pool,
        user_id=uid,
        role=user.role,
        question=text,
        status="ok" if result.ok else ("busy" if result.busy else "error"),
        duration_ms=int((time.monotonic() - started) * 1000),
        thread_id=result.thread or None,
    )

    if result.session_reset:
        await message.answer(
            "Cuộc trò chuyện trước đã kết thúc (hết hạn hoặc bị lỗi) nên mình bắt đầu cuộc mới."
        )
    answer = result.answer
    tag = _report_tag(result.thread) if result.ok else None
    if tag:  # đang hỏi tiếp trong thread báo cáo: nhắc để không nhầm với phiên chat thường
        answer = f"{tag}\n\n{answer}"
    await send_text(message, answer)


async def cmd_myid(message: Message) -> None:
    await message.answer(f"Telegram ID của bạn: {message.from_user.id}")


async def cmd_start(message: Message, user: AllowedUser) -> None:
    text = f"Xin chào {user.name or ''}!\n\n{HELP_TEXT}".replace("Xin chào !", "Xin chào!")
    text += _manager_help(user)
    if _can_grant(user):
        text += _grant_help()
    if user.is_admin:
        text += ADMIN_HELP_TEXT
    await message.answer(text)


async def cmd_help(message: Message, user: AllowedUser) -> None:
    text = (
        HELP_TEXT
        + _manager_help(user)
        + (_grant_help() if _can_grant(user) else "")
        + (ADMIN_HELP_TEXT if user.is_admin else "")
    )
    await message.answer(text)


async def cmd_new(message: Message, user: AllowedUser, graph, pool) -> None:
    uid = str(user.telegram_id)
    active = await peek_active(pool, uid)
    if active and is_busy(active):
        await message.answer("Mình đang xử lý câu trước, đợi xong rồi gõ /new nhé.")
        return
    try:
        # Phiên hiện tại còn trống thì dùng luôn, tránh gõ /new nhiều lần đẩy phiên thật ra ngoài
        if await reuse_empty_active(pool, uid):
            await message.answer("Bạn đang ở một cuộc trò chuyện mới rồi, cứ hỏi nhé.")
            return
        _, evicted = await create_session(graph, pool, uid)
    except Exception:
        logger.exception("Không tạo được phiên mới (user_id=%s)", uid)
        await message.answer("Chưa tạo được cuộc trò chuyện mới, bạn thử lại sau nhé.")
        return
    text = "Đã bắt đầu cuộc trò chuyện mới. Bạn muốn xem gì?"
    if evicted:
        text += (
            f"\n(Đã xoá cuộc ít dùng nhất do chỉ giữ tối đa {get_settings().max_sessions_per_user} cuộc. "
            "Gõ /sessions để xem các cuộc còn lại.)"
        )
    await message.answer(text)


async def _scopes_of(user: AllowedUser) -> set[str]:
    """Khối dữ liệu người dùng đang được xem (role + khối cấp riêng còn hạn). Lỗi đọc -> tập rỗng."""
    return await user_scopes(user.role, user.telegram_id)


async def _shortcut(message: Message, user: AllowedUser, graph, pool, prompt: str,
                    needs_db: bool = False) -> None:
    """/tuan /thang /dashboard cho mọi role. Chưa có khối nào thì báo ngay, không tốn lượt gọi LLM."""
    allowed = await _scopes_of(user)
    if not allowed:
        await message.answer(MSG_NO_SCOPE.format(scopes=_scope_list()))
        return
    if needs_db:
        db_scopes = load_scopes_config().db_scopes()
        if not allowed & set(db_scopes):
            await message.answer(MSG_DASHBOARD_NEEDS_DB.format(db=", ".join(db_scopes)))
            return
    await _ask(message, user, graph, pool, prompt)


async def cmd_week(message: Message, user: AllowedUser, graph, pool) -> None:
    await _shortcut(message, user, graph, pool, PROMPT_WEEK)


async def cmd_month(message: Message, user: AllowedUser, graph, pool) -> None:
    await _shortcut(message, user, graph, pool, PROMPT_MONTH)


async def cmd_dashboard(message: Message, user: AllowedUser, graph, pool) -> None:
    await _shortcut(message, user, graph, pool, PROMPT_DASHBOARD, needs_db=True)


# ---------------------------------------------------------------- báo cáo sáng (chỉ manager)
# Nội dung báo cáo do admin cấu hình trong config/reports.yaml. Thread báo cáo (kind='report') không
# tính vào hạn mức phiên; /reply chỉ chuyển phiên đang dùng sang thread đó.

_REPORT_THREAD_RE = re.compile(r":report:(\d{4})(\d{2})(\d{2})$")
_HHMM_RE = re.compile(r"^(\d{1,2})(?:[:h](\d{2})?)?$")


def _report_tag(thread: str | None) -> str | None:
    m = _REPORT_THREAD_RE.search(thread or "")
    return f"📊 Đang hỏi tiếp báo cáo ngày {m[3]}/{m[2]}" if m else None


def _parse_hhmm(text: str) -> dtime | None:
    """'07:30', '7:30', '7h30', '7h' -> time; không hiểu thì None."""
    m = _HHMM_RE.match(text.strip().lower())
    if not m:
        return None
    hour, minute = int(m[1]), int(m[2] or 0)
    return dtime(hour, minute) if hour < 24 and minute < 60 else None


def _report_entry(user: AllowedUser):
    """(cấu hình, mục của người này trong reports.yaml); phần nào không có thì None."""
    cfg = get_store().get()
    return cfg, (cfg.managers.get(user.telegram_id) if cfg is not None else None)


async def cmd_reply(message: Message, command: CommandObject, user: AllowedUser, graph, pool) -> None:
    """/reply: chuyển sang thread báo cáo gần nhất. /reply <câu hỏi>: chuyển rồi hỏi luôn."""
    if not _is_report_user(user):
        await message.answer(MSG_NOT_MANAGER)
        return
    uid = str(user.telegram_id)
    report = await latest_report_thread(pool, uid)
    if report is None or not await switch_session(pool, uid, report["thread_id"]):
        await message.answer(
            f"Chưa có báo cáo nào còn hiệu lực (báo cáo được giữ {REPORT_RETENTION_DAYS} ngày). "
            "Bạn cứ hỏi tự nhiên, mình trả lời trong cuộc trò chuyện hiện tại."
        )
        return
    question = (command.args or "").strip()
    if not question:
        await message.answer(
            f"Đã chuyển sang {report['title']}. Bạn cứ hỏi tiếp về báo cáo này, "
            "ví dụ: gửi dashboard doanh thu theo kênh.\n"
            "Gõ /new để quay lại cuộc trò chuyện thường."
        )
        return
    await _ask(message, user, graph, pool, question)


async def cmd_baocao(message: Message, user: AllowedUser, pool) -> None:
    """Xem trạng thái báo cáo sáng của chính mình."""
    if not _is_report_user(user):
        await message.answer(MSG_NOT_MANAGER)
        return
    cfg, manager = _report_entry(user)
    if cfg is None:
        await message.answer("Báo cáo tự động chưa được cấu hình.")
        return
    if manager is None:
        await message.answer(MSG_NOT_REGISTERED)
        return
    state = (await load_states(pool)).get(user.telegram_id)
    if not manager.enabled:
        status = "admin đang tắt"
    elif state is not None and state.paused:
        status = "bạn đang tạm dừng (gõ /baocao_bat để bật lại)"
    else:
        status = "đang bật"
    send_time = effective_send_time(cfg, manager, state)
    titles = ", ".join(SECTIONS[s].title if s in SECTIONS else s for s in manager.sections)
    last = state.last_sent_on.strftime("%d/%m/%Y") if state and state.last_sent_on else "chưa gửi lần nào"
    lines = [
        "📬 Báo cáo tự động hằng ngày",
        f"• Trạng thái: {status}",
        f"• Giờ gửi: {send_time:%H:%M} (giờ Việt Nam)"
        + ("" if cfg.manager_can_change_time else ", do admin đặt"),
        f"• Nội dung: {titles}",
        f"• Lần gửi gần nhất: {last}",
        "",
        "Số liệu là của ngày hôm qua, so với hôm trước và cùng ngày tuần trước.",
        "Lệnh: /baocao_tat, /baocao_bat, /reply"
        + (", /baocao_gio 07:30" if cfg.manager_can_change_time else ""),
    ]
    await message.answer("\n".join(lines))


async def _set_pause(message: Message, user: AllowedUser, pool, paused: bool) -> None:
    if not _is_report_user(user):
        await message.answer(MSG_NOT_MANAGER)
        return
    _, manager = _report_entry(user)
    if manager is None:
        await message.answer(MSG_NOT_REGISTERED)
        return
    if not paused and not manager.enabled:
        await message.answer("Admin đang tắt báo cáo của bạn nên chưa bật lại được. Liên hệ admin nhé.")
        return
    await set_paused(pool, user.telegram_id, paused)
    await message.answer(
        "Đã tạm dừng báo cáo sáng. Gõ /baocao_bat khi muốn nhận lại."
        if paused
        else "Đã bật lại báo cáo sáng."
    )


async def cmd_baocao_tat(message: Message, user: AllowedUser, pool) -> None:
    await _set_pause(message, user, pool, True)


async def cmd_baocao_bat(message: Message, user: AllowedUser, pool) -> None:
    await _set_pause(message, user, pool, False)


async def cmd_baocao_gio(message: Message, command: CommandObject, user: AllowedUser, pool) -> None:
    """Manager tự đổi giờ nhận báo cáo; chỉ hoạt động khi admin bật manager_can_change_time."""
    if not _is_report_user(user):
        await message.answer(MSG_NOT_MANAGER)
        return
    cfg, manager = _report_entry(user)
    if cfg is None or manager is None:
        await message.answer(MSG_NOT_REGISTERED)
        return
    if not cfg.manager_can_change_time:
        await message.answer(
            f"Giờ nhận báo cáo do admin đặt (hiện là {manager.send_time:%H:%M}). "
            "Liên hệ admin nếu muốn đổi."
        )
        return
    value = _parse_hhmm(command.args or "")
    if value is None:
        await message.answer(
            f"Cách dùng: /baocao_gio 07:30 (chọn giờ từ {cfg.manager_time_min:%H:%M} "
            f"đến {cfg.manager_time_max:%H:%M})."
        )
        return
    try:
        await set_send_time(pool, cfg, user.telegram_id, value)
    except ValueError as e:
        await message.answer(str(e))
        return
    await message.answer(f"Đã đổi giờ nhận báo cáo thành {value:%H:%M}, áp dụng từ lần gửi tiếp theo.")


# ---------------------------------------------------------------- quản lý phiên (/sessions)

def _fmt(dt: datetime) -> str:
    return dt.astimezone(ZoneInfo(get_settings().timezone)).strftime("%d/%m %H:%M")


def _render_sessions(rows: list[dict]) -> tuple[str, InlineKeyboardMarkup | None]:
    if not rows:
        return "Bạn chưa có cuộc trò chuyện nào. Cứ nhắn câu hỏi hoặc gõ /new.", None
    lines = [
        f"Cuộc trò chuyện của bạn (tối đa {get_settings().max_sessions_per_user}), "
        "▶ là cuộc đang dùng:"
    ]
    keyboard = []
    for i, r in enumerate(rows, 1):
        title = r["title"] or "(chưa có nội dung)"
        lines.append(
            f"\n{i}. {'▶ ' if r['is_active'] else ''}{title}\n"
            f"   dùng lần cuối {_fmt(r['last_active'])} · hết hạn {_fmt(session_expires_at(r))}"
        )
        keyboard.append([
            InlineKeyboardButton(
                text=f"{'✅ ' if r['is_active'] else ''}{i}. {title[:25]}",
                callback_data=f"sw:{r['thread_id']}",
            ),
            InlineKeyboardButton(text="🗑", callback_data=f"del:{r['thread_id']}"),
        ])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=keyboard)


async def cmd_sessions(message: Message, user: AllowedUser, pool) -> None:
    text, markup = _render_sessions(await list_sessions(pool, user.telegram_id))
    await message.answer(text, reply_markup=markup)


async def _refresh_sessions(cb: CallbackQuery, pool, user_id: int) -> None:
    text, markup = _render_sessions(await list_sessions(pool, user_id))
    try:
        await cb.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest:
        pass  # nội dung không đổi


async def on_session_button(cb: CallbackQuery, user: AllowedUser, graph, pool) -> None:
    """Nút trong /sessions: sw = chuyển, del = hỏi xác nhận xoá, cf = xoá, bk = quay lại."""
    action, _, thread = (cb.data or "").partition(":")
    uid = user.telegram_id

    if action == "sw":
        if await switch_session(pool, uid, thread):
            await cb.answer("Đã chuyển cuộc trò chuyện.")
        else:
            await cb.answer("Cuộc trò chuyện này không còn tồn tại.", show_alert=True)
        await _refresh_sessions(cb, pool, uid)

    elif action == "del":
        row = next((r for r in await list_sessions(pool, uid) if r["thread_id"] == thread), None)
        if row is None:
            await cb.answer("Cuộc trò chuyện này không còn tồn tại.", show_alert=True)
            await _refresh_sessions(cb, pool, uid)
            return
        confirm = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Xoá", callback_data=f"cf:{thread}"),
            InlineKeyboardButton(text="Huỷ", callback_data="bk:"),
        ]])
        title = row["title"] or "(chưa có nội dung)"
        try:
            await cb.message.edit_text(f"Xoá cuộc trò chuyện “{title}”?", reply_markup=confirm)
        except TelegramBadRequest:
            pass
        await cb.answer()

    elif action == "cf":
        status = await delete_own_session(graph, pool, uid, thread)
        notice = {
            "ok": "Đã xoá cuộc trò chuyện.",
            "busy": "Cuộc này đang xử lý câu hỏi, thử lại sau.",
            "not_found": "Cuộc trò chuyện này không còn tồn tại.",
        }[status]
        await cb.answer(notice, show_alert=status != "ok")
        await _refresh_sessions(cb, pool, uid)

    else:  # "bk"
        await cb.answer()
        await _refresh_sessions(cb, pool, uid)


# ---------------------------------------------------------------- lệnh quản trị
# Chỉ admin (cấp 1) và owner (cấp 2) dùng được. Người thường gõ thì im lặng.

async def _add(message: Message, command: CommandObject, user: AllowedUser, pool,
               level: int | None, usage: str) -> None:
    if not user.is_admin:
        return
    parts = (command.args or "").split(maxsplit=2)
    if len(parts) < 2 or not parts[0].isdigit():
        await message.answer(usage)
        return
    tid, role = int(parts[0]), parts[1].lower()
    name = parts[2] if len(parts) > 2 else None
    if not valid_role(role):
        await message.answer("Role không hợp lệ. Các role: " + ", ".join(sorted(ROLE_TABLES)))
        return
    err = await check_manage(pool, user, tid, role, level)
    if err:
        await message.answer(err)
        return
    await upsert_user(pool, tid, role, name, by=user.telegram_id, level=level)
    await log_change(pool, user.telegram_id, f"set {tid} role={role} level={level}")
    await message.answer(f"Đã lưu {tid}: role {role}" + (", cấp admin." if level == ADMIN else "."))


async def cmd_adduser(message: Message, command: CommandObject, user: AllowedUser, pool) -> None:
    await _add(message, command, user, pool, None, "Cú pháp: /adduser <telegram_id> <role> [tên]")


async def cmd_addadmin(message: Message, command: CommandObject, user: AllowedUser, pool) -> None:
    await _add(message, command, user, pool, ADMIN, "Cú pháp: /addadmin <telegram_id> <role> [tên]")


async def cmd_removeadmin(message: Message, command: CommandObject, user: AllowedUser, pool) -> None:
    if not user.is_admin:
        return
    arg = (command.args or "").strip()
    if not arg.isdigit():
        await message.answer("Cú pháp: /removeadmin <telegram_id>")
        return
    tid = int(arg)
    err = await check_manage(pool, user, tid, None, None)
    if err:
        await message.answer(err)
        return
    ok = await demote_admin(pool, tid)
    if ok:
        await log_change(pool, user.telegram_id, f"demote {tid}")
    await message.answer("Đã gỡ quyền admin." if ok else "Người này không phải admin.")


async def cmd_removeuser(message: Message, command: CommandObject, user: AllowedUser,
                         graph, pool) -> None:
    if not user.is_admin:
        return
    arg = (command.args or "").strip()
    if not arg.isdigit():
        await message.answer("Cú pháp: /removeuser <telegram_id>")
        return
    tid = int(arg)
    err = await check_manage(pool, user, tid, None, None)
    if err:
        await message.answer(err)
        return
    ok = await remove_user(pool, tid)
    if not ok:
        await message.answer("Không có người dùng này.")
        return
    try:
        await revoke_grant(str(tid), None)  # xoá luôn các khối dữ liệu đã cấp cho người này
    except Exception:
        logger.exception("Không xoá được quyền được cấp của %s", tid)
    # Người đã bị xoá thì dữ liệu hội thoại của họ cũng không nên nằm lại đến hết hạn
    try:
        purged = await purge_user_sessions(graph, pool, tid)
    except Exception:
        logger.exception("Không xoá hết phiên của người dùng %s", tid)
        purged = -1
    await log_change(pool, user.telegram_id, f"remove {tid} (xoá {purged} phiên)")
    await message.answer(
        "Đã xóa." if purged >= 0
        else "Đã xóa người dùng, nhưng chưa xoá hết hội thoại của họ (hệ thống sẽ tự dọn khi hết hạn)."
    )


async def cmd_users(message: Message, user: AllowedUser, pool) -> None:
    if not user.is_admin:
        return
    rows = await list_users(pool)
    lines = [
        f"{r['telegram_id']} | {r['role']}{_TAG.get(r['admin_level'], '')} | {r['name'] or ''}"
        for r in rows
    ]
    await message.answer("\n".join(lines) or "Chưa có người dùng nào.")


# ---------------------------------------------------------------- cấp quyền theo khối dữ liệu cho staff
# /capquyen, /thuhoi, /quyen <id>: chỉ boss/manager (người khác gõ thì im lặng). /quyen không tham số:
# ai cũng xem được khối của chính mình. Quyền lưu ở agent_state.scope_grants (app/agent/grants.py),
# có hiệu lực ngay ở lượt hỏi kế tiếp, không cần restart.

MAX_GRANT_HOURS = 24 * 365


async def cmd_capquyen(message: Message, command: CommandObject, user: AllowedUser, pool) -> None:
    if not _can_grant(user):
        return
    usage = (
        "Cú pháp: /capquyen <telegram_id> <khối> [số_giờ]\n"
        f"Khối: {_scope_list()}\n"
        "Ví dụ: /capquyen 123456789 coffee 48"
    )
    parts = (command.args or "").split()
    if len(parts) not in (2, 3) or not parts[0].isdigit():
        await message.answer(usage)
        return
    tid, scope = int(parts[0]), parts[1].lower()
    cfg = load_scopes_config()
    if scope not in cfg.scopes:
        await message.answer(f"Không có khối '{scope}'. Các khối: {_scope_list()}.")
        return
    hours = None
    if len(parts) == 3:
        if not parts[2].isdigit() or not 1 <= int(parts[2]) <= MAX_GRANT_HOURS:
            await message.answer(f"Số giờ phải từ 1 đến {MAX_GRANT_HOURS}. Bỏ trống nếu không giới hạn.")
            return
        hours = int(parts[2])

    target = await get_user(pool, tid)
    if target is None or (target["role"] or "").lower() != "staff":
        await message.answer(
            "Chỉ cấp quyền cho người dùng có role staff (boss/manager đã xem mọi khối theo vai trò). "
            "Thêm nhân viên bằng /adduser <id> staff [tên]."
        )
        return
    # Chống leo thang: chỉ cấp được khối mà chính người cấp đang xem được
    if scope not in await _scopes_of(user):
        await message.answer("Bạn không có quyền xem khối này nên không cấp được cho người khác.")
        return

    await add_grant(str(tid), scope, by=str(user.telegram_id), hours=hours)
    await log_change(pool, user.telegram_id, f"grant {tid} scope={scope} hours={hours}")
    await message.answer(
        f"Đã cấp cho {tid} quyền xem khối {scope} ({cfg.scopes[scope].title})"
        + (f" trong {hours} giờ." if hours else ", không giới hạn thời gian.")
    )


async def cmd_thuhoi(message: Message, command: CommandObject, user: AllowedUser, pool) -> None:
    if not _can_grant(user):
        return
    parts = (command.args or "").split()
    if len(parts) != 2 or not parts[0].isdigit():
        await message.answer(f"Cú pháp: /thuhoi <telegram_id> <khối|all>\nKhối: {_scope_list()}")
        return
    tid, scope = int(parts[0]), parts[1].lower()
    # Boss và manager đều thu hồi được mọi khối của mọi nhân viên, bất kể ai đã cấp
    n = await revoke_grant(str(tid), None if scope == "all" else scope)
    if n:
        await log_change(pool, user.telegram_id, f"revoke {tid} scope={scope} ({n} quyền)")
        await message.answer(f"Đã thu hồi {n} quyền của {tid}.")
    else:
        await message.answer("Không có quyền nào khớp để thu hồi.")


async def cmd_quyen(message: Message, command: CommandObject, user: AllowedUser, pool) -> None:
    """Không tham số: khối của chính mình. /quyen <id>: boss/manager xem của người khác."""
    arg = (command.args or "").strip()
    if arg:
        if not _can_grant(user):
            return
        if not arg.isdigit():
            await message.answer("Cú pháp: /quyen [telegram_id]")
            return
        tid = int(arg)
        target = await get_user(pool, tid)
        if target is None:
            await message.answer("Không có người dùng này.")
            return
        role, who = (target["role"] or ""), f"{tid} ({target['name'] or 'chưa có tên'})"
    else:
        tid, role, who = user.telegram_id, (user.role or ""), "Bạn"

    allowed = await user_scopes(role, tid)
    if role.lower() != "staff":
        await message.answer(f"{who} - role {role}, xem theo vai trò: {describe_scopes(allowed)}")
        return
    rows = await list_grants(str(tid))
    if not rows:
        await message.answer(f"{who} chưa được cấp khối dữ liệu nào.")
        return
    titles = load_scopes_config().scopes
    lines = [
        f"• {r['scope']}"
        + (f" - {titles[r['scope']].title}" if r["scope"] in titles else " (khối không còn trong cấu hình)")
        + (f" (hết hạn {_fmt(r['expires_at'])})" if r["expires_at"] else " (không giới hạn)")
        for r in rows
    ]
    await message.answer(f"{who} được xem các khối:\n" + "\n".join(lines))


# ---------------------------------------------------------------- tin còn lại

async def on_unknown_command(message: Message, user: AllowedUser) -> None:
    await message.answer("Mình chưa có lệnh này. Gõ /help để xem hướng dẫn.")


async def on_text(message: Message, user: AllowedUser, graph, pool) -> None:
    await _ask(message, user, graph, pool, message.text)


async def on_other(message: Message, user: AllowedUser) -> None:
    await message.answer("Mình chỉ đọc tin nhắn dạng chữ thôi nhé.")


def build_router() -> Router:
    """Tạo router mới (thứ tự đăng ký quan trọng: lệnh trước, tin thường sau)."""
    router = Router(name="telegram-main")
    router.message.outer_middleware(AllowlistMiddleware())
    router.callback_query.outer_middleware(CallbackAllowlistMiddleware())
    router.message.register(cmd_myid, Command("myid"))
    router.message.register(cmd_start, CommandStart())
    router.message.register(cmd_help, Command("help"))
    router.message.register(cmd_new, Command("new"))
    router.message.register(cmd_sessions, Command("sessions"))
    router.message.register(cmd_week, Command("tuan"))
    router.message.register(cmd_month, Command("thang"))
    router.message.register(cmd_dashboard, Command("dashboard"))
    # Báo cáo sáng (chỉ manager; người khác gõ thì được nhắc là lệnh không dành cho họ)
    router.message.register(cmd_reply, Command("reply"))
    router.message.register(cmd_baocao, Command("baocao"))
    router.message.register(cmd_baocao_tat, Command("baocao_tat"))
    router.message.register(cmd_baocao_bat, Command("baocao_bat"))
    router.message.register(cmd_baocao_gio, Command("baocao_gio"))
    # Lệnh quản trị (người thường gõ thì không có phản hồi)
    router.message.register(cmd_adduser, Command("adduser"))
    router.message.register(cmd_addadmin, Command("addadmin"))
    router.message.register(cmd_removeadmin, Command("removeadmin"))
    router.message.register(cmd_removeuser, Command("removeuser"))
    router.message.register(cmd_users, Command("users"))
    # Cấp quyền theo khối dữ liệu cho staff (boss/manager; /quyen ai cũng xem được quyền của mình)
    router.message.register(cmd_capquyen, Command("capquyen"))
    router.message.register(cmd_thuhoi, Command("thuhoi"))
    router.message.register(cmd_quyen, Command("quyen"))
    router.message.register(on_unknown_command, F.text.startswith("/"))
    router.message.register(on_text, F.text)
    router.message.register(on_other)
    # Nút bấm trong /sessions
    router.callback_query.register(
        on_session_button, F.data.startswith(("sw:", "del:", "cf:", "bk:"))
    )
    return router