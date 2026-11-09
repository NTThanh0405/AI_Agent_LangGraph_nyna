# app/agent/periods.py
# Đổi cụm thời gian tiếng Việt ("hôm qua", "tuần này", "tháng 10/2025", "từ 1/11 đến 5/11"...)
# thành khoảng ngày cụ thể theo giờ Việt Nam (UTC+7), và tính kỳ so sánh (kỳ trước, cùng ngày
# tuần trước). Là code thuần (không gọi DB/LLM) để dùng chung cho:
#   - tool get_date_range của agent (app/agent/tools/period_tools.py)
#   - báo cáo tự động 7h sáng (app/reports/...), để "hôm qua" luôn cùng một định nghĩa.
#
# Quy ước:
#   - Khoảng ngày [start, end] gồm cả hai đầu; end_exclusive dùng cho SQL (col >= start AND col < end_exclusive).
#   - Tuần bắt đầu từ thứ Hai.
#   - Kỳ chưa kết thúc (tuần này, tháng này...) chỉ lấy đến hôm nay, và đánh dấu partial=True.
#   - "N ngày qua" là N ngày đã đủ, kết thúc ở HÔM QUA (không gồm hôm nay).
#   - Ngày gõ thiếu năm (3/11): lấy năm gần nhất mà ngày đó không ở tương lai.

import calendar
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

DEFAULT_TZ = "Asia/Ho_Chi_Minh"
MAX_RANGE_DAYS = 731  # khoảng ngày tối đa (khoảng 2 năm)

_WEEKDAYS = ["Thứ Hai", "Thứ Ba", "Thứ Tư", "Thứ Năm", "Thứ Sáu", "Thứ Bảy", "Chủ nhật"]


class PeriodError(ValueError):
    """Cụm thời gian không hiểu được, không hợp lệ hoặc ở tương lai."""


@dataclass(frozen=True)
class Period:
    start: date          # ngày đầu (gồm)
    end: date            # ngày cuối (gồm)
    kind: str            # day | week | month | quarter | year | days | range
    label: str           # mô tả tiếng Việt, ví dụ "tháng 11/2026 (từ 01/11/2026 đến 06/11/2026)"
    partial: bool = False  # True nếu kỳ gồm cả hôm nay -> số liệu chưa đủ ngày

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    @property
    def end_exclusive(self) -> date:
        return self.end + timedelta(days=1)


# ---------------------------------------------------------------- tiện ích ngày

def _tz() -> ZoneInfo:
    """Múi giờ của hệ thống (đọc từ settings nếu có, không thì UTC+7)."""
    tz_name = DEFAULT_TZ
    try:
        from app.config import get_settings  # import tại chỗ để test không cần .env

        tz_name = getattr(get_settings(), "timezone", DEFAULT_TZ) or DEFAULT_TZ
    except Exception:
        pass
    return ZoneInfo(tz_name)


def now_local() -> datetime:
    """Giờ hiện tại theo giờ Việt Nam (có múi giờ), dùng cho bộ lập lịch báo cáo."""
    return datetime.now(_tz())


def today_local(now: Optional[datetime] = None) -> date:
    """Ngày hôm nay theo giờ Việt Nam."""
    tz = _tz()
    return (now.astimezone(tz) if now else datetime.now(tz)).date()


def weekday_name(d: date) -> str:
    return _WEEKDAYS[d.weekday()]


def _fmt(d: date) -> str:
    return d.strftime("%d/%m/%Y")


def _month_end(y: int, m: int) -> date:
    return date(y, m, calendar.monthrange(y, m)[1])


def _shift_month(y: int, m: int, delta: int) -> tuple[int, int]:
    idx = y * 12 + (m - 1) + delta
    return idx // 12, idx % 12 + 1


def _quarter_of(d: date) -> int:
    return (d.month - 1) // 3 + 1


def _quarter_start(y: int, q: int) -> date:
    return date(y, 3 * (q - 1) + 1, 1)


def _quarter_end(y: int, q: int) -> date:
    return _month_end(y, 3 * q)


