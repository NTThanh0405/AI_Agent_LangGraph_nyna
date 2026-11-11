# tests/test_analysis_tools.py
# snapshot_tools / analysis_tools / context_tools: dựng chữ đúng, không chia cho 0, quyền bị chặn thì báo rõ.
# Không kết nối DB: run_query_file / collect_kpis được thay bằng dữ liệu giả.

from datetime import date

import pytest

from app.agent import business_config as bc
from app.agent.periods import resolve_period
from app.agent.permissions import PermissionDenied
from app.agent.tools import analysis_tools as at
from app.agent.tools import context_tools as ct
from app.agent.tools import snapshot_tools as st
from app.agent.tools._queries import Kpis

TODAY = date(2026, 11, 11)


def kpis(period_text, **values) -> Kpis:
    return Kpis(resolve_period(period_text, TODAY), values)


FULL = dict(revenue=900.0, orders=30.0, aov=30.0, customers=20.0, new_customers=5.0, returning_customers=15.0)


# ----------------------------------------------------------------------------- snapshot

def test_snapshot_flags_drop_and_target_gap():
    cur = kpis("hôm qua", **FULL)
    prev = kpis("hôm kia", **{**FULL, "revenue": 1200.0, "orders": 30.0})
    th = bc.parse_thresholds({
        "change_vs_previous": {"revenue": {"warn_pct": 10, "critical_pct": 20, "direction": "down"}},
        "achievement_vs_target": {"revenue": {"warn_below_pct": 90, "critical_below_pct": 70}},
    })
    tg = bc.parse_targets({"defaults": {"month": {"revenue": 30000}}})   # 1000/ngày -> đạt 90%
    text = st.build_snapshot(cur, [("hôm kia", prev)], tg, th)
    assert "▼ 25,0%" in text
    assert "[NGHIÊM TRỌNG]" in text            # giảm 25% >= 20%
    assert "đạt 90,0%" in text
    assert "Cờ vượt ngưỡng:" in text


def test_snapshot_with_empty_config_says_so_and_has_no_flags():
    cur = kpis("hôm qua", **FULL)
    prev = kpis("hôm kia", **FULL)
    text = st.build_snapshot(cur, [("hôm kia", prev)], bc.Targets(), bc.Thresholds())
    assert "Chưa đặt ngưỡng cảnh báo" in text
    assert "Chưa đặt mục tiêu" in text
    assert "[CẢNH BÁO]" not in text and "[NGHIÊM TRỌNG]" not in text


def test_snapshot_prev_zero_does_not_divide_by_zero():
    cur = kpis("hôm qua", **FULL)
    prev = kpis("hôm kia", **{**FULL, "revenue": 0.0, "orders": 0.0, "aov": None})
    th = bc.parse_thresholds({"change_vs_previous": {"revenue": {"warn_pct": 10, "direction": "down"}}})
    text = st.build_snapshot(cur, [("hôm kia", prev)], bc.Targets(), th)
    assert "không tính được % thay đổi" in text


def test_snapshot_reports_partial_errors():
    cur = kpis("hôm qua", revenue=1.0, orders=1.0, aov=1.0, customers=1.0)
    cur.errors.append("customers_new_vs_returning.sql lỗi khi chạy")
    text = st.build_snapshot(cur, [], bc.Targets(), bc.Thresholds())
    assert "Lỗi một phần" in text


@pytest.mark.asyncio
async def test_snapshot_tool_permission_denied_message(monkeypatch):
    async def deny(*a, **k):
        raise PermissionDenied("dwh.fact_sales")
    monkeypatch.setattr(st, "collect_kpis", deny)
    out = await st.business_snapshot.ainvoke(
        {"period_text": "hôm qua"}, {"configurable": {"user_id": "1", "user_role": "staff"}})
    assert "Không có quyền" in out


@pytest.mark.asyncio
async def test_snapshot_tool_bad_period_is_text_not_exception():
    out = await st.business_snapshot.ainvoke({"period_text": "blah blah"}, {"configurable": {}})
    assert "Không xác định được khoảng thời gian" in out


# ----------------------------------------------------------------------------- analysis

def test_compare_periods_different_lengths_notes_and_per_day():
    a = kpis("7 ngày qua", **{**FULL, "revenue": 7000.0, "orders": 70.0})
    b = kpis("hôm qua", **{**FULL, "revenue": 1000.0, "orders": 10.0})
    text = at.build_comparison(a, b)
    assert "dài khác nhau" in text
    assert "trung bình mỗi ngày" in text
    assert "≈ 0%" in text                      # 7000/7 = 1000/1 -> không đổi theo ngày


