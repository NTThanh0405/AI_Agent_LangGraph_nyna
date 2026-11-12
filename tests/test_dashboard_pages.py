# tests/test_dashboard_pages.py
# create_dashboard theo trang có sẵn (pages + kỳ) và theo cách tự khai báo (dashboards).
# Dùng pool giả: không cần PostgreSQL, nhưng câu SQL vẫn đi qua guard và kiểm tra quyền thật.

from datetime import date
from types import SimpleNamespace

import pytest

from app.agent.contracts import AnalysisBrief
from app.agent.permissions import can_access
from app.agent.tools import dashboard_presets as P
from app.agent.tools import dashboard_tools as T
from app.db.guard import check_select

MANAGER = {"configurable": {"user_id": "1", "user_role": "manager"}}
STAFF = {"configurable": {"user_id": "2", "user_role": "staff"}}


# ---------------------------------------------------------------- pool giả

def _universal_row() -> tuple[list[str], list]:
    """Một dòng có đủ mọi cột mà các trang cần (cột thừa không sao)."""
    cols: list[str] = []
    for n in P.PAGE_NAMES:
        for c in P.required_columns(n):
            if c not in cols:
                cols.append(c)
    text = {"ky": "nay", "ngay_qd": date(2026, 11, 2), "gio": "08:00", "ca": "Sáng (5–10h)", "thu": "T2",
            "cua_hang": "</script><script>alert(1)</script>", "tuan": "02/11 – 08/11", "loai_dong": "Món chính"}
    row = [text.get(c, 1 if c.endswith("_stt") else (10 if c.startswith(("so_", "doanh", "gmv", "km_", "phut",
                                                                          "giam", "gia_von", "hoa")) else "x"))
           for c in cols]
    return cols, [row]


class _Cursor:
    def __init__(self, cols, rows):
        self.description = [SimpleNamespace(name=c) for c in cols]
        self._rows = rows

    async def fetchall(self):
        return self._rows


class _Conn:
    def __init__(self, calls):
        self.calls = calls

    async def execute(self, sql):
        self.calls.append(sql)
        if "fact_sales" in sql and "doanh_thu" not in sql:   # SQL tự khai báo tối giản trong test
            return _Cursor(["ngay", "doanh_thu"], [[date(2026, 11, 2), 100]])
        return _Cursor(*_universal_row())


class _Pool:
    def __init__(self):
        self.calls: list[str] = []

    def connection(self):
        pool = self

        class _Ctx:
            async def __aenter__(self):
                return _Conn(pool.calls)

            async def __aexit__(self, *a):
                return False
        return _Ctx()


@pytest.fixture
def env(tmp_path, monkeypatch):
    pool = _Pool()
    settings = SimpleNamespace(dashboard_base_url="https://dash.test", dashboard_dir=str(tmp_path),
                               dashboard_ttl_hours=24, dashboard_max_charts=6, dashboard_logo="")
    monkeypatch.setattr(T, "get_settings", lambda: settings)
    monkeypatch.setattr(T, "get_pool", lambda: pool)
    monkeypatch.setattr(T, "VN_TODAY", lambda: date(2026, 11, 12))
    return SimpleNamespace(pool=pool, dir=tmp_path)


async def _call(args, cfg=MANAGER):
    return await T.create_dashboard.ainvoke(args, config=cfg)


def _html(env) -> str:
    files = list(env.dir.glob("*.html"))
    assert len(files) == 1
    return files[0].read_text(encoding="utf-8")


# ---------------------------------------------------------------- SQL mẫu hợp lệ

@pytest.mark.parametrize("name", P.PAGE_NAMES)
def test_page_sql_passes_guard_and_manager_access(name):
    q = check_select(P.build_dataset_sql(name, "2026-11-02", "2026-11-08"), P.MAX_DATASET_ROWS)
    assert q.tables and all(can_access("manager", t) for t in q.tables)


