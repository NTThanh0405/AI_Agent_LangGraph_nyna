# app/reports/subscriptions.py
# Cấu hình và trạng thái của báo cáo tự động gửi cho manager.
#   - Cấu hình (ai nhận, mục nào, giờ nào): file config/reports.yaml do admin sửa, tự nạp lại
#     khi file đổi (ConfigStore). File sai thì giữ cấu hình cũ, không làm sập service.
#   - Trạng thái (hôm nay đã gửi chưa, đã bỏ lỡ chưa, manager có đổi giờ/tạm dừng không): bảng
#     agent_state.report_state trong database agent_app. Ghi ngày đã gửi vào DB nên restart
#     giữa chừng vẫn gửi bù được mà không bao giờ gửi trùng trong ngày.
# `pool` ở các hàm DB là pool tới database agent_app (row_factory=dict_row, như sessions.py).

import logging
import os
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Iterable, Optional
from zoneinfo import ZoneInfo

import yaml

logger = logging.getLogger(__name__)

CONFIG_PATH = Path(
    os.environ.get("REPORTS_CONFIG")
    or Path(__file__).resolve().parents[2] / "config" / "reports.yaml"
)


REPORT_ROLE = "manager"  # chỉ role này nhận báo cáo tự động và được dùng /reply, /baocao*


class ConfigError(ValueError):
    """File reports.yaml sai nội dung."""


@dataclass(frozen=True)
class ManagerReport:
    user_id: int
    name: str
    send_time: time
    sections: tuple[str, ...]
    enabled: bool = True


@dataclass(frozen=True)
class ReportConfig:
    default_send_time: time
    max_delay_minutes: int
    manager_can_change_time: bool
    manager_time_min: time
    manager_time_max: time
    footer: str
    managers: dict[int, ManagerReport]
    timezone: str = "Asia/Ho_Chi_Minh"
    retry_on_error: int = 2
    commentary_enabled: bool = False


@dataclass(frozen=True)
class ReportState:
    last_sent_on: Optional[date] = None
    send_time: Optional[time] = None  # giờ manager tự chọn (chỉ có tác dụng nếu cho phép)
    paused: bool = False
    last_skipped_on: Optional[date] = None  # ngày đã ghi nhận "bỏ lỡ vì quá muộn" (chỉ ghi 1 lần/ngày)


def now_in_tz(cfg: ReportConfig) -> datetime:
    """Giờ hiện tại theo múi giờ trong reports.yaml (có tzinfo). scheduler.py dùng hàm này."""
    return datetime.now(ZoneInfo(cfg.timezone))


# ---------------------------------------------------------------- đọc reports.yaml

def parse_time(value, where: str) -> time:
    if isinstance(value, bool):
        raise ConfigError(f"{where}: giờ không hợp lệ ({value!r})")
    if isinstance(value, int):
        # YAML đọc 10:30 (không có ngoặc kép) thành 630 (phút); đổi ngược lại thành 10:30
        hour, minute = divmod(value, 60)
    elif isinstance(value, str):
        m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", value)
        if not m:
            raise ConfigError(f'{where}: giờ "{value}" sai định dạng, cần HH:MM, ví dụ "07:00"')
        hour, minute = int(m[1]), int(m[2])
    else:
        raise ConfigError(f"{where}: giờ không hợp lệ ({value!r})")
    if not (0 <= hour < 24 and 0 <= minute < 60):
        raise ConfigError(f"{where}: giờ {hour:02d}:{minute:02d} không hợp lệ")
    return time(hour, minute)


def _parse_sections(value, where: str, known: Optional[Iterable[str]]) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or not all(isinstance(s, str) for s in value):
        raise ConfigError(f"{where}: sections phải là danh sách tên mục, không được rỗng")
    sections = tuple(dict.fromkeys(s.strip() for s in value))  # bỏ trùng, giữ thứ tự
    if known is not None:
        unknown = [s for s in sections if s not in set(known)]
        if unknown:
            raise ConfigError(
                f"{where}: mục không có: {', '.join(unknown)}. "
                f"Các mục hợp lệ: {', '.join(sorted(known))}"
            )
    return sections


