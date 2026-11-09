# tests/test_report_commands.py
# Kiểm tra các lệnh báo cáo sáng trong app/telegram/handlers.py: /reply, /baocao, /baocao_tat,
# /baocao_bat, /baocao_gio, và dòng nhắc khi đang hỏi tiếp trong thread báo cáo.
# Dùng message/DB giả, không cần Telegram hay PostgreSQL.

import asyncio
from contextlib import asynccontextmanager
from datetime import date, time
from types import SimpleNamespace

import pytest
from aiogram.filters import CommandObject

from app.reports.subscriptions import ManagerReport, ReportConfig, ReportState
from app.telegram import handlers as h


class FakeMessage:
    def __init__(self):
        self.sent = []

    async def answer(self, text, **kw):
        self.sent.append(text)

    @property
    def last(self):
        return self.sent[-1] if self.sent else None


def user(role="manager", uid=111):
    return SimpleNamespace(telegram_id=uid, role=role, name="A", is_admin=False)


def cmd(args=None):
    return CommandObject(command="x", args=args)


def make_cfg(can_change=False, manager=True, enabled=True):
    mgr = ManagerReport(111, "A", time(7, 0), ("kpi_summary", "revenue_by_channel"), enabled)
    return ReportConfig(time(7, 0), 180, can_change, time(6, 0), time(10, 0), "",
                        {111: mgr} if manager else {})


@pytest.fixture()
def env(monkeypatch):
    log = SimpleNamespace(cfg=make_cfg(), states={}, report={"thread_id": "tg:111:report:20261106", "title": "Báo cáo 06/11"},
                          switched=[], asked=[], paused=[], times=[], switch_ok=True, set_time_error=None)
    monkeypatch.setattr(h, "get_store", lambda: SimpleNamespace(get=lambda: log.cfg))

    async def latest(pool, uid): return log.report
    async def switch(pool, uid, thread):
        log.switched.append((uid, thread)); return log.switch_ok
    async def load(pool): return log.states
    async def paused(pool, uid, flag): log.paused.append((uid, flag))
    async def set_time(pool, cfg, uid, value):
        if log.set_time_error: raise ValueError(log.set_time_error)
        log.times.append((uid, value))
    async def ask(message, user, graph, pool, text): log.asked.append(text)

    monkeypatch.setattr(h, "latest_report_thread", latest)
    monkeypatch.setattr(h, "switch_session", switch)
    monkeypatch.setattr(h, "load_states", load)
    monkeypatch.setattr(h, "set_paused", paused)
    monkeypatch.setattr(h, "set_send_time", set_time)
    monkeypatch.setattr(h, "_ask", ask)
    return log


def run(coro):
    return asyncio.run(coro)


# ---- /reply
def test_reply_non_manager_is_refused(env):
    m = FakeMessage()
    run(h.cmd_reply(m, cmd(), user("boss"), None, None))
    assert m.last == h.MSG_NOT_MANAGER and env.switched == [] and env.asked == []


def test_reply_without_report(env):
    env.report = None
    m = FakeMessage()
    run(h.cmd_reply(m, cmd("gửi dashboard"), user(), None, None))
    assert "Chưa có báo cáo" in m.last and env.switched == [] and env.asked == []


def test_reply_switches_without_question(env):
    m = FakeMessage()
    run(h.cmd_reply(m, cmd(), user(), None, None))
    assert env.switched == [("111", "tg:111:report:20261106")]
    assert "Báo cáo 06/11" in m.last and env.asked == []


def test_reply_with_question_switches_then_asks(env):
    m = FakeMessage()
    run(h.cmd_reply(m, cmd("  gửi dashboard doanh thu theo kênh "), user(), None, None))
    assert env.switched and env.asked == ["gửi dashboard doanh thu theo kênh"]


def test_reply_when_report_was_purged_in_between(env):
    env.switch_ok = False
    m = FakeMessage()
    run(h.cmd_reply(m, cmd("hỏi gì đó"), user(), None, None))
    assert "Chưa có báo cáo" in m.last and env.asked == []


# ---- /baocao
def test_baocao_status(env):
    env.states[111] = ReportState(last_sent_on=date(2026, 11, 6))
    m = FakeMessage()
    run(h.cmd_baocao(m, user(), None))
    t = m.last
    assert "đang bật" in t and "07:00" in t and "do admin đặt" in t
    assert "Chỉ số chính, Doanh thu theo kênh" in t and "06/11/2026" in t
    assert "/baocao_gio" not in t


def test_baocao_status_paused_disabled_never_sent(env):
    env.states[111] = ReportState(paused=True)
    m = FakeMessage()
    run(h.cmd_baocao(m, user(), None))
    assert "tạm dừng" in m.last and "chưa gửi lần nào" in m.last
    env.cfg = make_cfg(enabled=False)
    run(h.cmd_baocao(m, user(), None))
    assert "admin đang tắt" in m.last


def test_baocao_not_registered_or_not_manager_or_no_config(env):
    env.cfg = make_cfg(manager=False)
    m = FakeMessage()
    run(h.cmd_baocao(m, user(), None))
    assert m.last == h.MSG_NOT_REGISTERED
    run(h.cmd_baocao(m, user("staff"), None))
    assert m.last == h.MSG_NOT_MANAGER
    env.cfg = None
    run(h.cmd_baocao(m, user(), None))
    assert "chưa được cấu hình" in m.last