def _natural_end(kind: str, start: date) -> date:
    """Ngày cuối "đủ kỳ" của một kỳ bắt đầu từ start."""
    if kind == "week":
        return start + timedelta(days=6)
    if kind == "month":
        return _month_end(start.year, start.month)
    if kind == "quarter":
        return _quarter_end(start.year, _quarter_of(start))
    if kind == "year":
        return date(start.year, 12, 31)
    return start  # day


def _label(kind: str, start: date, end: date, natural_end: date) -> str:
    if kind == "day":
        return f"ngày {_fmt(start)} ({weekday_name(start)})"
    if kind == "week":
        base = f"tuần {_fmt(start)} – {_fmt(natural_end)}"
    elif kind == "month":
        base = f"tháng {start.month:02d}/{start.year}"
    elif kind == "quarter":
        base = f"quý {_quarter_of(start)}/{start.year}"
    elif kind == "year":
        base = f"năm {start.year}"
    else:  # days / range
        return f"từ {_fmt(start)} đến {_fmt(end)}"
    if end != natural_end:  # kỳ bị cắt (chưa hết hoặc cắt để so sánh công bằng)
        base += f" (từ {_fmt(start)} đến {_fmt(end)})"
    return base


def _build(kind, start, end, natural_end, partial, label=None) -> Period:
    return Period(start, end, kind, label or _label(kind, start, end, natural_end), partial)


def _current(kind: str, start: date, today: date, natural_end: Optional[date] = None,
             label: Optional[str] = None) -> Period:
    """Dựng kỳ hiện tại/quá khứ: cắt ở hôm nay, báo lỗi nếu kỳ chưa bắt đầu."""
    nat = natural_end or _natural_end(kind, start)
    if start > today:
        raise PeriodError(
            f"Kỳ này bắt đầu từ {_fmt(start)}, chưa tới nên chưa có số liệu."
        )
    end = min(nat, today)
    if (end - start).days + 1 > MAX_RANGE_DAYS:
        raise PeriodError("Khoảng ngày quá dài (tối đa khoảng 2 năm), hãy chọn khoảng ngắn hơn.")
    return _build(kind, start, end, nat, end >= today, label)


# ---------------------------------------------------------------- đọc cụm thời gian

def _normalize(text: str) -> str:
    """Chữ thường, bỏ dấu tiếng Việt để "hôm qua" và "hom qua" đều hiểu được."""
    text = text.strip().lower().replace("đ", "d")
    text = unicodedata.normalize("NFD", text)
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", text)


_RE_LAST_N = re.compile(r"(?<!\d)(\d{1,3})\s*(ngay|tuan|thang)\s*(?:qua|gan day|gan nhat|truoc)\b")

_RE_DATE = re.compile(
    r"""(?<!\d)(?:
          (?P<iy>\d{4})-(?P<im>\d{1,2})-(?P<id>\d{1,2})
        | (?P<d1>\d{1,2})\s*thang\s*(?P<m1>\d{1,2})(?:\s*nam\s*(?P<y1>\d{4}))?
        | (?P<d2>\d{1,2})[/\-.](?P<m2>\d{1,2})(?:[/\-.](?P<y2>\d{4}|\d{2}))?
    )(?!\d)""",
    re.X,
)
_RE_CONNECTOR = re.compile(r"\b(?:den|toi)\b|[-–—>~]")
_RE_RELATIVE = re.compile(r"\b(tuan|thang|quy|nam) (nay|truoc|qua|ngoai)\b")
_RE_MONTH = re.compile(r"\bthang (\d{1,2})(?:\s*(?:/|-|nam)\s*(\d{4}))?\b")
_RE_QUARTER = re.compile(r"\bquy (\d)(?:\s*(?:/|-|nam)\s*(\d{4}))?\b")
_RE_YEAR = re.compile(r"\bnam (\d{4})\b")


