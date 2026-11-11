# tests/test_boss_manager_commands.py
# Kiểm tra các lệnh Telegram mà boss và manager dùng (app/telegram/handlers.py), theo từng vai trò:
#   - /start, /help: mỗi role thấy đúng phần hướng dẫn của mình
#   - /tuan, /thang, /dashboard: mọi role dùng được, staff cần có ít nhất một khối dữ liệu
#     (hiện chỉ có coffee; sau thêm house/misa qua API); dashboard cần khối database (coffee)
#   - /capquyen, /thuhoi, /quyen: cấp/thu hồi/xem quyền theo KHỐI dữ liệu cho staff
# Test có fixture api_scopes_config (tests/conftest.py) chạy với cấu hình tương lai có house/misa.
#   - /users, /adduser, /addadmin, /removeadmin, /removeuser: chỉ admin/owner, chống leo thang
#   - lệnh báo cáo sáng chỉ dành cho manager (boss gõ thì được nhắc)
#   - middleware: người lạ, group, tin cũ, /myid
# Dùng message và "database" giả trong RAM (dict), không cần Telegram, PostgreSQL hay LLM.
# Hàm check_manage, _role_allowed, scopes.user_scopes (đọc config/scopes.yaml thật) là bản THẬT.

import os

# handlers.py đọc Settings lúc import: đặt giá trị giả để chạy được file này riêng lẻ
for _k, _v in {"ANTHROPIC_API_KEY": "x", "TELEGRAM_BOT_TOKEN": "x", "DB_PASSWORD": "x"}.items():
    os.environ.setdefault(_k, _v)

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from aiogram.enums import ChatType
from aiogram.filters import CommandObject

from app.agent import grants as grants_mod
from app.telegram import auth
from app.telegram import handlers as h
from app.telegram.auth import ADMIN, OWNER, STAFF, AllowedUser

# ---------------------------------------------------------------- người dùng mẫu
BOSS = 1001          # boss, chủ chatbot (owner)
BOSS2 = 1002         # boss thường, không phải admin
MGR = 2001           # manager thường
MGR_ADMIN = 2002     # manager kiêm admin
STAFF_A = 3001       # nhân viên
STAFF_B = 3002
STRANGER = 9999      # không có trong bảng users

SEED = {
    BOSS: ("boss", OWNER, "Anh Chủ"),
    BOSS2: ("boss", STAFF, "Anh Boss 2"),
    MGR: ("manager", STAFF, "Chị Quản lý"),
    MGR_ADMIN: ("manager", ADMIN, "Anh QL Admin"),
    STAFF_A: ("staff", STAFF, "NV A"),
    STAFF_B: ("staff", STAFF, "NV B"),
}


def as_user(tid: int) -> AllowedUser:
    role, level, name = SEED[tid]
    return AllowedUser(tid, role, level, name)


class FakeMessage:
    def __init__(self, text: str = "", uid: int = BOSS, chat_type=ChatType.PRIVATE, age_seconds: int = 0):
        self.text = text
        self.sent: list[str] = []
        self.markups: list = []
        self.from_user = SimpleNamespace(id=uid)
        self.chat = SimpleNamespace(type=chat_type)
        self.date = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)

    async def answer(self, text, **kw):
        self.sent.append(text)
        self.markups.append(kw.get("reply_markup"))

    @property
    def last(self):
        return self.sent[-1] if self.sent else None