def load_config(path: Path = CONFIG_PATH, known_sections: Optional[Iterable[str]] = None) -> ReportConfig:
    """Đọc và kiểm tra reports.yaml. Raise ConfigError nếu có lỗi (message tiếng Việt)."""
    known = list(known_sections) if known_sections is not None else None
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8-sig")) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"YAML sai cú pháp: {e}") from e
    if not isinstance(raw, dict):
        raise ConfigError("Nội dung file phải là dạng 'khoá: giá trị'")

    default_time = parse_time(raw.get("default_send_time", "07:00"), "default_send_time")

    try:
        max_delay = int(raw.get("max_delay_minutes", 180))
    except (TypeError, ValueError):
        raise ConfigError("max_delay_minutes phải là số phút") from None
    if not 1 <= max_delay <= 720:
        raise ConfigError("max_delay_minutes phải từ 1 đến 720")

    rng = raw.get("manager_time_range", ["06:00", "10:00"])
    if not isinstance(rng, list) or len(rng) != 2:
        raise ConfigError('manager_time_range phải có đúng 2 giá trị, ví dụ ["06:00", "10:00"]')
    t_min = parse_time(rng[0], "manager_time_range[0]")
    t_max = parse_time(rng[1], "manager_time_range[1]")
    if t_min > t_max:
        raise ConfigError("manager_time_range: giờ đầu phải trước giờ cuối")

    default_sections = raw.get("default_sections")
    if default_sections is not None:
        default_sections = _parse_sections(default_sections, "default_sections", known)

    raw_managers = raw.get("managers") or {}
    if not isinstance(raw_managers, dict):
        raise ConfigError("managers phải là danh sách 'telegram_id: {...}'")

    managers: dict[int, ManagerReport] = {}
    for key, item in raw_managers.items():
        where = f"managers.{key}"
        if isinstance(key, bool) or not isinstance(key, int):
            raise ConfigError(f"{where}: khoá phải là Telegram ID dạng số")
        item = item or {}
        if not isinstance(item, dict):
            raise ConfigError(f"{where}: phải là khối có name/send_time/sections")
        if item.get("sections") is not None:
            sections = _parse_sections(item["sections"], f"{where}.sections", known)
        elif default_sections is not None:
            sections = default_sections  # đã kiểm tra ở trên
        else:
            raise ConfigError(f"{where}: thiếu sections và cũng không có default_sections")
        send_time = (
            parse_time(item["send_time"], f"{where}.send_time")
            if item.get("send_time") is not None
            else default_time
        )
        managers[key] = ManagerReport(
            user_id=key,
            name=str(item.get("name") or ""),
            send_time=send_time,
            sections=sections,
            enabled=bool(item.get("enabled", True)),
        )

    tz_name = str(raw.get("timezone") or "Asia/Ho_Chi_Minh")
    try:
        ZoneInfo(tz_name)
    except Exception:
        raise ConfigError(f'timezone "{tz_name}" không hợp lệ, ví dụ "Asia/Ho_Chi_Minh"') from None
    try:
        retry = int(raw.get("retry_on_error", 2))
    except (TypeError, ValueError):
        raise ConfigError("retry_on_error phải là số lần thử lại") from None
    if not 0 <= retry <= 5:
        raise ConfigError("retry_on_error phải từ 0 đến 5")

    return ReportConfig(
        default_send_time=default_time,
        max_delay_minutes=max_delay,
        manager_can_change_time=bool(raw.get("manager_can_change_time", False)),
        manager_time_min=t_min,
        manager_time_max=t_max,
        footer=str(raw.get("footer") or "").strip(),
        managers=managers,
        timezone=tz_name,
        retry_on_error=retry,
        commentary_enabled=bool(raw.get("commentary_enabled", False)),
    )


