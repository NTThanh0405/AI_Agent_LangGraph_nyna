# app/agent/business_config.py
# Nạp config/targets.yaml (mục tiêu kinh doanh) và config/thresholds.yaml (ngưỡng gắn cờ).
# Dùng cho snapshot_tools.py / analysis_tools.py / context_tools.py.
#
# NGUYÊN TẮC:
#   - Giá trị null / thiếu = "chưa đặt". KHÔNG bao giờ tự đoán mục tiêu hay ngưỡng.
#   - File rỗng, thiếu, hoặc sai cú pháp thì không làm sập service: dùng cấu hình rỗng
#     (file sai mà trước đó đã nạp được thì giữ bản cũ), có ghi log.
#   - Tự nạp lại khi file đổi (so mtime), không cần restart.
#
# targets.yaml:
#   defaults: {month: {revenue: 1000000000, orders: 5000, ...}}   # mục tiêu mỗi THÁNG
#   overrides: {"2026-12": {revenue: 1500000000}}                 # tháng nào khác mặc định
#   proration: daily_even                                         # chia đều theo ngày
# thresholds.yaml:
#   change_vs_previous: {revenue: {warn_pct: 10, critical_pct: 20, direction: down}}
#   achievement_vs_target: {revenue: {warn_below_pct: 90, critical_below_pct: 70}}
#   guards: {min_base: {revenue: 1000000}, skip_partial_periods: false}
# Ngưỡng % viết là số DƯƠNG (độ lớn); hướng do `direction` quyết định (down | up | both).
# Nếu ai đó viết số âm (vd. -10) thì lấy giá trị tuyệt đối.

import calendar
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

import yaml

log = logging.getLogger(__name__)

TARGETS_CONFIG_PATH = Path(os.environ.get("TARGETS_CONFIG", "config/targets.yaml"))
THRESHOLDS_CONFIG_PATH = Path(os.environ.get("THRESHOLDS_CONFIG", "config/thresholds.yaml"))

# Chỉ số cộng dồn được theo ngày (chia mục tiêu tháng theo ngày được).
ADDITIVE_METRICS = frozenset({"revenue", "orders", "new_customers"})
# Chỉ số dạng tỷ lệ/trung bình: mục tiêu áp dụng nguyên giá trị cho mọi kỳ.
RATE_METRICS = frozenset({"aov"})
# Chỉ số còn lại (vd. customers = số khách khác nhau) không cộng dồn được: chỉ có mục tiêu
# khi kỳ đúng bằng một tháng dương lịch đầy đủ.

_MONTH_KEY = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
_DIRECTIONS = ("down", "up", "both")


class ConfigError(ValueError):
    """File cấu hình sai dạng."""


# ----------------------------------------------------------------------------- tiện ích đọc số

def _num(value: Any, where: str) -> Optional[float]:
    """null/thiếu -> None. Số hợp lệ (>= 0 sau khi lấy trị tuyệt đối với ngưỡng) -> float. Sai -> None + log."""
    if value is None:
        return None
    if isinstance(value, bool):
        log.warning("business_config: %s: không phải số (%r), bỏ qua", where, value)
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        log.warning("business_config: %s: không phải số (%r), bỏ qua", where, value)
        return None


def _non_negative(value: Any, where: str) -> Optional[float]:
    n = _num(value, where)
    if n is not None and n < 0:
        log.warning("business_config: %s: số âm (%s), bỏ qua", where, n)
        return None
    return n


def _magnitude(value: Any, where: str) -> Optional[float]:
    n = _num(value, where)
    return None if n is None else abs(n)


def _as_dict(value: Any, where: str) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"{where} phải là một mục dạng khoá: giá trị")
    return value


def _metric_map(raw: Any, where: str) -> dict[str, Optional[float]]:
    out: dict[str, Optional[float]] = {}
    for key, val in _as_dict(raw, where).items():
        out[str(key).strip().lower()] = _non_negative(val, f"{where}.{key}")
    return out


# ============================================================================= MỤC TIÊU