def cmd(args=None):
    return CommandObject(command="x", args=args)


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- "database" giả
@pytest.fixture()
def db(monkeypatch):
    """Bảng users, scope_grants, audit giả. Vá handlers, auth (check_manage dùng auth.get_user) và
    grants.get_grants (scopes.user_scopes đọc khối được cấp qua hàm này)."""
    st = SimpleNamespace(
        users={tid: {"role": r, "admin_level": lv, "name": n} for tid, (r, lv, n) in SEED.items()},
        grants={},            # (tid_str, khối) -> hours
        changes=[],           # log_change
        asked=[],             # câu hỏi gửi cho agent qua _ask
        purged=[],
        purge_error=False,
        admin_count_override=None,
    )

    async def get_user(pool, tid):
        return st.users.get(tid)

    async def admin_count(pool):
        if st.admin_count_override is not None:
            return st.admin_count_override
        return sum(1 for u in st.users.values() if u["admin_level"] == ADMIN)

    async def upsert_user(pool, tid, role, name, by, level=None):
        old = st.users.get(tid, {"admin_level": 0, "name": None})
        st.users[tid] = {
            "role": role.lower(),
            "admin_level": old["admin_level"] if level is None else level,
            "name": name if name is not None else old["name"],
        }

    async def demote_admin(pool, tid):
        u = st.users.get(tid)
        if u and u["admin_level"] == ADMIN:
            u["admin_level"] = 0
            return True
        return False

    async def remove_user(pool, tid):
        u = st.users.get(tid)
        if u and u["admin_level"] < OWNER:
            del st.users[tid]
            return True
        return False

    async def list_users(pool):
        return [
            {"telegram_id": tid, **u}
            for tid, u in sorted(st.users.items(), key=lambda kv: -kv[1]["admin_level"])
        ]

    async def log_change(pool, actor, text):
        st.changes.append((actor, text))

    async def add_grant(uid, scope, by, hours=None):
        st.grants[(uid, scope)] = hours

    async def revoke_grant(uid, scope=None):
        keys = [k for k in st.grants if k[0] == uid and (scope is None or k[1] == scope)]
        for k in keys:
            del st.grants[k]
        return len(keys)

    async def list_grants(uid):
        expires = datetime(2026, 11, 20, 3, 0, tzinfo=timezone.utc)
        return [
            {"scope": sc, "granted_by": "api:hr", "created_at": expires, "expires_at": (expires if hrs else None)}
            for (u, sc), hrs in sorted(st.grants.items()) if u == uid
        ]

    async def get_grants(uid):
        return {sc for (u, sc) in st.grants if u == str(uid)}

    async def purge_sessions(graph, pool, tid):
        if st.purge_error:
            raise RuntimeError("purge failed")
        st.purged.append(tid)
        return 2

    async def ask(message, user, graph, pool, text):
        st.asked.append((user.telegram_id, text))

    monkeypatch.setattr(auth, "get_user", get_user)
    monkeypatch.setattr(auth, "_admin_count", admin_count)
    monkeypatch.setattr(grants_mod, "get_grants", get_grants)
    for name, fn in {
        "get_user": get_user, "upsert_user": upsert_user, "demote_admin": demote_admin,
        "remove_user": remove_user, "list_users": list_users, "log_change": log_change,
        "add_grant": add_grant, "revoke_grant": revoke_grant, "list_grants": list_grants,
        "purge_user_sessions": purge_sessions, "_ask": ask,
    }.items():
        monkeypatch.setattr(h, name, fn)
    # Cấu hình báo cáo sáng: chưa cấu hình (không ảnh hưởng phần lớn test)
    monkeypatch.setattr(h, "get_store", lambda: SimpleNamespace(get=lambda: None))
    return st


# ================================================================ /start, /help theo role
@pytest.mark.parametrize("tid,report,grant,admin", [
    (BOSS, False, True, True),        # boss chủ: cấp quyền + quản trị, không có báo cáo sáng
    (BOSS2, False, True, False),      # boss thường: chỉ cấp quyền
    (MGR, True, True, False),         # manager: báo cáo sáng + cấp quyền
    (MGR_ADMIN, True, True, True),    # manager admin: đủ cả ba
    (STAFF_A, False, False, False),   # staff: chỉ phần chung
])
def test_help_sections_by_role(db, tid, report, grant, admin):
    for fn in (h.cmd_start, h.cmd_help):
        m = FakeMessage()
        run(fn(m, as_user(tid)))
        t = m.last
        assert "/tuan" in t and "/sessions" in t and "/quyen" in t   # phần chung ai cũng thấy
        assert ("/baocao" in t) is report
        assert ("/reply" in t) is report
        assert ("/capquyen" in t) is grant and ("/thuhoi" in t) is grant and ("/quyen <id>" in t) is grant
        if grant:
            assert "theo khối (coffee)" in t      # liệt kê đúng các khối đang cấu hình
        assert ("/adduser" in t) is admin and ("/removeuser" in t) is admin


def test_start_greets_by_name(db):
    m = FakeMessage()
    run(h.cmd_start(m, as_user(BOSS)))
    assert m.last.startswith("Xin chào Anh Chủ!")
    m2 = FakeMessage()
    run(h.cmd_start(m2, AllowedUser(5, "boss", STAFF, None)))
    assert m2.last.startswith("Xin chào!")   # không có tên thì không thừa dấu cách


# ================================================================ /tuan /thang /dashboard
SHORTCUTS = [
    (h.cmd_week, h.PROMPT_WEEK),
    (h.cmd_month, h.PROMPT_MONTH),
    (h.cmd_dashboard, h.PROMPT_DASHBOARD),
]


@pytest.mark.parametrize("fn,prompt", SHORTCUTS)
@pytest.mark.parametrize("tid", [BOSS, BOSS2, MGR, MGR_ADMIN])
def test_shortcuts_send_fixed_prompt_for_boss_and_manager(db, fn, prompt, tid):
    m = FakeMessage()
    run(fn(m, as_user(tid), None, None))
    assert db.asked == [(tid, prompt)]
    assert m.sent == []          # câu trả lời do _ask gửi, handler không tự trả lời


