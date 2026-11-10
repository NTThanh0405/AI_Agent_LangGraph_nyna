# tests/test_reports.py
# Kiểm tra báo cáo sáng cho manager: đọc cấu hình, đến giờ/không gửi trùng, dựng nội dung,
# vòng gửi (dùng bot và DB giả), và việc lưu báo cáo vào thread để /reply nối tiếp được.
# Không cần DB thật, Telegram thật hay LLM. Dùng asyncio.run nên không cần pytest-asyncio.

import asyncio
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, time
from decimal import Decimal
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from aiogram.exceptions import TelegramForbiddenError
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition
from langchain_core.tools import tool

from app.agent.state import AgentState
from app.reports import builder, scheduler, subscriptions
from app.reports.subscriptions import (
    ConfigError, ConfigStore, ManagerReport, ReportConfig, ReportState,
    due_reports, effective_send_time, load_config, parse_time,
)

TZ = ZoneInfo("Asia/Ho_Chi_Minh")
TODAY = date(2026, 11, 6)
SECTIONS = ["kpi_summary", "revenue_by_channel", "top_products"]


def at(h, m=0, day=TODAY):
    return datetime(day.year, day.month, day.day, h, m, tzinfo=TZ)


def make_cfg(**kw):
    mgr = ManagerReport(111, "A", time(7, 0), ("kpi_summary",))
    base = dict(default_send_time=time(7, 0), max_delay_minutes=180, manager_can_change_time=False,
                manager_time_min=time(6, 0), manager_time_max=time(10, 0), footer="",
                managers={111: mgr})
    base.update(kw)
    return ReportConfig(**base)


# ------------------------------------------------------------ đọc reports.yaml
def write(tmp_path, text):
    p = tmp_path / "reports.yaml"
    p.write_text(text, encoding="utf-8")
    return p


def test_load_config_ok(tmp_path):
    p = write(tmp_path, '''
default_send_time: "07:00"
default_sections: [kpi_summary]
managers:
  111: {name: A, send_time: "07:30", sections: [kpi_summary, top_products, kpi_summary]}
  222: {name: B}
''')
    cfg = load_config(p, SECTIONS)
    assert cfg.managers[111].send_time == time(7, 30)
    assert cfg.managers[111].sections == ("kpi_summary", "top_products")  # bỏ trùng
    assert cfg.managers[222].sections == ("kpi_summary",) and cfg.managers[222].send_time == time(7, 0)


def test_unquoted_time_is_understood():
    assert parse_time(630, "x") == time(10, 30)   # YAML đọc 10:30 không ngoặc kép thành 630
    assert parse_time(420, "x") == time(7, 0)
    assert parse_time("07:05", "x") == time(7, 5)


@pytest.mark.parametrize("bad", ["25:00", "7h", "07:60", True, None, 2000])
def test_bad_time(bad):
    with pytest.raises(ConfigError):
        parse_time(bad, "x")


@pytest.mark.parametrize("text", [
    "managers:\n  111: {sections: [khong_co_muc_nay]}",
    "managers:\n  abc: {sections: [kpi_summary]}",
    "managers:\n  111: {name: A}",                      # thiếu sections, không có default
    "managers:\n  111: {sections: []}",
    "managers:\n  111: {sections: [kpi_summary], send_time: '99:99'}",
    "max_delay_minutes: 0",
    "manager_time_range: ['10:00', '06:00']",
    "- một danh sách",
    "managers: [1, 2]",
])
def test_invalid_config(tmp_path, text):
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, text), SECTIONS)


def test_config_store_hot_reload_and_keeps_old_on_error(tmp_path):
    p = write(tmp_path, "managers:\n  111: {sections: [kpi_summary], send_time: '07:00'}")
    store = ConfigStore(p, SECTIONS)
    assert store.get().managers[111].send_time == time(7, 0)

    p.write_text("managers:\n  111: {sections: [kpi_summary], send_time: '08:00'}", encoding="utf-8")
    os.utime(p, (p.stat().st_atime, p.stat().st_mtime + 5))
    assert store.get().managers[111].send_time == time(8, 0)       # nạp lại khi file đổi

    p.write_text("managers: [hỏng", encoding="utf-8")
    os.utime(p, (p.stat().st_atime, p.stat().st_mtime + 5))
    assert store.get().managers[111].send_time == time(8, 0)       # file lỗi: giữ bản cũ