def _try_last_n(t: str, today: date) -> Optional[Period]:
    m = _RE_LAST_N.search(t)
    if not m:
        return None
    n, unit = int(m[1]), m[2]
    if n < 1:
        raise PeriodError("Số ngày/tuần/tháng phải lớn hơn 0.")
    yesterday = today - timedelta(days=1)
    if unit in ("ngay", "tuan"):
        n_days = n if unit == "ngay" else n * 7
        if n_days > MAX_RANGE_DAYS:
            raise PeriodError("Khoảng ngày quá dài (tối đa khoảng 2 năm).")
        start = yesterday - timedelta(days=n_days - 1)
        label = f"{n} {unit.replace('ngay', 'ngày').replace('tuan', 'tuần')} gần nhất " \
                f"(không gồm hôm nay), từ {_fmt(start)} đến {_fmt(yesterday)}"
        return _build("days", start, yesterday, yesterday, False, label)
    if n > 24:
        raise PeriodError("Tối đa 24 tháng gần nhất.")
    y, mo = _shift_month(today.year, today.month, -n)
    start = date(y, mo, 1)
    end = today.replace(day=1) - timedelta(days=1)
    label = f"{n} tháng gần nhất đã đủ, từ {_fmt(start)} đến {_fmt(end)}"
    return _build("range", start, end, end, False, label)


def _parse_date_match(m: re.Match, today: date) -> tuple[date, bool]:
    """Trả về (ngày, có_ghi_năm). Thiếu năm thì lấy năm gần nhất không ở tương lai."""
    g = m.groupdict()
    if g["iy"]:
        d, mo, y = int(g["id"]), int(g["im"]), g["iy"]
    elif g["d1"]:
        d, mo, y = int(g["d1"]), int(g["m1"]), g["y1"]
    else:
        d, mo, y = int(g["d2"]), int(g["m2"]), g["y2"]

    if y is not None:
        year = int(y) + (2000 if len(y) == 2 else 0)
        try:
            return date(year, mo, d), True
        except ValueError:
            raise PeriodError(f"Ngày không hợp lệ: {d:02d}/{mo:02d}/{year}.") from None

    for back in range(5):  # lùi tối đa 5 năm (để xử lý 29/2)
        try:
            cand = date(today.year - back, mo, d)
        except ValueError:
            continue
        if cand <= today:
            return cand, False
    raise PeriodError(f"Ngày không hợp lệ: {d:02d}/{mo:02d}.")


def _try_dates(t: str, today: date) -> Optional[Period]:
    # "hôm nay/hôm qua/hôm kia" đổi thành ngày ISO để dùng chung logic với ngày gõ tay
    t2 = (
        t.replace("hom nay", today.isoformat())
        .replace("hom qua", (today - timedelta(days=1)).isoformat())
        .replace("hom kia", (today - timedelta(days=2)).isoformat())
    )
    matches = list(_RE_DATE.finditer(t2))
    if not matches:
        return None

    if len(matches) == 1:
        d, _ = _parse_date_match(matches[0], today)
        if d > today:
            raise PeriodError(f"Ngày {_fmt(d)} chưa tới nên chưa có số liệu.")
        return _build("day", d, d, d, d >= today)

    if len(matches) > 2 or not _RE_CONNECTOR.search(t2[matches[0].end():matches[1].start()]):
        raise PeriodError(
            "Chỉ nhận một ngày hoặc một khoảng 'từ ngày A đến ngày B'. "
            "Muốn so sánh nhiều kỳ thì gọi riêng từng kỳ."
        )
    start, _ = _parse_date_match(matches[0], today)
    end, _ = _parse_date_match(matches[1], today)
    if start > end:
        raise PeriodError("Ngày bắt đầu đang sau ngày kết thúc.")
    if start == end:
        return _try_dates(matches[0].group(0), today)
    return _current("range", start, today, natural_end=end)