@pytest.mark.parametrize("fn,prompt", SHORTCUTS)
def test_shortcuts_refused_for_staff_without_any_scope(db, fn, prompt):
    m = FakeMessage()
    run(fn(m, as_user(STAFF_A), None, None))
    assert m.last == h.MSG_NO_SCOPE.format(scopes="coffee") and db.asked == []
    assert "coffee" in m.last and "house" not in m.last


@pytest.mark.parametrize("fn,prompt", SHORTCUTS)
def test_shortcuts_work_for_staff_with_coffee(db, fn, prompt):
    db.grants = {(str(STAFF_A), "coffee"): None}
    m = FakeMessage()
    run(fn(m, as_user(STAFF_A), None, None))
    assert db.asked == [(STAFF_A, prompt)] and m.sent == []


@pytest.mark.parametrize("fn,prompt", SHORTCUTS)
@pytest.mark.parametrize("scopes", [{"coffee", "house"}, {"coffee", "house", "misa"}])
def test_shortcuts_with_future_api_scopes(db, api_scopes_config, fn, prompt, scopes):
    db.grants = {(str(STAFF_A), sc): None for sc in scopes}
    m = FakeMessage()
    run(fn(m, as_user(STAFF_A), None, None))
    assert db.asked == [(STAFF_A, prompt)] and m.sent == []


@pytest.mark.parametrize("scopes", [{"house"}, {"misa"}, {"house", "misa"}])
def test_staff_with_only_api_scopes(db, api_scopes_config, scopes):
    # /tuan, /thang chạy (agent lấy số qua API); dashboard cần dữ liệu database nên báo rõ
    db.grants = {(str(STAFF_A), sc): None for sc in scopes}
    m = FakeMessage()
    run(h.cmd_week(m, as_user(STAFF_A), None, None))
    run(h.cmd_month(m, as_user(STAFF_A), None, None))
    assert db.asked == [(STAFF_A, h.PROMPT_WEEK), (STAFF_A, h.PROMPT_MONTH)]
    run(h.cmd_dashboard(m, as_user(STAFF_A), None, None))
    assert "coffee" in m.last and "/tuan" in m.last and len(db.asked) == 2


def test_staff_scope_of_other_user_does_not_count(db):
    db.grants = {(str(STAFF_B), "coffee"): None}
    m = FakeMessage()
    run(h.cmd_week(m, as_user(STAFF_A), None, None))
    assert m.last == h.MSG_NO_SCOPE.format(scopes="coffee") and db.asked == []


def test_old_grant_of_scope_not_in_config_does_not_count(db):
    # misa chưa có trong cấu hình hiện tại: quyền misa cấp từ trước không mở /tuan
    db.grants = {(str(STAFF_A), "misa"): None}
    m = FakeMessage()
    run(h.cmd_week(m, as_user(STAFF_A), None, None))
    assert m.last.startswith("Bạn chưa được cấp") and db.asked == []


def test_shortcut_prompts_mention_period():
    assert "tuần này" in h.PROMPT_WEEK and "tuần trước" in h.PROMPT_WEEK
    assert "tháng này" in h.PROMPT_MONTH and "tháng trước" in h.PROMPT_MONTH
    assert "dashboard" in h.PROMPT_DASHBOARD.lower()


# ================================================================ /capquyen (theo khối)
@pytest.mark.parametrize("tid", [BOSS, BOSS2, MGR, MGR_ADMIN])
def test_capquyen_coffee_with_hours(db, tid):
    m = FakeMessage()
    run(h.cmd_capquyen(m, cmd(f"{STAFF_A} coffee 48"), as_user(tid), None))
    assert db.grants == {(str(STAFF_A), "coffee"): 48}
    assert m.last == f"Đã cấp cho {STAFF_A} quyền xem khối coffee (Nyna Coffee (iPOS)) trong 48 giờ."
    assert db.changes == [(tid, f"grant {STAFF_A} scope=coffee hours=48")]


def test_capquyen_unlimited_uppercase_and_regrant(db):
    m = FakeMessage()
    run(h.cmd_capquyen(m, cmd(f"{STAFF_A} COFFEE 24"), as_user(BOSS), None))
    run(h.cmd_capquyen(m, cmd(f"{STAFF_A} coffee"), as_user(MGR), None))
    assert db.grants == {(str(STAFF_A), "coffee"): None}
    assert m.last.endswith("không giới hạn thời gian.")