@dataclass(frozen=True)
class Targets:
    monthly_default: Mapping[str, Optional[float]] = field(default_factory=dict)
    monthly_override: Mapping[str, Mapping[str, Optional[float]]] = field(default_factory=dict)

    def monthly(self, metric: str, year: int, month: int) -> Optional[float]:
        """Mục tiêu CẢ THÁNG của chỉ số; None = chưa đặt."""
        metric = metric.lower()
        override = self.monthly_override.get(f"{year:04d}-{month:02d}", {})
        if metric in override and override[metric] is not None:
            return override[metric]
        return self.monthly_default.get(metric)

    def has_any(self) -> bool:
        if any(v is not None for v in self.monthly_default.values()):
            return True
        return any(v is not None for m in self.monthly_override.values() for v in m.values())

    def for_period(self, metric: str, start: date, end: date) -> Optional[float]:
        """Mục tiêu cho khoảng [start, end] (gồm cả hai đầu). None nếu chưa đặt hoặc không tính được.

        - Chỉ số cộng dồn (doanh thu, số đơn, khách mới): chia đều mục tiêu tháng theo ngày rồi cộng.
        - Chỉ số tỷ lệ (aov): trung bình có trọng số theo số ngày của mỗi tháng.
        - Chỉ số khác (số khách): chỉ khi [start, end] đúng là một tháng đầy đủ.
        Thiếu mục tiêu ở BẤT KỲ tháng nào trong khoảng thì trả None (không tính một nửa).
        """
        if end < start:
            return None
        metric = metric.lower()
        days_by_month: dict[tuple[int, int], int] = {}
        d = start
        while d <= end:
            days_by_month[(d.year, d.month)] = days_by_month.get((d.year, d.month), 0) + 1
            d += timedelta(days=1)

        monthly = {ym: self.monthly(metric, *ym) for ym in days_by_month}
        if any(v is None for v in monthly.values()):
            return None

        total_days = sum(days_by_month.values())
        if metric in ADDITIVE_METRICS:
            return sum(
                monthly[ym] * n / calendar.monthrange(*ym)[1] for ym, n in days_by_month.items()
            )
        if metric in RATE_METRICS:
            return sum(monthly[ym] * n for ym, n in days_by_month.items()) / total_days
        # không cộng dồn được: chỉ nhận đúng một tháng đầy đủ
        if len(days_by_month) == 1:
            (y, m), n = next(iter(days_by_month.items()))
            if n == calendar.monthrange(y, m)[1]:
                return monthly[(y, m)]
        return None


def parse_targets(data: Any) -> Targets:
    if data is None:
        return Targets()
    root = _as_dict(data, "targets.yaml")
    proration = root.get("proration", "daily_even")
    if proration not in (None, "daily_even"):
        log.warning("targets.yaml: proration=%r chưa hỗ trợ, dùng daily_even", proration)
    defaults = _as_dict(_as_dict(root.get("defaults"), "defaults").get("month"), "defaults.month")
    overrides_raw = _as_dict(root.get("overrides"), "overrides")
    overrides: dict[str, dict[str, Optional[float]]] = {}
    for key, raw in overrides_raw.items():
        ym = str(key).strip()
        if not _MONTH_KEY.match(ym):
            log.warning("targets.yaml: overrides có khoá %r không phải YYYY-MM, bỏ qua", key)
            continue
        overrides[ym] = _metric_map(raw, f"overrides.{ym}")
    return Targets(
        monthly_default=_metric_map(defaults, "defaults.month"),
        monthly_override=overrides,
    )


# ============================================================================= NGƯỠNG

@dataclass(frozen=True)
class ChangeRule:
    warn_pct: Optional[float] = None
    critical_pct: Optional[float] = None
    direction: str = "down"

    def is_set(self) -> bool:
        return self.warn_pct is not None or self.critical_pct is not None


@dataclass(frozen=True)
class AchievementRule:
    warn_below_pct: Optional[float] = None
    critical_below_pct: Optional[float] = None

    def is_set(self) -> bool:
        return self.warn_below_pct is not None or self.critical_below_pct is not None


@dataclass(frozen=True)
class Flag:
    """level: None = không đánh giá (chưa có ngưỡng / thiếu dữ liệu); ok | warn | critical."""
    level: Optional[str]
    pct: Optional[float] = None
    note: str = ""


