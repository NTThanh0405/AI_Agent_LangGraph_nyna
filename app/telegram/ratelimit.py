# app/telegram/ratelimit.py
# Giới hạn số câu hỏi của mỗi người dùng để tránh tốn tiền API do spam hoặc bấm liên tục:
#   - tối đa RATE_LIMIT_PER_MIN câu trong 60 giây gần nhất (cửa sổ trượt),
#   - tối đa RATE_LIMIT_PER_DAY câu mỗi ngày (tính theo TIMEZONE, làm mới lúc 00:00).
# Lưu trong RAM của process (agent chạy 1 worker), mất khi restart.

import math
import time
from collections import deque
from datetime import datetime, tzinfo
from typing import Callable, Optional
from zoneinfo import ZoneInfo

from app.config import get_settings

_WINDOW_SECONDS = 60


class RateLimiter:
    def __init__(
        self,
        per_min: int,
        per_day: int,
        tz: tzinfo,
        monotonic: Callable[[], float] = time.monotonic,
        now: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self._per_min = per_min
        self._per_day = per_day
        self._monotonic = monotonic
        self._now = now or (lambda: datetime.now(tz))
        self._recent: dict[str, deque[float]] = {}
        self._daily: dict[str, tuple[object, int]] = {}  # user_id -> (ngày, số lượt)

    def hit(self, user_id: str) -> Optional[str]:
        """Đếm một câu hỏi. Trả None nếu được phép; nếu vượt hạn mức thì trả câu
        thông báo cho người dùng (và KHÔNG tính câu đó vào hạn mức)."""
        t = self._monotonic()
        window = self._recent.setdefault(user_id, deque())
        while window and t - window[0] >= _WINDOW_SECONDS:
            window.popleft()

        today = self._now().date()
        day, count = self._daily.get(user_id, (today, 0))
        if day != today:
            day, count = today, 0

        if count >= self._per_day:
            return (
                f"Hôm nay bạn đã dùng hết {self._per_day} lượt hỏi. "
                "Hạn mức sẽ được làm mới vào 00:00 ngày mai."
            )
        if len(window) >= self._per_min:
            wait = max(1, math.ceil(_WINDOW_SECONDS - (t - window[0])))
            return f"Bạn gửi hơi nhanh rồi, đợi khoảng {wait} giây rồi hỏi tiếp nhé."

        window.append(t)
        self._daily[user_id] = (day, count + 1)
        return None


_limiter: Optional[RateLimiter] = None


def get_limiter() -> RateLimiter:
    """RateLimiter dùng chung, cấu hình theo .env."""
    global _limiter
    if _limiter is None:
        s = get_settings()
        _limiter = RateLimiter(
            per_min=s.rate_limit_per_min,
            per_day=s.rate_limit_per_day,
            tz=ZoneInfo(s.timezone),
        )
    return _limiter