def test_capquyen_grant_makes_shortcut_work_immediately(db):
    m = FakeMessage()
    run(h.cmd_week(m, as_user(STAFF_A), None, None))
    assert db.asked == []
    run(h.cmd_capquyen(m, cmd(f"{STAFF_A} coffee"), as_user(MGR), None))
    run(h.cmd_week(m, as_user(STAFF_A), None, None))
    assert db.asked == [(STAFF_A, h.PROMPT_WEEK)]


@pytest.mark.parametrize("scope", ["house", "misa", "base", "dwh.fact_sales", "all", "*"])
def test_capquyen_unknown_scope_today(db, scope):
    # Hiện chỉ có khối coffee: house/misa/base chưa có dữ liệu nên chưa cấp được; tên bảng kiểu cũ cũng không
    m = FakeMessage()
    run(h.cmd_capquyen(m, cmd(f"{STAFF_A} {scope}"), as_user(BOSS), None))
    assert m.last == f"Không có khối '{scope.lower()}'. Các khối: coffee." and db.grants == {}


def test_capquyen_future_api_scope(db, api_scopes_config):
    m = FakeMessage()
    run(h.cmd_capquyen(m, cmd(f"{STAFF_A} misa 8"), as_user(MGR), None))
    assert db.grants == {(str(STAFF_A), "misa"): 8} and "khối misa (Kế toán MISA)" in m.last


@pytest.mark.parametrize("args", [None, "", f"{STAFF_A}", "abc coffee", f"{STAFF_A} coffee 1 2"])
def test_capquyen_usage(db, args):
    m = FakeMessage()
    run(h.cmd_capquyen(m, cmd(args), as_user(BOSS), None))
    assert m.last.startswith("Cú pháp: /capquyen") and "Khối: coffee" in m.last and db.grants == {}


@pytest.mark.parametrize("hours", ["0", "8761", "abc", "-5", "1.5"])
def test_capquyen_bad_hours(db, hours):
    m = FakeMessage()
    run(h.cmd_capquyen(m, cmd(f"{STAFF_A} coffee {hours}"), as_user(BOSS), None))
    assert "Số giờ phải từ 1 đến" in m.last and db.grants == {}


@pytest.mark.parametrize("target", [MGR, BOSS2, STRANGER])
def test_capquyen_only_for_staff_targets(db, target):
    m = FakeMessage()
    run(h.cmd_capquyen(m, cmd(f"{target} coffee"), as_user(BOSS), None))
    assert m.last.startswith("Chỉ cấp quyền cho người dùng có role staff") and db.grants == {}


def test_capquyen_cannot_grant_scope_grantor_lacks(db, monkeypatch, api_scopes_config):
    # Chống leo thang: nếu role của người cấp không có khối đó thì không cấp được
    from app.agent import scopes
    cfg = scopes.parse_scopes_config({**__import__("tests.conftest", fromlist=["x"]).API_SCOPES_DATA,
                                      "role_scopes": {"boss": ["*"], "manager": ["coffee"], "staff": []}})
    monkeypatch.setattr(scopes, "load_scopes_config", lambda *a, **k: cfg)
    monkeypatch.setattr(h, "load_scopes_config", lambda *a, **k: cfg)
    m = FakeMessage()
    run(h.cmd_capquyen(m, cmd(f"{STAFF_A} misa"), as_user(MGR), None))
    assert "không có quyền xem khối này" in m.last and db.grants == {}
    run(h.cmd_capquyen(m, cmd(f"{STAFF_A} misa"), as_user(BOSS), None))
    assert db.grants == {(str(STAFF_A), "misa"): None}


def test_capquyen_silent_for_staff(db):
    m = FakeMessage()
    run(h.cmd_capquyen(m, cmd(f"{STAFF_B} coffee"), as_user(STAFF_A), None))
    assert m.sent == [] and db.grants == {}


# ================================================================ /thuhoi
def test_thuhoi_one_scope_and_all(db, api_scopes_config):
    db.grants = {(str(STAFF_A), "coffee"): 5, (str(STAFF_A), "misa"): None, (str(STAFF_B), "coffee"): None}
    m = FakeMessage()
    run(h.cmd_thuhoi(m, cmd(f"{STAFF_A} COFFEE"), as_user(MGR), None))
    assert m.last == f"Đã thu hồi 1 quyền của {STAFF_A}." and (str(STAFF_A), "misa") in db.grants
    run(h.cmd_thuhoi(m, cmd(f"{STAFF_A} all"), as_user(BOSS), None))
    assert m.last == f"Đã thu hồi 1 quyền của {STAFF_A}."
    assert list(db.grants) == [(str(STAFF_B), "coffee")]     # người khác không bị đụng
    assert [c[1] for c in db.changes] == [
        f"revoke {STAFF_A} scope=coffee (1 quyền)", f"revoke {STAFF_A} scope=all (1 quyền)"]