def test_last_full_week_is_monday_to_sunday_before_today():
    assert P.last_full_week(date(2026, 11, 12)) == (date(2026, 11, 2), date(2026, 11, 8))  # thứ Năm
    assert P.last_full_week(date(2026, 11, 9)) == (date(2026, 11, 2), date(2026, 11, 8))   # thứ Hai
    assert P.last_full_week(date(2026, 11, 15)) == (date(2026, 11, 2), date(2026, 11, 8))  # Chủ nhật: tuần chưa xong


def test_brief_keeps_only_known_pages():
    b = AnalysisBrief(question="x", dashboard_pages=["Tong_Quan", "sai", "san_pham", "tong_quan", "custom"])
    assert b.dashboard_pages == ["tong_quan", "san_pham", "custom"]


# ---------------------------------------------------------------- trang có sẵn

@pytest.mark.asyncio
async def test_one_page_with_period(env):
    out = await _call({"pages": ["tong_quan"], "period_start": "2026-11-02", "period_end": "2026-11-08"})
    assert "https://dash.test/" in out and "02/11/2026–08/11/2026" in out and "26/10/2026–01/11/2026" in out
    html = _html(env)
    assert "Tổng quan hiệu suất FoodApp" in html
    assert "</script><script>alert(1)" not in html          # dữ liệu không phá được thẻ script


@pytest.mark.asyncio
async def test_empty_pages_and_period_build_full_report_last_week(env):
    out = await _call({})
    assert "5 trang" in out and "02/11/2026–08/11/2026" in out
    # tong_quan, cua_hang, hanh_vi dùng chung SQL -> 3 truy vấn cho 5 trang
    assert len(env.pool.calls) == 3


@pytest.mark.asyncio
async def test_unknown_page_is_reported(env):
    out = await _call({"pages": ["tong_quan", "abc"]})
    assert out.startswith("Không tạo được dashboard") and "abc" in out and not env.pool.calls


@pytest.mark.asyncio
async def test_period_too_long_is_reported(env):
    out = await _call({"pages": ["san_pham"], "period_start": "2026-01-01", "period_end": "2026-11-08"})
    assert "tối đa 92 ngày" in out and not env.pool.calls


@pytest.mark.asyncio
async def test_half_period_is_reported(env):
    out = await _call({"pages": ["tong_quan"], "period_start": "2026-11-02"})
    assert "period_end" in out


@pytest.mark.asyncio
async def test_staff_without_scope_is_denied(env):
    out = await _call({"pages": ["tong_quan"]}, STAFF)
    assert "từ chối" in out and not list(env.dir.glob("*.html"))


# ---------------------------------------------------------------- tự khai báo

@pytest.mark.asyncio
async def test_page_preset_through_dashboards_points_to_pages(env):
    out = await _call({"dashboards": [{"name": "X", "preset": "tong_quan", "dataset_sql": "SELECT 1"}]})
    assert "pages=['tong_quan']" in out


@pytest.mark.asyncio
async def test_cannot_mix_pages_and_dashboards(env):
    out = await _call({"pages": ["tong_quan"],
                       "dashboards": [{"name": "X", "dataset_sql": "SELECT 1",
                                       "charts": [{"title": "a", "type": "bar", "x": "a", "y": ["b"]}]}]})
    assert "MỘT cách" in out


@pytest.mark.asyncio
async def test_custom_default_dashboard_still_works(env):
    out = await _call({"title": "Doanh thu theo ngày", "dashboards": [{
        "name": "Ngày", "preset": "default",
        "dataset_sql": "SELECT tran_date AS ngay, SUM(final_amount) AS tien FROM dwh.fact_sales GROUP BY 1",
        "charts": [{"title": "Doanh thu", "type": "line", "x": "ngay", "y": ["doanh_thu"]}],
    }]})
    assert "Doanh thu theo ngày" in out and "https://dash.test/" in out
