# tests/test_periods.py
# Kiểm tra app/agent/periods.py. "Hôm nay" cố định = thứ Sáu 06/11/2026 để kết quả ổn định.

from datetime import date

import pytest

from app.agent.periods import (
    PeriodError,
    comparison_periods,
    previous_period,
    resolve_period,
    today_local,
)

TODAY = date(2026, 11, 6)  # thứ Sáu


def rp(text):
    return resolve_period(text, TODAY)


def span(p):
    return (p.start, p.end)


# ---- ngày tương đối
def test_today_yesterday():
    p = rp("hôm nay")
    assert span(p) == (TODAY, TODAY) and p.kind == "day" and p.partial
    p = rp("Hôm Qua")
    assert span(p) == (date(2026, 11, 5),) * 2 and not p.partial
    assert span(rp("hom qua")) == (date(2026, 11, 5),) * 2  # không dấu
    assert span(rp("hôm kia")) == (date(2026, 11, 4),) * 2


# ---- tuần / tháng / quý / năm
def test_week():
    p = rp("tuần này")
    assert span(p) == (date(2026, 11, 2), TODAY) and p.partial
    p = rp("tuần trước")
    assert span(p) == (date(2026, 10, 26), date(2026, 11, 1)) and not p.partial


def test_month():
    assert span(rp("tháng này")) == (date(2026, 11, 1), TODAY)
    assert span(rp("tháng trước")) == (date(2026, 10, 1), date(2026, 10, 31))
    assert span(rp("tháng 10")) == (date(2026, 10, 1), date(2026, 10, 31))
    assert span(rp("tháng 1")) == (date(2026, 1, 1), date(2026, 1, 31))
    assert span(rp("tháng 12")) == (date(2025, 12, 1), date(2025, 12, 31))  # chưa tới -> năm ngoái
    assert span(rp("tháng 10/2025")) == (date(2025, 10, 1), date(2025, 10, 31))
    assert span(rp("tháng 2 năm 2024")) == (date(2024, 2, 1), date(2024, 2, 29))


def test_quarter():
    assert span(rp("quý này")) == (date(2026, 10, 1), TODAY)
    assert span(rp("quý trước")) == (date(2026, 7, 1), date(2026, 9, 30))
    assert span(rp("quý 3/2025")) == (date(2025, 7, 1), date(2025, 9, 30))
    assert span(rp("quý 1")) == (date(2026, 1, 1), date(2026, 3, 31))


def test_year():
    assert span(rp("năm nay")) == (date(2026, 1, 1), TODAY)
    assert span(rp("năm ngoái")) == (date(2025, 1, 1), date(2025, 12, 31))
    assert span(rp("năm 2024")) == (date(2024, 1, 1), date(2024, 12, 31))


def test_boundary_year_start():
    jan2 = date(2027, 1, 2)  # thứ Bảy, tuần này bắt đầu 28/12/2026
    assert span(resolve_period("tuần này", jan2)) == (date(2026, 12, 28), jan2)
    assert span(resolve_period("tháng trước", jan2)) == (date(2026, 12, 1), date(2026, 12, 31))
    assert span(resolve_period("quý trước", jan2)) == (date(2026, 10, 1), date(2026, 12, 31))


# ---- N ngày qua
def test_last_n():
    assert span(rp("7 ngày qua")) == (date(2026, 10, 30), date(2026, 11, 5))
    assert rp("7 ngày qua").days == 7
    assert span(rp("2 tuần qua")) == (date(2026, 10, 23), date(2026, 11, 5))
    assert span(rp("3 tháng qua")) == (date(2026, 8, 1), date(2026, 10, 31))
    with pytest.raises(PeriodError):
        rp("0 ngày qua")


# ---- ngày cụ thể
@pytest.mark.parametrize("text,expected", [
    ("ngày 3/11", date(2026, 11, 3)),
    ("3/11", date(2026, 11, 3)),
    ("03/11/2025", date(2025, 11, 3)),
    ("3/11/25", date(2025, 11, 3)),
    ("3-11-2025", date(2025, 11, 3)),
    ("ngày 3 tháng 11", date(2026, 11, 3)),
    ("15 tháng 10 năm 2025", date(2025, 10, 15)),
    ("2025-11-03", date(2025, 11, 3)),
    ("28/12", date(2025, 12, 28)),  # tương lai -> năm ngoái
])
def test_single_date(text, expected):
    p = rp(text)
    assert span(p) == (expected, expected) and p.kind == "day"


def test_date_ranges():
    assert span(rp("từ 1/11 đến 5/11")) == (date(2026, 11, 1), date(2026, 11, 5))
    assert span(rp("1/11 - 5/11")) == (date(2026, 11, 1), date(2026, 11, 5))
    assert span(rp("từ ngày 1 tháng 11 đến ngày 5 tháng 11")) == (date(2026, 11, 1), date(2026, 11, 5))
    assert span(rp("từ 28/12 đến 5/1")) == (date(2025, 12, 28), date(2026, 1, 5))
    assert span(rp("từ 1/11 đến hôm nay")) == (date(2026, 11, 1), TODAY)
    assert rp("từ 1/11 đến hôm nay").partial


def test_month_not_mistaken_for_date():
    assert rp("tháng 10/2025").kind == "month"
    assert rp("quý 3/2025").kind == "quarter"


# ---- lỗi
@pytest.mark.parametrize("text", [
    "", "   ", "abc xyz", "31/02", "29/2/2026", "ngày 20/11/2026", "tháng 12/2026",
    "tháng 13", "quý 5", "5/11 đến 1/11", "hôm nay và hôm qua", "1/11 5/11",
])
def test_errors(text):
    with pytest.raises(PeriodError):
        rp(text)


# ---- kỳ so sánh
def test_previous_day_and_week_ago():
    p = rp("hôm qua")
    names = dict(comparison_periods(p))
    assert span(previous_period(p)) == (date(2026, 11, 4),) * 2
    key = next(k for k in names if k.startswith("cùng ngày tuần trước"))
    assert span(names[key]) == (date(2026, 10, 29),) * 2


def test_previous_partial_month_is_cut_equally():
    prev = previous_period(rp("tháng này"))
    assert span(prev) == (date(2026, 10, 1), date(2026, 10, 6)) and not prev.partial


def test_previous_full_month_keeps_full_length():
    prev = previous_period(rp("tháng 11/2025"))
    assert span(prev) == (date(2025, 10, 1), date(2025, 10, 31))
    # tháng 3 (31 ngày) so với tháng 2 (28 ngày): không tràn sang tháng 3
    assert span(previous_period(rp("tháng 3/2025"))) == (date(2025, 2, 1), date(2025, 2, 28))


def test_previous_week_quarter_year():
    assert span(previous_period(rp("tuần này"))) == (date(2026, 10, 26), date(2026, 10, 30))
    assert span(previous_period(rp("tuần trước"))) == (date(2026, 10, 19), date(2026, 10, 25))
    assert span(previous_period(rp("quý này"))) == (date(2026, 7, 1), date(2026, 8, 6))
    assert span(previous_period(rp("quý 1"))) == (date(2025, 10, 1), date(2025, 12, 31))
    assert span(previous_period(rp("năm nay"))) == (date(2025, 1, 1), date(2025, 11, 6))


def test_previous_range_same_length():
    p = rp("7 ngày qua")
    prev = previous_period(p)
    assert prev.days == 7 and prev.end == date(2026, 10, 29)


def test_sql_end_exclusive_and_today_local():
    p = rp("tháng 10")
    assert p.end_exclusive == date(2026, 11, 1) and p.days == 31
    assert isinstance(today_local(), date)