def test_config_store_missing_file(tmp_path):
    assert ConfigStore(tmp_path / "khong_co.yaml").get() is None


# ------------------------------------------------------------ đến giờ gửi chưa
def due_ids(cfg, states, now):
    return [d.manager.user_id for d in due_reports(cfg, states, now)]


def test_not_due_before_time_due_at_time():
    cfg = make_cfg()
    assert due_ids(cfg, {}, at(6, 59)) == []
    assert due_ids(cfg, {}, at(7, 0)) == [111]


def test_no_duplicate_same_day_but_next_day_ok():
    cfg = make_cfg()
    st = {111: ReportState(last_sent_on=TODAY)}
    assert due_ids(cfg, st, at(7, 30)) == []
    assert due_ids(cfg, st, at(7, 0, day=date(2026, 11, 7))) == [111]


def test_restart_late_sends_catch_up_but_not_after_window():
    cfg = make_cfg()
    assert due_ids(cfg, {}, at(7, 2)) == [111]       # restart 6h59, lên lại 7h02: gửi bù
    assert due_ids(cfg, {}, at(10, 0)) == [111]      # đúng mép 180 phút
    assert due_ids(cfg, {}, at(10, 1)) == []         # quá muộn: bỏ qua hôm nay
    assert due_ids(cfg, {}, at(15, 0)) == []


def test_disabled_and_paused():
    off = ManagerReport(111, "A", time(7, 0), ("kpi_summary",), enabled=False)
    assert due_ids(make_cfg(managers={111: off}), {}, at(7, 5)) == []
    assert due_ids(make_cfg(), {111: ReportState(paused=True)}, at(7, 5)) == []


def test_manager_time_override_only_when_allowed():
    st = {111: ReportState(send_time=time(9, 0))}
    locked = make_cfg(manager_can_change_time=False)
    assert effective_send_time(locked, locked.managers[111], st[111]) == time(7, 0)
    assert due_ids(locked, st, at(7, 5)) == [111]

    free = make_cfg(manager_can_change_time=True)
    assert effective_send_time(free, free.managers[111], st[111]) == time(9, 0)
    assert due_ids(free, st, at(7, 5)) == []
    assert due_ids(free, st, at(9, 5)) == [111]


def test_set_send_time_validation():
    class P:  # pool giả: chỉ cần tới được bước kiểm tra
        def connection(self): raise AssertionError("không được ghi DB khi không hợp lệ")
    run = lambda cfg, t: asyncio.run(subscriptions.set_send_time(P(), cfg, 111, t))
    with pytest.raises(ValueError):
        run(make_cfg(manager_can_change_time=False), time(8, 0))
    with pytest.raises(ValueError):
        run(make_cfg(manager_can_change_time=True), time(5, 0))
    with pytest.raises(ValueError):
        run(make_cfg(manager_can_change_time=True), time(11, 0))
    with pytest.raises(ValueError):  # người chưa được đăng ký
        asyncio.run(subscriptions.set_send_time(P(), make_cfg(manager_can_change_time=True), 999, time(8, 0)))


# ------------------------------------------------------------ định dạng số
def test_number_formatting():
    assert builder.fmt_value(1234567, "revenue") == "1.234.567 ₫"
    assert builder.fmt_value(Decimal("12.5"), "growth_pct") == "12,5%"
    assert builder.fmt_value(1500, "orders") == "1.500"
    assert builder.fmt_value(None, "x") == "—"
    assert builder.fmt_value(date(2026, 11, 5), "day") == "05/11/2026"


def test_delta_text_including_divide_by_zero():
    assert builder.delta_text(110, 100) == "▲ 10,0%"
    assert builder.delta_text(90, 100) == "▼ 10,0%"
    assert builder.delta_text(100, 100) == "≈ 0%"
    assert builder.delta_text(50, 0) is None
    assert builder.delta_text(50, None) is None
    assert builder.delta_text("a", 1) is None