def _try_relative(t: str, today: date) -> Optional[Period]:
    m = _RE_RELATIVE.search(t)
    if not m:
        return None
    unit, which = m[1], m[2]
    back = 0 if which == "nay" else 1
    if unit == "tuan":
        start = today - timedelta(days=today.weekday()) - timedelta(days=7 * back)
        return _current("week", start, today)
    if unit == "thang":
        y, mo = _shift_month(today.year, today.month, -back)
        return _current("month", date(y, mo, 1), today)
    if unit == "quy":
        idx = today.year * 4 + (_quarter_of(today) - 1) - back
        return _current("quarter", _quarter_start(idx // 4, idx % 4 + 1), today)
    return _current("year", date(today.year - back, 1, 1), today)


def _try_explicit(t: str, today: date) -> Optional[Period]:
    m = _RE_MONTH.search(t)
    if m:
        mo = int(m[1])
        if not 1 <= mo <= 12:
            raise PeriodError(f"Tháng {mo} không hợp lệ.")
        year = int(m[2]) if m[2] else today.year
        if not m[2] and date(year, mo, 1) > today:
            year -= 1  # "tháng 12" khi đang là tháng 11 -> tháng 12 năm ngoái
        return _current("month", date(year, mo, 1), today)

    m = _RE_QUARTER.search(t)
    if m:
        q = int(m[1])
        if not 1 <= q <= 4:
            raise PeriodError(f"Quý {q} không hợp lệ (chỉ có quý 1-4).")
        year = int(m[2]) if m[2] else today.year
        if not m[2] and _quarter_start(year, q) > today:
            year -= 1
        return _current("quarter", _quarter_start(year, q), today)

    m = _RE_YEAR.search(t)
    if m:
        year = int(m[1])
        if not 2000 <= year <= 2100:
            raise PeriodError(f"Năm {year} không hợp lệ.")
        return _current("year", date(year, 1, 1), today)
    return None


def resolve_period(text: str, today: Optional[date] = None) -> Period:
    """Đổi cụm thời gian tiếng Việt thành Period. Raise PeriodError nếu không hiểu/không hợp lệ."""
    if not text or not text.strip():
        raise PeriodError("Chưa có cụm thời gian nào để đổi.")
    today = today or today_local()
    t = _normalize(text)
    period = (
        _try_last_n(t, today)
        or _try_dates(t, today)
        or _try_relative(t, today)
        or _try_explicit(t, today)
    )
    if period is None:
        raise PeriodError(
            f"Không hiểu cụm thời gian '{text}'. Ví dụ: hôm qua, tuần này, tháng trước, "
            "tháng 10/2025, quý 3, 7 ngày qua, 3/11, từ 1/11 đến 5/11."
        )
    return period


# ---------------------------------------------------------------- kỳ so sánh

_PREV_NAME = {
    "day": "ngày hôm trước", "week": "tuần trước", "month": "tháng trước",
    "quarter": "quý trước", "year": "năm trước",
}


def previous_period(p: Period) -> Period:
    """Kỳ liền trước, cắt cùng độ dài nếu kỳ hiện tại chưa đủ (so sánh công bằng:
    "tháng này đến ngày 6" so với "tháng trước đến ngày 6")."""
    k = p.kind
    if k in ("days", "range"):
        n = p.days
        end = p.start - timedelta(days=1)
        start = end - timedelta(days=n - 1)
        return _build("range", start, end, end, False)

    if k == "day":
        start = p.start - timedelta(days=1)
        return _build("day", start, start, start, False)

    if k == "week":
        start = p.start - timedelta(days=7)
    elif k == "month":
        y, mo = _shift_month(p.start.year, p.start.month, -1)
        start = date(y, mo, 1)
    elif k == "quarter":
        idx = p.start.year * 4 + (_quarter_of(p.start) - 1) - 1
        start = _quarter_start(idx // 4, idx % 4 + 1)
    else:  # year
        start = date(p.start.year - 1, 1, 1)

    nat = _natural_end(k, start)
    full = p.end == _natural_end(k, p.start)
    end = nat if full else min(start + (p.end - p.start), nat)
    return _build(k, start, end, nat, False)


def comparison_periods(p: Period) -> list[tuple[str, Period]]:
    """Các kỳ nên đem so sánh: (tên gọi, kỳ). Ngày thì thêm "cùng ngày tuần trước"."""
    out = [(_PREV_NAME.get(p.kind, "kỳ liền trước cùng độ dài"), previous_period(p))]
    if p.kind == "day":
        d = p.start - timedelta(days=7)
        out.append((f"cùng ngày tuần trước ({weekday_name(d)})", _build("day", d, d, d, False)))
    return out