@dataclass(frozen=True)
class Thresholds:
    change: Mapping[str, ChangeRule] = field(default_factory=dict)
    achievement: Mapping[str, AchievementRule] = field(default_factory=dict)
    min_base: Mapping[str, Optional[float]] = field(default_factory=dict)
    skip_partial_periods: bool = False

    def has_any(self) -> bool:
        return any(r.is_set() for r in self.change.values()) or any(
            r.is_set() for r in self.achievement.values()
        )

    def change_flag(self, metric: str, current: Optional[float], previous: Optional[float],
                    partial: bool = False) -> Flag:
        """Gắn cờ theo % thay đổi so với kỳ trước."""
        metric = metric.lower()
        rule = self.change.get(metric)
        if rule is None or not rule.is_set():
            return Flag(None)
        if current is None or previous is None:
            return Flag(None, note="thiếu số liệu để so sánh")
        if previous == 0:
            return Flag(None, note="kỳ trước bằng 0, không tính được % thay đổi")
        pct = (current - previous) / abs(previous) * 100
        if self.skip_partial_periods and partial:
            return Flag(None, pct, "kỳ chưa đủ ngày, chưa gắn cờ")
        base = self.min_base.get(metric)
        if base is not None and abs(previous) < base:
            return Flag(None, pct, "kỳ trước quá nhỏ, không gắn cờ")
        move = {"down": -pct, "up": pct, "both": abs(pct)}[rule.direction]
        if rule.critical_pct is not None and move >= rule.critical_pct:
            return Flag("critical", pct)
        if rule.warn_pct is not None and move >= rule.warn_pct:
            return Flag("warn", pct)
        return Flag("ok", pct)

    def achievement_flag(self, metric: str, achievement_pct: Optional[float]) -> Flag:
        """Gắn cờ theo tỷ lệ đạt mục tiêu (thang 0-100)."""
        rule = self.achievement.get(metric.lower())
        if rule is None or not rule.is_set():
            return Flag(None)
        if achievement_pct is None:
            return Flag(None, note="chưa có mục tiêu hoặc thiếu số liệu")
        if rule.critical_below_pct is not None and achievement_pct < rule.critical_below_pct:
            return Flag("critical", achievement_pct)
        if rule.warn_below_pct is not None and achievement_pct < rule.warn_below_pct:
            return Flag("warn", achievement_pct)
        return Flag("ok", achievement_pct)


def parse_thresholds(data: Any) -> Thresholds:
    if data is None:
        return Thresholds()
    root = _as_dict(data, "thresholds.yaml")

    change: dict[str, ChangeRule] = {}
    for metric, raw in _as_dict(root.get("change_vs_previous"), "change_vs_previous").items():
        r = _as_dict(raw, f"change_vs_previous.{metric}")
        direction = str(r.get("direction") or "down").strip().lower()
        if direction not in _DIRECTIONS:
            log.warning("thresholds.yaml: %s.direction=%r sai (down|up|both), dùng down", metric, direction)
            direction = "down"
        change[str(metric).strip().lower()] = ChangeRule(
            warn_pct=_magnitude(r.get("warn_pct"), f"{metric}.warn_pct"),
            critical_pct=_magnitude(r.get("critical_pct"), f"{metric}.critical_pct"),
            direction=direction,
        )

    achievement: dict[str, AchievementRule] = {}
    for metric, raw in _as_dict(root.get("achievement_vs_target"), "achievement_vs_target").items():
        r = _as_dict(raw, f"achievement_vs_target.{metric}")
        achievement[str(metric).strip().lower()] = AchievementRule(
            warn_below_pct=_non_negative(r.get("warn_below_pct"), f"{metric}.warn_below_pct"),
            critical_below_pct=_non_negative(r.get("critical_below_pct"), f"{metric}.critical_below_pct"),
        )

    guards = _as_dict(root.get("guards"), "guards")
    skip_partial = guards.get("skip_partial_periods", False)
    if not isinstance(skip_partial, bool):
        log.warning("thresholds.yaml: guards.skip_partial_periods phải là true/false, dùng false")
        skip_partial = False
    return Thresholds(
        change=change,
        achievement=achievement,
        min_base=_metric_map(guards.get("min_base"), "guards.min_base"),
        skip_partial_periods=skip_partial,
    )


# ============================================================================= NẠP FILE (có cache theo mtime)

_cache: dict[Path, tuple[float, Any]] = {}


def _load(path: Path, parser: Callable[[Any], Any], empty: Callable[[], Any]) -> Any:
    try:
        mtime = path.stat().st_mtime
    except OSError:
        log.info("business_config: không thấy %s, dùng cấu hình rỗng", path)
        return empty()
    cached = _cache.get(path)
    if cached is not None and cached[0] == mtime:
        return cached[1]
    try:
        obj = parser(yaml.safe_load(path.read_text(encoding="utf-8-sig")))
    except (OSError, yaml.YAMLError, ConfigError) as e:
        log.warning("business_config: %s lỗi (%s); %s", path, e,
                    "giữ bản cũ" if cached else "dùng cấu hình rỗng")
        return cached[1] if cached is not None else empty()
    _cache[path] = (mtime, obj)
    return obj


def load_targets(path: Optional[Path] = None) -> Targets:
    return _load(path or TARGETS_CONFIG_PATH, parse_targets, Targets)


def load_thresholds(path: Optional[Path] = None) -> Thresholds:
    return _load(path or THRESHOLDS_CONFIG_PATH, parse_thresholds, Thresholds)