class ConfigStore:
    """Giữ cấu hình trong RAM và tự nạp lại khi file đổi. File lỗi thì giữ bản cũ."""

    def __init__(self, path: Path = CONFIG_PATH, known_sections: Optional[Iterable[str]] = None):
        self.path = Path(path)
        self.known_sections = list(known_sections) if known_sections is not None else None
        self._cfg: Optional[ReportConfig] = None
        self._mtime: Optional[float] = None

    def get(self) -> Optional[ReportConfig]:
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            if self._mtime != -1:  # chỉ ghi log một lần, không lặp mỗi phút
                logger.error("Không đọc được %s: báo cáo tự động tạm dừng", self.path)
                self._mtime = -1
            return self._cfg
        if mtime != self._mtime:
            self._mtime = mtime  # đặt trước để file lỗi không bị báo lại mỗi phút
            try:
                self._cfg = load_config(self.path, self.known_sections)
                logger.info("Đã nạp %s: %d manager nhận báo cáo", self.path.name, len(self._cfg.managers))
            except ConfigError as e:
                logger.error("%s sai, giữ cấu hình cũ: %s", self.path.name, e)
        return self._cfg


_store: Optional[ConfigStore] = None


def get_store() -> ConfigStore:
    """ConfigStore dùng chung cho cả vòng gửi báo cáo (main.py) lẫn các lệnh /baocao (handlers.py)."""
    global _store
    if _store is None:
        from app.reports.builder import SECTIONS  # import tại chỗ để tránh import vòng

        _store = ConfigStore(known_sections=SECTIONS)
    return _store


# ---------------------------------------------------------------- đến giờ gửi chưa

@dataclass(frozen=True)
class DueReport:
    manager: ManagerReport
    send_time: time


def effective_send_time(cfg: ReportConfig, manager: ManagerReport, state: Optional[ReportState]) -> time:
    """Giờ gửi thực tế: giờ manager tự chọn nếu được phép, không thì giờ admin đặt trong yaml."""
    if cfg.manager_can_change_time and state is not None and state.send_time is not None:
        return state.send_time
    return manager.send_time


def due_reports(cfg: ReportConfig, states: dict[int, ReportState], now: datetime) -> list[DueReport]:
    """Những manager cần gửi báo cáo ngay lúc `now` (now phải có múi giờ).

    Điều kiện: đang bật, không tạm dừng, hôm nay chưa gửi, và giờ hiện tại nằm trong
    [giờ hẹn, giờ hẹn + max_delay_minutes]. Nhờ vậy restart lúc 6h59, lên lại 7h02 vẫn gửi bù.
    """
    today = now.date()
    out = []
    for manager in cfg.managers.values():
        state = states.get(manager.user_id)
        if not manager.enabled or (state is not None and state.paused):
            continue
        if state is not None and (state.last_sent_on == today or state.last_skipped_on == today):
            continue  # đã gửi, hoặc đã bỏ cuộc/bỏ lỡ hôm nay (restart cũng không thử lại)
        send_time = effective_send_time(cfg, manager, state)
        scheduled = datetime.combine(today, send_time, tzinfo=now.tzinfo)
        if scheduled <= now <= scheduled + timedelta(minutes=cfg.max_delay_minutes):
            out.append(DueReport(manager, send_time))
    return out


def late_reports(cfg: ReportConfig, states: dict[int, ReportState], now: datetime) -> list[ManagerReport]:
    """Manager đã quá giờ hẹn + max_delay_minutes mà hôm nay chưa gửi và chưa ghi nhận bỏ lỡ.

    scheduler.py gọi mark_skipped() + ghi audit (status = skipped_late) cho từng người trả về,
    nhờ cột last_skipped_on nên mỗi người chỉ bị ghi đúng 1 lần/ngày dù vòng nền chạy mỗi phút.
    """
    today = now.date()
    out = []
    for manager in cfg.managers.values():
        state = states.get(manager.user_id)
        if not manager.enabled or (state is not None and state.paused):
            continue
        if state is not None and (state.last_sent_on == today or state.last_skipped_on == today):
            continue
        send_time = effective_send_time(cfg, manager, state)
        deadline = datetime.combine(today, send_time, tzinfo=now.tzinfo) + timedelta(
            minutes=cfg.max_delay_minutes
        )
        if now > deadline:
            out.append(manager)
    return out