def test_split_text():
    assert builder.split_text("ngắn") == ["ngắn"]
    text = "\n\n".join(f"đoạn {i} " + "x" * 300 for i in range(30))
    chunks = builder.split_text(text, limit=1000)
    assert all(len(c) <= 1000 for c in chunks) and len(chunks) > 1
    assert "".join(chunks).replace("\n", "").count("đoạn") == 30
    assert all(len(c) <= 100 for c in builder.split_text("y" * 450, limit=100))  # cắt cứng khi không có xuống dòng


# ------------------------------------------------------------ dựng báo cáo (DB giả)
SQL_KPI = "SELECT sum(total) AS revenue, count(*) AS orders FROM public.orders WHERE created_at >= %(start)s AND created_at < %(end_exclusive)s"
SQL_CHANNEL = "SELECT channel, sum(total) AS revenue, count(*) AS orders FROM public.orders WHERE created_at >= %(start)s AND created_at < %(end_exclusive)s GROUP BY channel"
SQL_SECRET = "SELECT * FROM public.salaries WHERE d >= %(start)s"


class FakePool:
    """Trả kết quả theo (file SQL, ngày bắt đầu). Ghi lại các lần gọi."""

    def __init__(self, data, fail_for=()):
        self.data, self.fail_for, self.calls = data, set(fail_for), []

    @asynccontextmanager
    async def connection(self):
        pool = self

        class Conn:
            async def execute(self, sql, params):
                pool.calls.append((sql, params))
                for marker in pool.fail_for:
                    if marker in sql:
                        raise RuntimeError("DB lỗi")
                key = ("channel" if "GROUP BY channel" in sql else "kpi", params["start"])
                cols, rows = pool.data.get(key, (["revenue", "orders"], []))
                return SimpleNamespace(
                    description=[SimpleNamespace(name=c) for c in cols],
                    fetchmany=lambda n, rows=rows: _aw(rows[:n]),  # builder v2 dùng fetchmany(MAX_FETCH_ROWS)
                )

        yield Conn()


async def _aw(x):
    return x


@pytest.fixture()
def queries(tmp_path, monkeypatch):
    (tmp_path / "kpi_summary.sql").write_text(SQL_KPI, encoding="utf-8")
    (tmp_path / "revenue_by_channel.sql").write_text(SQL_CHANNEL + ";", encoding="utf-8")
    (tmp_path / "top_products.sql").write_text(SQL_SECRET, encoding="utf-8")  # bảng manager không được xem
    monkeypatch.setattr(builder, "QUERY_DIR", tmp_path)
    # Giả lập quyền để test không phụ thuộc ROLE_TABLES thật: chỉ bảng salaries bị cấm
    monkeypatch.setattr(builder, "can_access", lambda role, table: table != "public.salaries")
    builder._prepare.cache_clear()  # v2: file SQL đi qua guard.check_template rồi được giữ trong RAM
    return tmp_path


D = lambda y, m, d: date(y, m, d)
DATA = {
    ("kpi", D(2026, 11, 5)): (["revenue", "orders"], [(Decimal("11000000"), 55)]),       # hôm qua
    ("kpi", D(2026, 11, 4)): (["revenue", "orders"], [(Decimal("10000000"), 0)]),        # hôm trước (đơn = 0)
    ("kpi", D(2026, 10, 29)): (["revenue", "orders"], [(Decimal("12000000"), 50)]),      # cùng ngày tuần trước
    ("channel", D(2026, 11, 5)): (["channel", "revenue", "orders"],
                                  [("Shopee", Decimal("6000000"), 30), (None, Decimal("5000000"), 25)]),
}


def build(pool, sections, role="manager"):
    return asyncio.run(builder.build_report(pool, role, sections, today=TODAY))