def test_baocao_shows_time_command_when_allowed(env):
    env.cfg = make_cfg(can_change=True)
    m = FakeMessage()
    run(h.cmd_baocao(m, user(), None))
    assert "/baocao_gio" in m.last and "do admin đặt" not in m.last


# ---- /baocao_tat, /baocao_bat
def test_pause_and_resume(env):
    m = FakeMessage()
    run(h.cmd_baocao_tat(m, user(), None))
    run(h.cmd_baocao_bat(m, user(), None))
    assert env.paused == [(111, True), (111, False)]


def test_resume_refused_if_admin_disabled(env):
    env.cfg = make_cfg(enabled=False)
    m = FakeMessage()
    run(h.cmd_baocao_bat(m, user(), None))
    assert env.paused == [] and "Admin đang tắt" in m.last


def test_pause_by_non_manager_or_unregistered(env):
    m = FakeMessage()
    run(h.cmd_baocao_tat(m, user("boss"), None))
    assert env.paused == [] and m.last == h.MSG_NOT_MANAGER
    env.cfg = make_cfg(manager=False)
    run(h.cmd_baocao_tat(m, user(), None))
    assert env.paused == [] and m.last == h.MSG_NOT_REGISTERED


# ---- /baocao_gio
@pytest.mark.parametrize("text,expected", [
    ("07:30", time(7, 30)), ("7:30", time(7, 30)), ("7h30", time(7, 30)), ("7h", time(7, 0)),
    ("  08:00 ", time(8, 0)), ("23:59", time(23, 59)),
])
def test_parse_hhmm_ok(text, expected):
    assert h._parse_hhmm(text) == expected


@pytest.mark.parametrize("text", ["", "abc", "24:00", "7:60", "7:5", "7h5", "07:30:00", "-1"])
def test_parse_hhmm_bad(text):
    assert h._parse_hhmm(text) is None


def test_gio_locked_by_admin(env):
    m = FakeMessage()
    run(h.cmd_baocao_gio(m, cmd("08:00"), user(), None))
    assert env.times == [] and "do admin đặt" in m.last and "07:00" in m.last


def test_gio_allowed(env):
    env.cfg = make_cfg(can_change=True)
    m = FakeMessage()
    run(h.cmd_baocao_gio(m, cmd("7h30"), user(), None))
    assert env.times == [(111, time(7, 30))] and "07:30" in m.last


def test_gio_bad_input_and_out_of_range(env):
    env.cfg = make_cfg(can_change=True)
    m = FakeMessage()
    run(h.cmd_baocao_gio(m, cmd("sáng sớm"), user(), None))
    assert env.times == [] and "Cách dùng" in m.last and "06:00" in m.last and "10:00" in m.last
    run(h.cmd_baocao_gio(m, cmd(None), user(), None))
    assert "Cách dùng" in m.last
    env.set_time_error = "Chỉ chọn được giờ từ 06:00 đến 10:00."
    run(h.cmd_baocao_gio(m, cmd("11:00"), user(), None))
    assert m.last == "Chỉ chọn được giờ từ 06:00 đến 10:00."


# ---- /help
def test_help_shows_report_commands_only_to_managers(env):
    m = FakeMessage()
    run(h.cmd_help(m, user("manager")))
    assert "/baocao" in m.last and "/reply" in m.last and "/baocao_gio" not in m.last
    run(h.cmd_help(m, user("boss")))
    assert "/baocao" not in m.last and "/reply" not in m.last
    env.cfg = make_cfg(can_change=True)
    run(h.cmd_help(m, user("manager")))
    assert "/baocao_gio" in m.last


# ---- dòng nhắc khi hỏi tiếp trong thread báo cáo (_ask thật, run_turn giả)
def test_ask_prefixes_answer_in_report_thread(monkeypatch):
    sent = []
    async def peek(pool, uid): return "tg:111:report:20261106"
    async def run_turn(graph, pool, **kw):
        return SimpleNamespace(ok=True, busy=False, session_reset=False, answer="Đây là dashboard",
                               thread="tg:111:report:20261106")
    async def send(message, text): sent.append(text)
    monkeypatch.setattr(h, "peek_active", peek)
    monkeypatch.setattr(h, "run_turn", run_turn)
    monkeypatch.setattr(h, "send_text", send)
    monkeypatch.setattr(h, "is_busy", lambda t: False)
    # progress/limiter/audit thật cần bot Telegram và DB, nên thay bằng bản giả
    @asynccontextmanager
    async def no_progress(message):
        yield
    async def no_audit(*a, **k): return None
    monkeypatch.setattr(h, "progress", no_progress)
    monkeypatch.setattr(h, "get_limiter", lambda: SimpleNamespace(hit=lambda uid: None))
    monkeypatch.setattr(h, "log_turn", no_audit)

    run(h._ask(FakeMessage(), user(), None, None, "gửi dashboard"))
    assert sent == ["📊 Đang hỏi tiếp báo cáo ngày 06/11\n\nĐây là dashboard"]

    async def run_normal(graph, pool, **kw):
        return SimpleNamespace(ok=True, busy=False, session_reset=False, answer="Doanh thu 5tr", thread="tg:111:ab12cd34")
    monkeypatch.setattr(h, "run_turn", run_normal)
    sent.clear()
    run(h._ask(FakeMessage(), user(), None, None, "doanh thu"))
    assert sent == ["Doanh thu 5tr"]          # phiên thường: không thêm dòng nhắc


def test_router_builds_with_new_commands():
    assert h.build_router() is not None