# app/agent/knowledge/loader.py
# Nạp tri thức nghiệp vụ cho agent từ các file cùng thư mục:
#   business_context.md  glossary.md  sql_examples.md  calendar.yaml
# Hàm trả về chuỗi sẵn để ghép vào system prompt (đã bỏ phần "chưa rõ"/chưa điền).
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Optional

import yaml

log = logging.getLogger(__name__)

KNOWLEDGE_DIR = Path(__file__).resolve().parent

# Dòng chứa các dấu này là chỗ chủ doanh nghiệp chưa điền -> không gửi cho agent.
_PLACEHOLDER_MARKERS = ("Chưa rõ", "[ĐIỀN")
# Phần "quy tắc" của sql_examples.md kết thúc ở mục ví dụ đầu tiên ("## 1. ...").
_FIRST_EXAMPLE_HEADING = re.compile(r"^##\s*1[.\s]", re.M)
# Khoá trong calendar.yaml đánh dấu sự kiện chưa chắc ngày (chấp nhận vài cách đặt tên).
_UNCONFIRMED_TRUE_KEYS = ("tentative", "estimated", "approximate", "approx", "uncertain", "unconfirmed")


def _read(name: str) -> str:
    return (KNOWLEDGE_DIR / name).read_text(encoding="utf-8")


def _drop_placeholders(text: str) -> str:
    kept = [ln for ln in text.splitlines() if not any(m in ln for m in _PLACEHOLDER_MARKERS)]
    return "\n".join(kept).strip()


@lru_cache(maxsize=None)
def business_context() -> str:
    return _drop_placeholders(_read("business_context.md"))


@lru_cache(maxsize=None)
def glossary() -> str:
    return _drop_placeholders(_read("glossary.md"))


@lru_cache(maxsize=None)
def sql_examples() -> str:
    return _read("sql_examples.md").strip()


@lru_cache(maxsize=None)
def sql_rules() -> str:
    """Chỉ phần quy tắc (trước mục ví dụ '## 1. ...'), không kèm các ví dụ."""
    text = sql_examples()
    m = _FIRST_EXAMPLE_HEADING.search(text)
    return (text[: m.start()] if m else text).strip()


# ----------------------------------------------------------------------------- lịch sự kiện
@dataclass(frozen=True)
class Event:
    name: str
    start: date
    end: date
    confirmed: bool = True
    note: str = ""


def _to_date(value) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        return date.fromisoformat(value.strip())
    raise ValueError(f"không phải ngày: {value!r}")


def _is_confirmed(item: dict) -> bool:
    # calendar.yaml dùng "verified: false" cho ngày ước tính (vd. Tết); chấp nhận thêm "confirmed: false".
    if item.get("verified") is False or item.get("confirmed") is False:
        return False
    return not any(bool(item.get(k)) for k in _UNCONFIRMED_TRUE_KEYS)


def parse_events(data) -> tuple[Event, ...]:
    """Đọc dict từ calendar.yaml; mục thiếu ngày, ngày sai hoặc end < start bị bỏ qua (có log)."""
    items = data.get("events") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return ()
    events: list[Event] = []
    for item in items:
        try:
            if not isinstance(item, dict):
                raise ValueError("mục không phải dict")
            name = str(item["name"]).strip()
            start, end = _to_date(item["start"]), _to_date(item["end"])
            if not name or end < start:
                raise ValueError("tên rỗng hoặc ngày đảo")
        except (KeyError, ValueError, TypeError) as e:
            log.warning("calendar.yaml: bỏ qua mục %r (%s)", item, e)
            continue
        note = str(item.get("note") or item.get("impact") or "").strip()
        events.append(Event(name, start, end, _is_confirmed(item), note))
    return tuple(events)


@lru_cache(maxsize=None)
def load_events() -> tuple[Event, ...]:
    try:
        data = yaml.safe_load(_read("calendar.yaml"))
    except (OSError, yaml.YAMLError) as e:
        log.warning("không đọc được calendar.yaml: %s", e)
        return ()
    return parse_events(data)


def _overlaps(e: Event, start: date, end: date) -> bool:
    return e.start <= end and e.end >= start


def _fmt_range(e: Event) -> str:
    if e.start == e.end:
        return e.start.strftime("%d/%m/%Y")
    return f"{e.start.strftime('%d/%m/%Y')} - {e.end.strftime('%d/%m/%Y')}"


def _line(e: Event, suffix: str = "") -> str:
    flag = "" if e.confirmed else " (chưa xác nhận ngày)"
    note = f": {e.note}" if e.note else ""
    return f"- {e.name} [{_fmt_range(e)}]{flag}{suffix}{note}"


def events_text(start: Optional[date], end: Optional[date], include_previous: bool = True) -> Optional[str]:
    """Các sự kiện trùng kỳ [start, end]; tuỳ chọn thêm sự kiện của kỳ trước cùng độ dài
    (để agent giải thích khi so sánh). Trả None nếu không có gì."""
    if start is None or end is None or end < start:
        return None
    events = load_events()
    in_period = [e for e in events if _overlaps(e, start, end)]
    lines = [_line(e) for e in in_period]

    if include_previous:
        length = (end - start).days + 1
        prev_end = start - timedelta(days=1)
        prev_start = prev_end - timedelta(days=length - 1)
        seen = {id(e) for e in in_period}
        lines += [
            _line(e, " — rơi vào kỳ trước (kỳ so sánh)")
            for e in events
            if id(e) not in seen and _overlaps(e, prev_start, prev_end)
        ]

    if not lines:
        return None
    return "Sự kiện/ngày đặc biệt có thể ảnh hưởng doanh số:\n" + "\n".join(lines)