def test_decompose_revenue_is_exact():
    cur = kpis("hôm qua", revenue=1200.0, orders=40.0)   # aov 30
    prev = kpis("hôm kia", revenue=1000.0, orders=20.0)  # aov 50
    line = at.decompose_revenue(cur, prev)
    # Δđơn = 20 × 50 = +1000; ΔAOV = 40 × (30-50) = -800; tổng = +200
    assert "+200 ₫" in line and "+1.000 ₫" in line and "-800 ₫" in line
    assert at.decompose_revenue(cur, kpis("hôm kia", revenue=0.0, orders=0.0)) is None


def test_breakdown_sorts_by_abs_delta_and_marks_absent():
    cur = [{"channel": "A", "revenue": 100}, {"channel": "B", "revenue": 50}]
    prev = [{"channel": "A", "revenue": 90}, {"channel": "C", "revenue": 200}]
    items = at.breakdown(cur, prev, "channel")
    assert [i["name"] for i in items] == ["C", "B", "A"]
    assert items[0]["cur"] is None and items[0]["delta"] == -200
    assert items[1]["prev"] is None and items[1]["delta"] == 50


def test_explanation_contains_facts_and_hypothesis_reminder():
    cur = kpis("hôm qua", revenue=800.0, orders=20.0, aov=40.0, customers=10.0, new_customers=2.0, returning_customers=8.0)
    prev = kpis("hôm kia", revenue=1000.0, orders=25.0, aov=40.0, customers=12.0, new_customers=3.0, returning_customers=9.0)
    ch = at.breakdown([{"channel": "Web", "revenue": 300}], [{"channel": "Web", "revenue": 500}], "channel")
    text = at.build_explanation(cur, prev, "hôm kia", ch, None, None, [])
    assert "Web" in text and "-200 ₫" in text
    assert "giả thuyết" in text
    assert "không có ngày lễ" in text         # không có sự kiện thì nói rõ, không bịa


def test_trend_up_down_flat_and_short():
    p = resolve_period("từ 1/11 đến 8/11", TODAY)
    up = [{"day": date(2026, 11, d), "revenue": 100 * d, "orders": 1} for d in range(1, 9)]
    assert "xu hướng TĂNG" in at.summarize_trend(p, up, TODAY)
    down = [{"day": date(2026, 11, d), "revenue": 100 * (9 - d), "orders": 1} for d in range(1, 9)]
    assert "xu hướng GIẢM" in at.summarize_trend(p, down, TODAY)
    flat = [{"day": date(2026, 11, d), "revenue": 100, "orders": 1} for d in range(1, 9)]
    assert "đi ngang" in at.summarize_trend(p, flat, TODAY)
    short = resolve_period("từ 1/11 đến 3/11", TODAY)
    assert "chưa đủ để nói về xu hướng" in at.summarize_trend(short, up[:3], TODAY)


def test_trend_partial_period_drops_today_and_reports_missing_days():
    p = resolve_period("tháng này", TODAY)                 # 1/11 .. 11/11, gồm hôm nay
    assert p.partial
    rows = [{"day": date(2026, 11, d), "revenue": 100, "orders": 1} for d in range(1, 11) if d != 4]
    rows.append({"day": TODAY, "revenue": 5, "orders": 1})   # hôm nay chưa đủ ngày
    text = at.summarize_trend(p, rows, TODAY)
    assert "10 ngày phân tích" in text
    assert "đã bỏ ngày hôm nay" in text.lower()
    assert "1 ngày không có doanh thu" in text
    assert "04/11" in text


# ----------------------------------------------------------------------------- context

def test_context_tools_text_and_no_invention(monkeypatch):
    monkeypatch.setattr(ct, "load_targets", lambda: bc.Targets())
    assert "Chưa đặt mục tiêu" in ct.get_business_targets.invoke({"period_text": "tháng này"})
    assert "Doanh thu" in ct.get_business_knowledge.invoke({"topic": "glossary"})
    assert "Không xác định" in ct.get_period_events.invoke({"period_text": "blah blah"})

    monkeypatch.setattr(ct, "load_targets",
                        lambda: bc.parse_targets({"defaults": {"month": {"revenue": 3000000}}}))
    out = ct.get_business_targets.invoke({"period_text": "tháng 10/2026"})   # tháng đã kết thúc, đủ ngày
    assert "3.000.000 ₫" in out and "chưa đặt mục tiêu" in out   # doanh thu có, các chỉ số khác chưa đặt