def test_thuhoi_removes_access_immediately(db):
    db.grants = {(str(STAFF_A), "coffee"): None}
    m = FakeMessage()
    run(h.cmd_thuhoi(m, cmd(f"{STAFF_A} coffee"), as_user(BOSS), None))
    run(h.cmd_week(m, as_user(STAFF_A), None, None))
    assert m.last.startswith("Bạn chưa được cấp") and db.asked == []


def test_thuhoi_nothing_and_usage(db):
    m = FakeMessage()
    run(h.cmd_thuhoi(m, cmd(f"{STAFF_A} coffee"), as_user(BOSS), None))
    assert m.last == "Không có quyền nào khớp để thu hồi." and db.changes == []
    for bad in (None, f"{STAFF_A}", "abc all", f"{STAFF_A} a b"):
        run(h.cmd_thuhoi(m, cmd(bad), as_user(BOSS), None))
        assert m.last.startswith("Cú pháp: /thuhoi")


def test_thuhoi_silent_for_staff(db):
    db.grants = {(str(STAFF_B), "coffee"): None}
    m = FakeMessage()
    run(h.cmd_thuhoi(m, cmd(f"{STAFF_B} all"), as_user(STAFF_A), None))
    assert m.sent == [] and db.grants


# ================================================================ /quyen
@pytest.mark.parametrize("tid,role", [(BOSS, "boss"), (MGR, "manager")])
def test_quyen_self_for_boss_and_manager(db, tid, role):
    m = FakeMessage()
    run(h.cmd_quyen(m, cmd(), as_user(tid), None))
    assert m.last == f"Bạn - role {role}, xem theo vai trò: coffee (Nyna Coffee (iPOS))"


def test_quyen_self_for_boss_with_future_scopes(db, api_scopes_config):
    m = FakeMessage()
    run(h.cmd_quyen(m, cmd(), as_user(BOSS), None))
    assert "coffee" in m.last and "house (Nyna House)" in m.last and "misa (Kế toán MISA)" in m.last


def test_quyen_of_staff_lists_scopes(db, api_scopes_config):
    db.grants = {(str(STAFF_A), "coffee"): 48, (str(STAFF_A), "house"): None}
    m = FakeMessage()
    run(h.cmd_quyen(m, cmd(str(STAFF_A)), as_user(MGR), None))
    assert m.last.startswith(f"{STAFF_A} (NV A) được xem các khối:")
    assert "• coffee - Nyna Coffee (iPOS) (hết hạn 20/11 10:00)" in m.last   # giờ VN (UTC+7)
    assert "• house - Nyna House (không giới hạn)" in m.last
    assert "misa" not in m.last


def test_quyen_flags_scope_removed_from_config(db):
    db.grants = {(str(STAFF_A), "kho_cu"): None}
    m = FakeMessage()
    run(h.cmd_quyen(m, cmd(str(STAFF_A)), as_user(BOSS), None))
    assert "kho_cu (khối không còn trong cấu hình)" in m.last


def test_quyen_of_staff_without_scopes(db):
    m = FakeMessage()
    run(h.cmd_quyen(m, cmd(str(STAFF_B)), as_user(BOSS), None))
    assert m.last == f"{STAFF_B} (NV B) chưa được cấp khối dữ liệu nào."


def test_quyen_of_other_roles_unknown_and_bad_arg(db):
    m = FakeMessage()
    run(h.cmd_quyen(m, cmd(str(MGR)), as_user(BOSS), None))
    assert m.last.startswith(f"{MGR} (Chị Quản lý) - role manager, xem theo vai trò:")
    run(h.cmd_quyen(m, cmd(str(STRANGER)), as_user(BOSS), None))
    assert m.last == "Không có người dùng này."
    run(h.cmd_quyen(m, cmd("abc"), as_user(BOSS), None))
    assert m.last == "Cú pháp: /quyen [telegram_id]"


def test_quyen_staff_sees_own_but_not_others(db):
    db.grants = {(str(STAFF_A), "coffee"): None}
    m = FakeMessage()
    run(h.cmd_quyen(m, cmd(), as_user(STAFF_A), None))
    assert m.last == "Bạn được xem các khối:\n• coffee - Nyna Coffee (iPOS) (không giới hạn)"
    m2 = FakeMessage()
    run(h.cmd_quyen(m2, cmd(str(STAFF_B)), as_user(STAFF_A), None))
    assert m2.sent == []