def test_build_report_content(queries):
    rep = build(FakePool(DATA), ["kpi_summary", "revenue_by_channel"])
    t = rep.text
    assert "Báo cáo kinh doanh ngày 05/11/2026 (Thứ Năm)" in t
    assert "• Doanh thu: 11.000.000 ₫ (▲ 10,0% so với ngày hôm trước; ▼ 8,3% so với cùng ngày tuần trước" in t
    assert "• Số đơn: 55 (▲ 10,0% so với cùng ngày tuần trước" in t
    assert "so với ngày hôm trước" not in t.split("• Số đơn")[1].split("\n")[0]   # hôm trước = 0: không chia cho 0
    assert "1. Shopee — Doanh thu: 6.000.000 ₫, Số đơn: 30" in t
    assert "2. (không rõ)" in t                       # tên kênh NULL
    assert rep.ok == ("kpi_summary", "revenue_by_channel") and not rep.failed and not rep.denied


def test_section_over_permission_is_skipped(queries):
    rep = build(FakePool(DATA), ["kpi_summary", "top_products"])
    assert rep.denied == ("top_products",) and rep.ok == ("kpi_summary",)
    assert "Sản phẩm bán chạy" not in rep.text


def test_failed_section_does_not_kill_report(queries):
    rep = build(FakePool(DATA, fail_for=["GROUP BY channel"]), ["kpi_summary", "revenue_by_channel"])
    assert rep.failed == ("revenue_by_channel",) and rep.ok == ("kpi_summary",)
    assert "Tạm thời không lấy được số liệu" in rep.text


def test_all_sections_failed_raises(queries):
    with pytest.raises(builder.ReportError):
        build(FakePool(DATA, fail_for=["public.orders"]), ["kpi_summary", "revenue_by_channel"])
    with pytest.raises(builder.ReportError):
        build(FakePool(DATA), ["top_products"])        # chỉ có mục vượt quyền
    with pytest.raises(builder.ReportError):
        build(FakePool(DATA), ["ten_khong_co"])


def test_empty_result_message(queries):
    rep = build(FakePool({}), ["revenue_by_channel"])
    assert "Không có dữ liệu trong kỳ này" in rep.text


def test_dict_rows_are_supported(queries):
    data = {("kpi", D(2026, 11, 5)): (["revenue", "orders"], [{"revenue": 5, "orders": 1}])}
    assert "• Số đơn: 1" in build(FakePool(data), ["kpi_summary"]).text


# ------------------------------------------------------------ vòng gửi (bot, DB giả)
class FakeBot:
    def __init__(self, error=None):
        self.sent, self.error = [], error

    async def send_message(self, chat_id, text, parse_mode="unset"):
        if self.error:
            raise self.error
        self.sent.append((chat_id, text, parse_mode))


@pytest.fixture()
def env(monkeypatch, tmp_path):
    """Dựng môi trường giả cho scheduler.run_once; trả về các 'sổ ghi' để kiểm tra."""
    monkeypatch.setattr(scheduler, "SEND_GAP_SECONDS", 0)
    scheduler._attempts.clear()
    log = SimpleNamespace(marked=[], threads=[], states={}, role="manager", user_exists=True,
                          build_calls=0, build_fail=False, skipped=[], audit=[], mark_sent_fails=False)

    async def load_states(pool): return log.states
    async def mark_sent(pool, uid, day):
        if log.mark_sent_fails:
            raise RuntimeError("DB lỗi khi ghi report_state")
        log.marked.append((uid, day))
    async def get_user(pool, uid): return SimpleNamespace(role=log.role) if log.user_exists else None
    async def build_report(pool, role, sections, today=None, commentary=None, **kw):
        log.build_calls += 1
        if log.build_fail:
            raise builder.ReportError("lỗi giả")
        return builder.BuiltReport("📊 Báo cáo giả", None, ("kpi_summary",), (), ())
    async def record(graph, pool, uid, role, text, title, today): log.threads.append((uid, text, title))
    async def mark_skipped(pool, uid, day): log.skipped.append((uid, day))
    async def log_turn(pool, **kw): log.audit.append(kw)

    monkeypatch.setattr(scheduler, "load_states", load_states)
    monkeypatch.setattr(scheduler, "mark_sent", mark_sent)
    monkeypatch.setattr(scheduler, "get_allowed_user", get_user)
    monkeypatch.setattr(scheduler, "build_report", build_report)
    monkeypatch.setattr(scheduler, "_record_thread", record)
    monkeypatch.setattr(scheduler, "mark_skipped", mark_skipped)
    monkeypatch.setattr(scheduler, "log_turn", log_turn)

    cfg_path = write(tmp_path, "footer: 'Gõ /reply để hỏi tiếp'\nmanagers:\n  111: {sections: [kpi_summary], send_time: '07:00'}")
    log.store = ConfigStore(cfg_path, SECTIONS)
    return log