# ---------------------------------------------------------------- bảng trạng thái (PostgreSQL)

_SCHEMA_SQL = [
    "CREATE TABLE IF NOT EXISTS agent_state.report_state ("
    " user_id BIGINT PRIMARY KEY,"
    " last_sent_on DATE,"
    " send_time TIME,"
    " paused BOOLEAN NOT NULL DEFAULT FALSE,"
    " updated_at TIMESTAMPTZ NOT NULL DEFAULT now())",
    "ALTER TABLE agent_state.report_state ADD COLUMN IF NOT EXISTS last_skipped_on DATE",
    # Phân biệt phiên chat thường (tính vào hạn mức) với thread báo cáo (không tính)
    "ALTER TABLE agent_state.chat_sessions ADD COLUMN IF NOT EXISTS kind TEXT NOT NULL DEFAULT 'chat'",
]


async def ensure_schema(pool) -> None:
    """Tạo bảng/cột cần cho báo cáo nếu chưa có. Gọi một lần khi khởi động, trước khi dùng phiên."""
    async with pool.connection() as conn:
        for statement in _SCHEMA_SQL:
            await conn.execute(statement)


async def load_states(pool) -> dict[int, ReportState]:
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT user_id, last_sent_on, send_time, paused, last_skipped_on "
            "FROM agent_state.report_state"
        )
        rows = await cur.fetchall()
    return {
        int(r["user_id"]): ReportState(
            r["last_sent_on"], r["send_time"], bool(r["paused"]), r["last_skipped_on"]
        )
        for r in rows
    }


async def mark_sent(pool, user_id: int, day: date) -> None:
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO agent_state.report_state (user_id, last_sent_on) VALUES (%s, %s) "
            "ON CONFLICT (user_id) DO UPDATE SET last_sent_on = EXCLUDED.last_sent_on, updated_at = now()",
            (user_id, day),
        )


async def mark_skipped(pool, user_id: int, day: date) -> None:
    """Ghi nhận hôm nay bỏ lỡ báo cáo vì quá max_delay_minutes (để audit chỉ ghi 1 lần/ngày)."""
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO agent_state.report_state (user_id, last_skipped_on) VALUES (%s, %s) "
            "ON CONFLICT (user_id) DO UPDATE SET last_skipped_on = EXCLUDED.last_skipped_on, updated_at = now()",
            (user_id, day),
        )


async def set_paused(pool, user_id: int, paused: bool) -> None:
    """Manager tắt/bật báo cáo sáng của mình (dành cho lệnh /baocao_tat, /baocao_bat sau này)."""
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO agent_state.report_state (user_id, paused) VALUES (%s, %s) "
            "ON CONFLICT (user_id) DO UPDATE SET paused = EXCLUDED.paused, updated_at = now()",
            (user_id, paused),
        )


async def set_send_time(pool, cfg: ReportConfig, user_id: int, value: time) -> None:
    """Manager tự đổi giờ nhận (dành cho lệnh /baocao_gio sau này).

    Raise ValueError nếu admin chưa cho phép hoặc giờ ngoài khoảng cho phép."""
    if not cfg.manager_can_change_time:
        raise ValueError("Admin chưa cho phép manager tự đổi giờ nhận báo cáo.")
    if user_id not in cfg.managers:
        raise ValueError("Bạn chưa được đăng ký nhận báo cáo tự động.")
    if not cfg.manager_time_min <= value <= cfg.manager_time_max:
        raise ValueError(
            f"Chỉ chọn được giờ từ {cfg.manager_time_min:%H:%M} đến {cfg.manager_time_max:%H:%M}."
        )
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO agent_state.report_state (user_id, send_time) VALUES (%s, %s) "
            "ON CONFLICT (user_id) DO UPDATE SET send_time = EXCLUDED.send_time, updated_at = now()",
            (user_id, value),
        )