# ================================================================ lệnh quản trị
ADMIN_CMDS = [
    (h.cmd_users, False, False),
    (h.cmd_adduser, True, False),
    (h.cmd_addadmin, True, False),
    (h.cmd_removeadmin, True, False),
    (h.cmd_removeuser, True, True),
]


@pytest.mark.parametrize("fn,has_cmd,has_graph", ADMIN_CMDS)
@pytest.mark.parametrize("tid", [BOSS2, MGR, STAFF_A])
def test_admin_commands_silent_for_non_admin(db, fn, has_cmd, has_graph, tid):
    before = {k: dict(v) for k, v in db.users.items()}
    m = FakeMessage()
    args = [m] + ([cmd(f"{STAFF_A} manager")] if has_cmd else []) + [as_user(tid)]
    args += [None, None] if has_graph else [None]
    run(fn(*args))
    assert m.sent == [] and db.users == before and db.changes == []


def test_users_list_for_owner(db):
    m = FakeMessage()
    run(h.cmd_users(m, as_user(BOSS), None))
    lines = m.last.split("\n")
    assert lines[0] == f"{BOSS} | boss (chủ) | Anh Chủ"
    assert f"{MGR_ADMIN} | manager (admin) | Anh QL Admin" in lines
    assert f"{STAFF_A} | staff | NV A" in lines


def test_adduser_new_staff_and_change_role(db):
    m = FakeMessage()
    run(h.cmd_adduser(m, cmd("3003 staff Nhân viên mới"), as_user(BOSS), None))
    assert db.users[3003] == {"role": "staff", "admin_level": 0, "name": "Nhân viên mới"}
    assert m.last == "Đã lưu 3003: role staff."
    run(h.cmd_adduser(m, cmd(f"{STAFF_A} MANAGER"), as_user(MGR_ADMIN), None))
    assert db.users[STAFF_A]["role"] == "manager" and db.users[STAFF_A]["name"] == "NV A"
    assert db.changes[-1] == (MGR_ADMIN, f"set {STAFF_A} role=manager level=None")


@pytest.mark.parametrize("args", [None, "3003", "abc staff", "3003"])
def test_adduser_usage(db, args):
    m = FakeMessage()
    run(h.cmd_adduser(m, cmd(args), as_user(BOSS), None))
    assert m.last == "Cú pháp: /adduser <telegram_id> <role> [tên]"


def test_adduser_invalid_role(db):
    m = FakeMessage()
    run(h.cmd_adduser(m, cmd("3003 giamdoc"), as_user(BOSS), None))
    assert m.last == "Role không hợp lệ. Các role: boss, manager, staff" and 3003 not in db.users


def test_adduser_cannot_edit_self_or_owner(db):
    m = FakeMessage()
    run(h.cmd_adduser(m, cmd(f"{MGR_ADMIN} staff"), as_user(MGR_ADMIN), None))
    assert m.last == "Không tự sửa hoặc tự xóa chính mình qua bot."
    run(h.cmd_adduser(m, cmd(f"{BOSS} staff"), as_user(MGR_ADMIN), None))
    assert m.last == "Không thể tác động lên chủ chatbot."
    assert db.users[BOSS]["role"] == "boss" and db.users[MGR_ADMIN]["role"] == "manager"


def test_role_escalation_rule_follows_role_tables(monkeypatch):
    # Luật chống leo thang so tập bảng: actor không cấp được role xem rộng hơn mình.
    monkeypatch.setitem(h.ROLE_TABLES, "auditor", {"*"})
    assert auth._role_allowed("manager", "staff")
    assert not auth._role_allowed("manager", "auditor")
    # LƯU Ý: boss và manager hiện cùng tập {"dwh.*"} nên manager-admin tạo/nâng được người lên boss.
    # Nếu muốn chặn, phải thêm luật thứ bậc role trong auth._role_allowed (test này ghi lại hành vi hiện tại).
    assert auth._role_allowed("manager", "boss")


def test_addadmin_and_limit(db):
    m = FakeMessage()
    run(h.cmd_addadmin(m, cmd(f"{MGR} manager"), as_user(BOSS), None))
    assert db.users[MGR]["admin_level"] == ADMIN and m.last.endswith("cấp admin.")
    db.admin_count_override = auth.MAX_ADMINS
    run(h.cmd_addadmin(m, cmd(f"{STAFF_A} staff"), as_user(BOSS), None))
    assert m.last == f"Đã đủ {auth.MAX_ADMINS} admin. Hãy gỡ bớt trước khi thêm."
    assert db.users[STAFF_A]["admin_level"] == 0
    # người đã là admin thì đổi role vẫn được dù đủ số admin
    run(h.cmd_addadmin(m, cmd(f"{MGR} boss"), as_user(BOSS), None))
    assert db.users[MGR]["role"] == "boss"