def run(env, bot, now):
    return asyncio.run(scheduler.run_once(bot, None, None, lambda role: object(), env.store, now=now))


def test_run_once_sends_marks_and_records_thread(env):
    bot = FakeBot()
    assert run(env, bot, at(6, 59)) == 0 and bot.sent == []          # chưa đến giờ
    assert run(env, bot, at(7, 0)) == 1
    assert bot.sent == [(111, "📊 Báo cáo giả\n\nGõ /reply để hỏi tiếp", None)]   # parse_mode=None, có footer
    assert env.marked == [(111, TODAY)] and env.threads[0][0] == 111 and env.threads[0][2] == "Báo cáo 06/11"


def test_run_once_no_duplicate_after_marked(env):
    bot = FakeBot()
    run(env, bot, at(7, 0))
    env.states[111] = ReportState(last_sent_on=TODAY)               # DB đã ghi ngày gửi
    assert run(env, bot, at(7, 1)) == 0 and len(bot.sent) == 1


def test_blocked_user_skipped_for_the_day(env):
    err = TelegramForbiddenError(method=object(), message="Forbidden: bot was blocked by the user")
    bot = FakeBot(error=err)
    assert run(env, bot, at(7, 0)) == 0 and env.marked == []
    env.build_calls = 0
    run(env, bot, at(7, 1))
    assert env.build_calls == 0                                      # không thử lại cả buổi sáng


def test_repeated_failure_gives_up_after_max_attempts(env):
    env.build_fail = True
    bot = FakeBot()
    for minute in range(10):
        run(env, bot, at(7, minute))
    max_attempts = scheduler._max_attempts(env.store.get())      # retry_on_error + 1 (mặc định 2 + 1)
    assert env.build_calls == max_attempts and bot.sent == [] and env.marked == []
    assert env.skipped == [(111, TODAY)]                         # bỏ cuộc thì ghi vào DB, restart cũng không thử lại
    assert env.audit[-1]["status"] == "error"
    # sang ngày mới thì được thử lại
    env.build_fail = False
    assert run(env, bot, at(7, 0, day=date(2026, 11, 7))) == 1


def test_user_removed_or_not_manager_gets_nothing(env):
    bot = FakeBot()
    env.user_exists = False
    assert run(env, bot, at(7, 0)) == 0
    env.user_exists, env.role = True, "staff"
    assert run(env, bot, at(7, 1)) == 0 and bot.sent == [] and env.marked == []


# ------------------------------------------------------------ lưu báo cáo vào thread để /reply nối tiếp
def test_report_in_thread_gives_context_to_followup(monkeypatch):
    """Dùng đúng cấu trúc graph của dự án: chat -> (tools_condition) -> tools/END."""
    seen = {}

    async def chat(state):
        seen["history"] = [(type(m).__name__, m.content) for m in state["messages"]]
        return {"messages": [AIMessage(content="đã nhận")]}

    @tool
    def noop() -> str:
        """không làm gì"""
        return ""

    g = StateGraph(AgentState)
    g.add_node("chat", chat)
    g.add_node("tools", ToolNode([noop]))
    g.add_edge(START, "chat")
    g.add_conditional_edges("chat", tools_condition, {"tools": "tools", END: END})
    g.add_edge("tools", "chat")
    graph = g.compile(checkpointer=MemorySaver())

    registered = []
    async def register(pool, uid, thread, title): registered.append((uid, thread, title))
    monkeypatch.setattr(scheduler, "register_report_thread", register)

    async def flow():
        await scheduler._record_thread(graph, None, 111, "manager", "BÁO CÁO: doanh thu 11 triệu", "Báo cáo 06/11", TODAY)
        config = {"configurable": {"thread_id": registered[0][1], "user_id": "111", "user_role": "manager"}}
        snapshot = await graph.aget_state(config)
        assert snapshot.next == ()                                   # báo cáo không để graph "chạy dở"
        await graph.ainvoke({"messages": [("user", "gửi dashboard doanh thu theo kênh")],
                             "user_id": "111", "user_role": "manager"}, config)

    asyncio.run(flow())
    assert registered == [(111, "tg:111:report:20261106", "Báo cáo 06/11")]
    assert seen["history"][0] == ("AIMessage", "BÁO CÁO: doanh thu 11 triệu")   # agent thấy báo cáo trước
    assert seen["history"][1][0] == "HumanMessage"

