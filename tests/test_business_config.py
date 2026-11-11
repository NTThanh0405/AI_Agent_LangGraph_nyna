# tests/test_business_config.py
# Nạp targets.yaml / thresholds.yaml: file rỗng/sai không làm sập, mục tiêu chia theo ngày đúng,
# cờ vượt ngưỡng đúng hướng, không chia cho 0.

import os
from datetime import date

import pytest

from app.agent import business_config as bc


def targets(text: str) -> bc.Targets:
    import yaml
    return bc.parse_targets(yaml.safe_load(text))


def thresholds(text: str) -> bc.Thresholds:
    import yaml
    return bc.parse_thresholds(yaml.safe_load(text))


# ----------------------------------------------------------------------------- targets

def test_empty_and_null_targets_mean_not_set():
    assert not bc.parse_targets(None).has_any()
    t = targets("defaults: {month: {revenue: null}}")
    assert not t.has_any()
    assert t.for_period("revenue", date(2026, 11, 1), date(2026, 11, 30)) is None


def test_full_month_equals_monthly_target():
    t = targets("defaults: {month: {revenue: 3000000, customers: 900, aov: 150000}}")
    assert t.for_period("revenue", date(2026, 11, 1), date(2026, 11, 30)) == pytest.approx(3_000_000)
    assert t.for_period("customers", date(2026, 11, 1), date(2026, 11, 30)) == 900


def test_partial_period_prorated_by_days_of_that_month():
    t = targets("defaults: {month: {revenue: 3000000}}")  # tháng 11 có 30 ngày -> 100.000/ngày
    assert t.for_period("revenue", date(2026, 11, 1), date(2026, 11, 6)) == pytest.approx(600_000)


def test_period_spanning_two_months_uses_each_months_target_and_length():
    t = targets("""
defaults: {month: {revenue: 3100000}}
overrides: {"2026-12": {revenue: 6200000}}
""")
    # 30/11 (1 ngày của tháng 30 ngày, 3,1tr) + 1/12 (1 ngày của tháng 31 ngày, 6,2tr)
    got = t.for_period("revenue", date(2026, 11, 30), date(2026, 12, 1))
    assert got == pytest.approx(3_100_000 / 30 + 6_200_000 / 31)


def test_missing_target_in_any_month_gives_none_not_half():
    t = targets('overrides: {"2026-11": {revenue: 3000000}}')
    assert t.for_period("revenue", date(2026, 11, 20), date(2026, 12, 5)) is None


def test_aov_not_prorated_and_customers_only_full_month():
    t = targets("defaults: {month: {aov: 150000, customers: 900}}")
    assert t.for_period("aov", date(2026, 11, 3), date(2026, 11, 9)) == pytest.approx(150_000)
    assert t.for_period("customers", date(2026, 11, 3), date(2026, 11, 9)) is None  # không chia được


def test_bad_values_are_ignored_not_fatal():
    t = targets("defaults: {month: {revenue: -5, orders: abc, aov: true}}")
    assert not t.has_any()
    t = targets('overrides: {"11/2026": {revenue: 1}}')   # khoá sai dạng
    assert not t.has_any()


def test_wrong_top_level_type_raises_config_error():
    with pytest.raises(bc.ConfigError):
        bc.parse_targets(["not", "a", "dict"])


# ----------------------------------------------------------------------------- thresholds

def test_empty_thresholds_flag_nothing():
    th = bc.parse_thresholds(None)
    assert not th.has_any()
    assert th.change_flag("revenue", 50, 100).level is None


def test_change_flag_direction_down():
    th = thresholds("change_vs_previous: {revenue: {warn_pct: 10, critical_pct: 20, direction: down}}")
    assert th.change_flag("revenue", 95, 100).level == "ok"
    assert th.change_flag("revenue", 90, 100).level == "warn"      # đúng ngưỡng thì cờ
    assert th.change_flag("revenue", 79, 100).level == "critical"
    assert th.change_flag("revenue", 150, 100).level == "ok"       # tăng mạnh không phải vấn đề khi direction=down


def test_change_flag_up_and_both_and_negative_threshold_taken_as_magnitude():
    th = thresholds("""
change_vs_previous:
  orders: {warn_pct: -10, direction: down}
  aov: {warn_pct: 10, direction: both}
  new_customers: {warn_pct: 30, direction: up}
""")
    assert th.change_flag("orders", 85, 100).level == "warn"        # -10 được hiểu là 10
    assert th.change_flag("aov", 115, 100).level == "warn"
    assert th.change_flag("aov", 85, 100).level == "warn"
    assert th.change_flag("new_customers", 140, 100).level == "warn"
    assert th.change_flag("new_customers", 60, 100).level == "ok"


def test_change_flag_guards():
    th = thresholds("""
change_vs_previous: {revenue: {warn_pct: 10, direction: down}}
guards: {min_base: {revenue: 1000}, skip_partial_periods: true}
""")
    assert th.change_flag("revenue", 10, 0).level is None            # kỳ trước = 0: không chia cho 0
    assert th.change_flag("revenue", 100, 500).level is None         # kỳ trước nhỏ hơn min_base
    assert th.change_flag("revenue", 1000, 2000, partial=True).level is None
    assert th.change_flag("revenue", 1000, 2000, partial=False).level == "warn"   # -50%, chỉ có warn_pct
    assert th.change_flag("revenue", None, 2000).level is None


def test_achievement_flag():
    th = thresholds("achievement_vs_target: {revenue: {warn_below_pct: 90, critical_below_pct: 70}}")
    assert th.achievement_flag("revenue", 95).level == "ok"
    assert th.achievement_flag("revenue", 85).level == "warn"
    assert th.achievement_flag("revenue", 60).level == "critical"
    assert th.achievement_flag("revenue", None).level is None
    assert th.achievement_flag("orders", 10).level is None           # chưa có ngưỡng cho orders


def test_invalid_direction_falls_back_to_down():
    th = thresholds("change_vs_previous: {revenue: {warn_pct: 10, direction: sideways}}")
    assert th.change["revenue"].direction == "down"


# ----------------------------------------------------------------------------- nạp file

def test_load_missing_empty_broken_and_reload(tmp_path):
    bc._cache.clear()
    p = tmp_path / "targets.yaml"
    assert not bc.load_targets(p).has_any()                         # thiếu file

    p.write_text("", encoding="utf-8")
    assert not bc.load_targets(p).has_any()                         # file rỗng (như hiện tại trong repo)

    p.write_text("defaults: {month: {revenue: 1000}}", encoding="utf-8")
    os.utime(p, (1_000_000, 1_000_000))
    assert bc.load_targets(p).monthly("revenue", 2026, 11) == 1000

    p.write_text("defaults: [unclosed", encoding="utf-8")           # sai cú pháp -> giữ bản cũ
    os.utime(p, (2_000_000, 2_000_000))
    assert bc.load_targets(p).monthly("revenue", 2026, 11) == 1000

    p.write_text("defaults: {month: {revenue: 2000}}", encoding="utf-8")   # sửa lại -> tự nạp bản mới
    os.utime(p, (3_000_000, 3_000_000))
    assert bc.load_targets(p).monthly("revenue", 2026, 11) == 2000


def test_shipped_config_files_parse_and_are_empty_of_numbers():
    """Hai file mẫu trong repo phải nạp được và chưa chứa số nào (chủ doanh nghiệp tự điền)."""
    bc._cache.clear()
    assert not bc.load_targets(bc.Path("config/targets.yaml")).has_any()
    assert not bc.load_thresholds(bc.Path("config/thresholds.yaml")).has_any()