def test_removeadmin(db):
    m = FakeMessage()
    run(h.cmd_removeadmin(m, cmd(str(MGR_ADMIN)), as_user(BOSS), None))
    assert m.last == "Đã gỡ quyền admin." and db.users[MGR_ADMIN]["admin_level"] == 0
    assert db.changes == [(BOSS, f"demote {MGR_ADMIN}")]
    run(h.cmd_removeadmin(m, cmd(str(MGR)), as_user(BOSS), None))
    assert m.last == "Người này không phải admin."
    run(h.cmd_removeadmin(m, cmd("x"), as_user(BOSS), None))
    assert m.last == "Cú pháp: /removeadmin <telegram_id>"


def test_removeadmin_cannot_touch_owner(db):
    m = FakeMessage()
    run(h.cmd_removeadmin(m, cmd(str(BOSS)), as_user(MGR_ADMIN), None))
    assert m.last == "Không thể tác động lên chủ chatbot." and db.users[BOSS]["admin_level"] == OWNER


def test_removeuser_cleans_grants_and_sessions(db):
    db.grants = {(str(STAFF_A), "coffee"): None, (str(STAFF_A), "house"): 5, (str(STAFF_B), "coffee"): None}
    m = FakeMessage()
    run(h.cmd_removeuser(m, cmd(str(STAFF_A)), as_user(BOSS), None, None))
    assert m.last == "Đã xóa." and STAFF_A not in db.users
    assert list(db.grants) == [(str(STAFF_B), "coffee")]
    assert db.purged == [STAFF_A]
    assert db.changes == [(BOSS, f"remove {STAFF_A} (xoá 2 phiên)")]


def test_removeuser_errors(db):
    m = FakeMessage()
    run(h.cmd_removeuser(m, cmd(str(STRANGER)), as_user(BOSS), None, None))
    assert m.last == "Không có người dùng này."
    run(h.cmd_removeuser(m, cmd(str(BOSS)), as_user(MGR_ADMIN), None, None))
    assert m.last == "Không thể tác động lên chủ chatbot." and BOSS in db.users
    run(h.cmd_removeuser(m, cmd(str(MGR_ADMIN)), as_user(MGR_ADMIN), None, None))
    assert m.last == "Không tự sửa hoặc tự xóa chính mình qua bot."
    run(h.cmd_removeuser(m, cmd(""), as_user(BOSS), None, None))
    assert m.last == "Cú pháp: /removeuser <telegram_id>"


def test_removeuser_when_purge_fails(db):
    db.purge_error = True
    m = FakeMessage()
    run(h.cmd_removeuser(m, cmd(str(STAFF_B)), as_user(BOSS), None, None))
    assert STAFF_B not in db.users and "chưa xoá hết hội thoại" in m.last


# ================================================================ lệnh báo cáo sáng: boss bị nhắc
@pytest.mark.parametrize("call", [
    lambda m, u: h.cmd_baocao(m, u, None),
    lambda m, u: h.cmd_baocao_tat(m, u, None),
    lambda m, u: h.cmd_baocao_bat(m, u, None),
    lambda m, u: h.cmd_baocao_gio(m, cmd("07:30"), u, None),
    lambda m, u: h.cmd_reply(m, cmd("doanh thu theo kênh"), u, None, None),
])
@pytest.mark.parametrize("tid", [BOSS, BOSS2, STAFF_A])
def test_report_commands_are_manager_only(db, call, tid):
    m = FakeMessage()
    run(call(m, as_user(tid)))
    assert m.last == h.MSG_NOT_MANAGER and db.asked == []


@pytest.mark.parametrize("tid", [MGR, MGR_ADMIN])
def test_report_commands_for_manager_without_config(db, tid):
    # reports.yaml chưa có: manager được báo rõ, không bị nhắc "không dành cho bạn"
    m = FakeMessage()
    run(h.cmd_baocao(m, as_user(tid), None))
    assert m.last == "Báo cáo tự động chưa được cấu hình."
    run(h.cmd_baocao_tat(m, as_user(tid), None))
    assert m.last == h.MSG_NOT_REGISTERED


# ================================================================ middleware
@pytest.fixture()
def mw_users(monkeypatch):
    async def get_allowed(pool, tid):
        return as_user(tid) if tid in SEED else None
    monkeypatch.setattr(h, "get_allowed_user", get_allowed)


def _through_middleware(msg):
    seen = {}

    async def handler(event, data):
        seen["user"] = data.get("user", "unset")
        return "handled"

    out = run(h.AllowlistMiddleware()(handler, msg, {"pool": None}))
    return out, seen