# ------------------------------------------------------------ v2: bỏ lỡ giờ, ghi DB lỗi, retry_on_error
def test_late_report_is_skipped_once_with_audit(env):
    bot = FakeBot()
    assert run(env, bot, at(10, 30)) == 0 and bot.sent == []         # quá 180 phút sau 07:00
    assert env.skipped == [(111, TODAY)]
    assert [a["status"] for a in env.audit] == ["skipped_late"]
    env.states[111] = ReportState(last_skipped_on=TODAY)             # DB đã ghi nhận
    run(env, bot, at(10, 31))
    assert env.skipped == [(111, TODAY)] and len(env.audit) == 1     # đúng 1 lần/ngày


def test_mark_sent_failure_does_not_resend_every_minute(env):
    env.mark_sent_fails = True
    bot = FakeBot()
    assert run(env, bot, at(7, 0)) == 1 and len(bot.sent) == 1
    for minute in range(1, 6):
        run(env, bot, at(7, minute))
    assert len(bot.sent) == 1                                        # chặn trong RAM đến hết hôm nay
    assert run(env, bot, at(7, 0, day=date(2026, 11, 7))) == 1       # sang ngày mới thì gửi lại bình thường


def test_retry_on_error_is_configurable(env, tmp_path):
    path = write(tmp_path, "retry_on_error: 0\nmanagers:\n  111: {sections: [kpi_summary], send_time: '07:00'}")
    env.store = ConfigStore(path, SECTIONS)
    env.build_fail = True
    bot = FakeBot()
    for minute in range(5):
        run(env, bot, at(7, minute))
    assert env.build_calls == 1                                      # retry_on_error = 0: thử đúng 1 lần


# ------------------------------------------------------------ v2: file SQL đi qua guard.check_template
def test_unsafe_sql_file_is_reported_as_failed_not_executed(queries):
    (queries / "top_products.sql").write_text("DELETE FROM public.orders", encoding="utf-8")
    builder._prepare.cache_clear()
    pool = FakePool(DATA)
    rep = build(pool, ["kpi_summary", "top_products"])
    assert rep.failed == ("top_products",) and rep.ok == ("kpi_summary",)
    assert all("DELETE" not in sql.upper() for sql, _ in pool.calls)   # câu nguy hiểm không bao giờ tới DB


def test_sql_param_outside_convention_is_rejected(queries):
    (queries / "top_products.sql").write_text("SELECT 1 FROM public.orders WHERE a = %(evil)s", encoding="utf-8")
    builder._prepare.cache_clear()
    assert build(FakePool(DATA), ["kpi_summary", "top_products"]).failed == ("top_products",)


def test_executed_sql_is_the_guard_output_with_default_schema(queries, monkeypatch):
    (queries / "kpi_summary.sql").write_text(
        "SELECT sum(final_amount) AS revenue FROM fact_sales WHERE tran_date >= %(start)s AND tran_date < %(end_exclusive)s",
        encoding="utf-8")
    builder._prepare.cache_clear()
    pool = FakePool({("kpi", D(2026, 11, 5)): (["revenue"], [(5,)])})
    build(pool, ["kpi_summary"])
    assert "dwh.fact_sales" in pool.calls[0][0].lower()                # bảng thiếu schema được gán dwh trước khi chạy
    assert set(pool.calls[0][1]) == {"start", "end", "end_exclusive"}