@pytest.mark.parametrize("tid", [BOSS, MGR])
def test_middleware_lets_boss_and_manager_through(mw_users, tid):
    out, seen = _through_middleware(FakeMessage("doanh thu hôm qua", uid=tid))
    assert out == "handled" and seen["user"].telegram_id == tid


def test_middleware_ignores_stranger_group_and_stale(mw_users):
    assert _through_middleware(FakeMessage("hi", uid=STRANGER)) == (None, {})
    assert _through_middleware(FakeMessage("hi", uid=BOSS, chat_type=ChatType.GROUP)) == (None, {})
    stale = FakeMessage("hi", uid=BOSS, age_seconds=h.STALE_AFTER_SECONDS + 5)
    assert _through_middleware(stale) == (None, {})


@pytest.mark.parametrize("text", ["/myid", "/myid@nyna_bot", "/MYID abc"])
def test_middleware_allows_myid_for_stranger(mw_users, text):
    out, seen = _through_middleware(FakeMessage(text, uid=STRANGER))
    assert out == "handled" and seen["user"] is None


def test_middleware_db_error_is_silent(monkeypatch):
    async def boom(pool, tid):
        raise RuntimeError("db down")
    monkeypatch.setattr(h, "get_allowed_user", boom)
    assert _through_middleware(FakeMessage("hi", uid=BOSS)) == (None, {})


def test_myid_reply():
    m = FakeMessage("/myid", uid=STRANGER)
    run(h.cmd_myid(m))
    assert m.last == f"Telegram ID của bạn: {STRANGER}"


# ================================================================ _ask: bận, rate limit, audit
@pytest.fixture()
def ask_env(monkeypatch):
    st = SimpleNamespace(busy=False, blocked=None, audits=[], sent=[], turns=[])

    async def peek(pool, uid): return "tg:1:abc"
    async def run_turn(graph, pool, **kw):
        st.turns.append(kw)
        return SimpleNamespace(ok=True, busy=False, session_reset=False, answer="Doanh thu: 1.000.000 ₫",
                               thread="tg:1:abc")
    async def send(message, text): st.sent.append(text)
    async def audit(pool, **kw): st.audits.append(kw)

    @asynccontextmanager
    async def no_progress(message):
        yield

    monkeypatch.setattr(h, "peek_active", peek)
    monkeypatch.setattr(h, "is_busy", lambda t: st.busy)
    monkeypatch.setattr(h, "get_limiter", lambda: SimpleNamespace(hit=lambda uid: st.blocked))
    monkeypatch.setattr(h, "run_turn", run_turn)
    monkeypatch.setattr(h, "send_text", send)
    monkeypatch.setattr(h, "log_turn", audit)
    monkeypatch.setattr(h, "progress", no_progress)
    return st


@pytest.mark.parametrize("tid", [BOSS, MGR])
def test_ask_passes_server_role_and_audits(ask_env, tid):
    run(h._ask(FakeMessage(), as_user(tid), None, None, "doanh thu hôm qua"))
    assert ask_env.turns == [{"user_id": str(tid), "role": SEED[tid][0], "message": "doanh thu hôm qua"}]
    assert ask_env.sent == ["Doanh thu: 1.000.000 ₫"]
    a = ask_env.audits[0]
    assert a["user_id"] == str(tid) and a["role"] == SEED[tid][0] and a["status"] == "ok"


def test_ask_busy_and_rate_limited(ask_env):
    ask_env.busy = True
    m = FakeMessage()
    run(h._ask(m, as_user(BOSS), None, None, "x"))
    assert m.last == h.MSG_BUSY and ask_env.turns == []
    ask_env.busy, ask_env.blocked = False, "Bạn hỏi nhanh quá, đợi 1 phút nhé."
    run(h._ask(m, as_user(BOSS), None, None, "x"))
    assert m.last == "Bạn hỏi nhanh quá, đợi 1 phút nhé." and ask_env.turns == [] and ask_env.audits == []


def test_unknown_command_reply():
    m = FakeMessage("/abc")
    run(h.on_unknown_command(m, as_user(BOSS)))
    assert "Gõ /help" in m.last


def test_router_registers_all_boss_manager_commands():
    router = h.build_router()
    commands = set()
    for handler in router.message.handlers:
        for f in handler.filters or []:
            cmds = getattr(f.callback, "commands", None)
            if cmds:
                commands |= {getattr(c, "command", c) for c in cmds}
    expected = {"tuan", "thang", "dashboard", "capquyen", "thuhoi", "quyen", "users", "adduser",
                "addadmin", "removeadmin", "removeuser", "baocao", "baocao_tat", "baocao_bat",
                "baocao_gio", "reply", "new", "sessions", "help", "myid"}
    assert expected <= commands
