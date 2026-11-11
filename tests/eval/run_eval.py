#!/usr/bin/env python
# tests/eval/run_eval.py
# Đánh giá chất lượng trả lời của agent bằng bộ câu hỏi mẫu (questions.yaml).
# CÓ GỌI Claude và PostgreSQL THẬT -> TỐN CHI PHÍ. Không chạy trong `pytest` (tên file không bắt đầu bằng test_).
#
# Mỗi câu hỏi được chạy qua graph v2 (supervisor -> analyst -> coder -> designer -> chat) và chấm:
#   - ĐÚNG/SAI câu trả lời: so với số liệu thật lấy từ file SQL trong app/agent/queries/, cụm bắt buộc,
#     cụm cấm, link dashboard, không bịa số tiền khi bị từ chối; tuỳ chọn thêm LLM chấm (--judge).
#   - ROUTE: các node đã chạy có đúng đường mong đợi không, intent và khoảng ngày trong brief có đúng không.
#   - CHI PHÍ / ĐỘ TRỄ: token từng lần gọi LLM (tách theo agent), quy ra USD theo bảng giá trong yaml.
#   - --compare: chạy thêm bản 1 agent (một LLM + toàn bộ tool, vòng ReAct như bản v1) để so sánh.
#
# Cách chạy (ở thư mục gốc repo, cần .env giống khi chạy bot: ANTHROPIC_API_KEY, DB_*, DASHBOARD_BASE_URL...):
#   python tests/eval/run_eval.py --dry-run                 # chỉ kiểm tra file yaml, KHÔNG tốn tiền
#   python tests/eval/run_eval.py --tag smoke               # chạy nhóm câu nhanh
#   python tests/eval/run_eval.py --case revenue_yesterday --case dashboard_week
#   python tests/eval/run_eval.py --compare --judge         # đầy đủ: so với bản 1 agent + LLM chấm
#   python tests/eval/run_eval.py --repeat 3 --fail-under 0.8   # chạy lặp để đo độ ổn định; dùng trong CI
# Kết quả chi tiết ghi vào tests/eval/results/eval-<thời điểm>.json (thư mục nên để trong .gitignore).
#
# Lưu ý:
#   - Câu hỏi dashboard tạo file HTML thật trong DASHBOARD_DIR (tự xoá theo DASHBOARD_TTL_HOURS).
#   - Case role staff chạy KHÔNG có quyền cấp riêng (không nối database agent_app) -> luôn bị từ chối.
#   - Graph dùng MemorySaver (RAM), không đụng bảng chat_sessions/checkpoint của bot đang chạy.

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import re
import statistics
import sys
import time
import unicodedata
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.chdir(ROOT)  # config/agents.yaml, .env, config/reports.yaml dùng đường dẫn tương đối

import yaml  # noqa: E402
from langchain_core.callbacks import AsyncCallbackHandler  # noqa: E402

HERE = Path(__file__).resolve().parent
DEFAULT_QUESTIONS = HERE / "questions.yaml"
RESULTS_DIR = HERE / "results"

ROUTES = ("smalltalk", "report", "other", "data", "visual", "any")
ROLES = ("boss", "manager", "staff")
EXPECT_KEYS = {
    "route", "intent", "period", "reference", "contains_any", "contains_all", "not_contains",
    "not_regex", "dashboard_link", "no_money", "tools", "judge",
}
# Tool bắt buộc của bản 1 agent, suy ra từ route nếu case không ghi `tools`
ROUTE_TOOLS = {"data": ["run_select_query"], "visual": ["create_dashboard"], "report": ["run_select_query"]}


# ================================================================ đọc và kiểm tra questions.yaml

@dataclass
class Case:
    id: str
    question: str
    role: str
    tags: list[str]
    setup: list[str]
    expect: dict


class SpecError(ValueError):
    pass


def _as_list(v) -> list:
    if v is None:
        return []
    return list(v) if isinstance(v, (list, tuple)) else [v]


def load_spec(path: Path) -> tuple[list[Case], dict]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    defaults = data.get("defaults") or {}
    pricing = data.get("pricing") or {}
    cases, seen, errors = [], set(), []
    for i, raw in enumerate(data.get("cases") or []):
        cid = str(raw.get("id") or f"#{i}")
        try:
            if cid in seen:
                raise SpecError("id bị trùng")
            seen.add(cid)
            q = str(raw.get("question") or "").strip()
            if not q:
                raise SpecError("thiếu question")
            role = str(raw.get("role") or defaults.get("role") or "boss").lower()
            if role not in ROLES:
                raise SpecError(f"role {role!r} không hợp lệ")
            exp = raw.get("expect") or {}
            unknown = set(exp) - EXPECT_KEYS
            if unknown:
                raise SpecError(f"khoá expect lạ: {sorted(unknown)}")
            routes = _as_list(exp.get("route", "any"))
            if not routes or any(r not in ROUTES for r in routes):
                raise SpecError(f"route phải thuộc {ROUTES}")
            ref = exp.get("reference")
            if ref is not None:
                if not isinstance(ref, dict) or not ref.get("query"):
                    raise SpecError("reference cần query")
                if not (ROOT / "app/agent/queries" / f"{ref['query']}.sql").exists():
                    raise SpecError(f"không có app/agent/queries/{ref['query']}.sql")
                if not exp.get("period"):
                    raise SpecError("reference cần period")
                if not (ref.get("numbers") or ref.get("texts")):
                    raise SpecError("reference cần numbers hoặc texts")
            for pat in _as_list(exp.get("not_regex")):
                re.compile(pat)
            cases.append(Case(cid, q, role, [str(t) for t in _as_list(raw.get("tags"))],
                              [str(s) for s in _as_list(raw.get("setup"))], exp))
        except (SpecError, re.error) as e:
            errors.append(f"case {cid}: {e}")
    if errors:
        raise SpecError("questions.yaml sai:\n  " + "\n  ".join(errors))
    return cases, pricing


# ================================================================ chuẩn hoá chữ và đọc số trong câu trả lời

def norm(text: str) -> str:
    """chữ thường, bỏ dấu, đ -> d, gộp khoảng trắng (so khớp không phân biệt dấu)."""
    t = unicodedata.normalize("NFD", (text or "").lower())
    t = "".join(c for c in t if unicodedata.category(c) != "Mn").replace("đ", "d")
    return re.sub(r"\s+", " ", t).strip()


_UNITS = {
    "ty": 1e9, "tỷ": 1e9, "tỉ": 1e9,
    "trieu": 1e6, "triệu": 1e6, "tr": 1e6,
    "nghin": 1e3, "nghìn": 1e3, "ngan": 1e3, "ngàn": 1e3, "k": 1e3,
}
_NUM_RE = re.compile(
    r"(?<![\w.,])(-?\d{1,3}(?:[.,]\d{3})+(?:,\d+)?|-?\d+(?:[.,]\d+)?)\s*"
    r"(tỷ|tỉ|ty|triệu|trieu|tr|nghìn|nghin|ngàn|ngan|k)?(?![\w])",
    re.IGNORECASE,
)
_MONEY_RE = re.compile(
    r"\d[\d.,]*\s*(₫|đồng|đ\b|vnđ|vnd|tỷ|tỉ|triệu|tr\b|nghìn|ngàn|k\b)", re.IGNORECASE
)


@dataclass(frozen=True)
class Num:
    value: float
    resolution: float  # bước làm tròn mà người viết đã dùng ("1,2 triệu" -> 100.000)
    raw: str


def _readings(body: str) -> list[tuple[float, int]]:
    """Mọi cách đọc hợp lý của một cụm số -> [(giá trị, số chữ số thập phân)].
    "1.234.567" -> 1234567 (kiểu Việt); "1,234,567" -> 1234567 (kiểu Anh); "1,2" / "1.2" -> 1.2;
    "1.234" có hai cách đọc (1234 hoặc 1,234) nên trả cả hai, bước so khớp chọn cách đúng."""
    sign = -1.0 if body.startswith("-") else 1.0
    s = body.lstrip("-")
    out: list[tuple[float, int]] = []
    m = re.fullmatch(r"(\d{1,3}(?:\.\d{3})+)(?:,(\d+))?", s)          # 1.234.567,5
    if m:
        out.append((float(m[1].replace(".", "") + (f".{m[2]}" if m[2] else "")), len(m[2] or "")))
    m = re.fullmatch(r"(\d{1,3}(?:,\d{3})+)(?:\.(\d+))?", s)          # 1,234,567.5
    if m:
        out.append((float(m[1].replace(",", "") + (f".{m[2]}" if m[2] else "")), len(m[2] or "")))
    m = re.fullmatch(r"(\d+)[.,](\d+)", s)                             # 1,2 | 12.5
    if m:
        out.append((float(f"{m[1]}.{m[2]}"), len(m[2])))
    if re.fullmatch(r"\d+", s):
        out.append((float(s), 0))
    return [(sign * v, d) for v, d in out]


def parse_numbers(text: str) -> list[Num]:
    out = []
    for m in _NUM_RE.finditer(text or ""):
        body, unit = m.group(1), (m.group(2) or "").lower()
        mult = _UNITS.get(unit, 1.0)
        for v, dec in _readings(body):
            out.append(Num(v * mult, (10 ** -dec) * mult, m.group(0).strip()))
    return out


def number_in_text(expected: float, nums: list[Num], tolerance: float) -> Optional[Num]:
    """Có số nào trong câu trả lời khớp `expected` không (theo sai số tương đối hoặc độ làm tròn)."""
    for n in nums:
        allowed = max(abs(expected) * tolerance, n.resolution / 2 + 1e-9, 0.5 if abs(expected) >= 1 else 0)
        if abs(n.value - expected) <= allowed:
            return n
    return None


def has_money(text: str) -> list[str]:
    return [m.group(0) for m in _MONEY_RE.finditer(text or "")]


# ================================================================ đếm token và chi phí

class UsageTracker(AsyncCallbackHandler):
    """Callback LangChain: ghi token mỗi lần gọi model, kèm node LangGraph (agent) đã gọi.
    Callback truyền qua config của graph và tự lan xuống lời gọi LLM bên trong từng node."""

    def __init__(self, label: str = ""):
        super().__init__()
        self.label = label
        self.calls: list[dict] = []
        self._node: dict = {}
        self._started: dict = {}

    async def on_chat_model_start(self, serialized, messages, *, run_id, metadata=None, **kw):
        self._node[run_id] = (metadata or {}).get("langgraph_node") or self.label or "?"
        self._started[run_id] = time.monotonic()

    async def on_llm_end(self, response, *, run_id, **kw):
        node = self._node.pop(run_id, self.label or "?")
        started = self._started.pop(run_id, None)
        for gens in response.generations:
            for g in gens:
                msg = getattr(g, "message", None)
                if msg is None:
                    continue
                um = getattr(msg, "usage_metadata", None) or {}
                det = um.get("input_token_details") or {}
                meta = getattr(msg, "response_metadata", None) or {}
                self.calls.append({
                    "node": node,
                    "model": meta.get("model_name") or meta.get("model") or "?",
                    "input": int(um.get("input_tokens") or 0),   # langchain: đã gồm cả token cache
                    "output": int(um.get("output_tokens") or 0),
                    "cache_read": int(det.get("cache_read") or 0),
                    "cache_write": int(det.get("cache_creation") or 0),
                    "seconds": round(time.monotonic() - started, 2) if started else None,
                })


def price_for(model: str, pricing: dict) -> Optional[dict]:
    best = None
    for key, p in pricing.items():
        if model.startswith(key) and (best is None or len(key) > len(best[0])):
            best = (key, p)
    return best[1] if best else None


def call_cost(call: dict, pricing: dict) -> Optional[float]:
    p = price_for(call["model"], pricing)
    if p is None:
        return None
    fresh = max(call["input"] - call["cache_read"] - call["cache_write"], 0)
    return (
        fresh * p["input"] + call["cache_write"] * p.get("cache_write", p["input"])
        + call["cache_read"] * p.get("cache_read", p["input"]) + call["output"] * p["output"]
    ) / 1e6


def summarize_usage(calls: list[dict], pricing: dict) -> dict:
    total, unknown, by_node = 0.0, False, {}
    for c in calls:
        cost = call_cost(c, pricing)
        unknown |= cost is None
        total += cost or 0.0
        n = by_node.setdefault(c["node"], {"calls": 0, "input": 0, "output": 0, "cache_read": 0, "cost_usd": 0.0})
        n["calls"] += 1
        n["input"] += c["input"]
        n["output"] += c["output"]
        n["cache_read"] += c["cache_read"]
        n["cost_usd"] = round(n["cost_usd"] + (cost or 0.0), 6)
    return {
        "llm_calls": len(calls),
        "input_tokens": sum(c["input"] for c in calls),
        "output_tokens": sum(c["output"] for c in calls),
        "cache_read_tokens": sum(c["cache_read"] for c in calls),
        "cost_usd": None if unknown and total == 0 else round(total, 6),
        "cost_incomplete": unknown,
        "by_node": by_node,
    }


# ================================================================ chạy một câu hỏi

@dataclass
class RunOutput:
    answer: str
    nodes: list[str] = field(default_factory=list)       # thứ tự node đã chạy (v2) / tool đã gọi (single)
    tools: list[str] = field(default_factory=list)
    brief: Optional[dict] = None
    latency_s: float = 0.0
    usage: list[dict] = field(default_factory=list)
    error: Optional[str] = None


def _settings():
    from app.config import get_settings
    return get_settings()


def _cfg(case: Case, thread: str, tracker) -> dict:
    from app.agent.models import recursion_limit
    return {
        "configurable": {"thread_id": thread, "user_id": f"eval-{case.role}", "user_role": case.role},
        "recursion_limit": recursion_limit(),
        "callbacks": [tracker],
    }


async def run_v2(graph, case: Case, thread: str) -> RunOutput:
    """Chạy giống app/agent/runner.run_turn (cùng state đầu vào, recursion_limit, timeout), nhưng không
    dùng bảng chat_sessions và ghi lại các node đã chạy."""
    from langchain_core.messages import HumanMessage
    from langgraph.errors import GraphRecursionError

    from app.agent import runner
    from app.agent.state import new_turn_state

    timeout = _settings().invoke_timeout_seconds
    tracker = UsageTracker()
    out = RunOutput(answer="")
    for i, text in enumerate([*case.setup, case.question]):
        is_main = i == len(case.setup)
        t = UsageTracker() if not is_main else tracker  # chỉ tính chi phí câu chính
        cfg = _cfg(case, thread, t)
        # giống runner.run_turn: báo cho LLM các khối dữ liệu người hỏi được xem (staff trong eval: không có)
        from app.agent.scopes import user_scopes
        cfg["configurable"]["user_scopes"] = sorted(await user_scopes(case.role, cfg["configurable"]["user_id"]))
        nodes: list[str] = []
        started = time.monotonic()
        try:
            async with asyncio.timeout(timeout):
                async for chunk in graph.astream(
                    new_turn_state(cfg["configurable"]["user_id"], case.role, HumanMessage(content=text)),
                    cfg, stream_mode="updates",
                ):
                    nodes.extend(k for k in chunk if not k.startswith("__"))
            state = (await graph.aget_state(cfg)).values
            answer = runner.extract_text(state["messages"][-1].content).strip()
            err = None if answer else "empty"
        except GraphRecursionError:
            answer, err, state = runner.MSG_TOO_COMPLEX, "recursion", {}
        except TimeoutError:
            answer, err, state = runner.MSG_TIMEOUT, "timeout", {}
        except Exception as e:  # noqa: BLE001
            answer, err, state = runner.MSG_ERROR, f"{type(e).__name__}: {e}", {}
        if not is_main:
            if err:
                return RunOutput(answer=answer, error=f"setup lỗi: {err}")
            continue
        out = RunOutput(
            answer=answer, nodes=nodes, brief=state.get("brief"),
            tools=[m.name for k in ("coder_messages", "designer_messages") for m in state.get(k) or []
                   if type(m).__name__ == "ToolMessage"],
            latency_s=round(time.monotonic() - started, 2), usage=tracker.calls, error=err,
        )
    return out


SINGLE_PREFACE = (
    "Trong cấu hình này bạn là agent DUY NHẤT: tự hiểu câu hỏi, tự gọi tool lấy số liệu / dựng dashboard, "
    "rồi tự viết câu trả lời cuối gửi người dùng. Bỏ qua mọi câu bên dưới nói rằng bạn không trả lời trực "
    "tiếp người dùng hoặc nói tới bản tóm tắt phân tích/agent khác. Tự đổi cụm thời gian ra ngày dựa vào "
    "ngày hôm nay (tuần bắt đầu thứ Hai, giờ Việt Nam)."
)


def _single_prompt() -> str:
    from app.agent.prompts import load_prompt
    ttl = str(_settings().dashboard_ttl_hours)
    parts = [load_prompt("supervisor").replace("{ttl_hours}", ttl), load_prompt("coder"), load_prompt("designer")]
    return SINGLE_PREFACE + "\n\n---\n\n" + "\n\n---\n\n".join(parts)


async def run_single(case: Case, model: str, max_steps: int) -> RunOutput:
    """Bản 1 agent (kiểu v1): một LLM, bind toàn bộ ALL_TOOLS, vòng ReAct, không có analyst/đường tắt."""
    from langchain_anthropic import ChatAnthropic
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    from app.agent import knowledge, runner
    from app.agent.nodes import _common as C
    from app.agent.nodes._worker import FORCE_FINISH
    from app.agent.tools import ALL_TOOLS

    s = _settings()
    by_name = {t.name: t for t in ALL_TOOLS}
    base_llm = ChatAnthropic(model=model, api_key=s.anthropic_api_key, max_tokens=4096,
                             timeout=60, max_retries=2)
    llm = base_llm.bind_tools(ALL_TOOLS)
    history: list = []
    out = RunOutput(answer="")
    for i, text in enumerate([*case.setup, case.question]):
        is_main = i == len(case.setup)
        tracker = UsageTracker(label="single")
        cfg = {"configurable": {"thread_id": f"single-{case.id}", "user_id": f"eval-{case.role}",
                                "user_role": case.role}, "callbacks": [tracker]}
        system = C.system_message("supervisor", cfg, [knowledge.business_context(), knowledge.glossary(),
                                                      knowledge.sql_examples()], base=_single_prompt())
        msgs = [system, *history, HumanMessage(content=text)]
        tools_used: list[str] = []
        started = time.monotonic()
        err = None
        try:
            async with asyncio.timeout(s.invoke_timeout_seconds):
                for step in range(max_steps + 1):
                    forced = step == max_steps
                    if forced:
                        msgs.append(HumanMessage(content=FORCE_FINISH))
                    resp = await (base_llm if forced else llm).ainvoke(msgs, cfg)
                    msgs.append(resp)
                    calls = getattr(resp, "tool_calls", None) or []
                    if not calls or forced:
                        break
                    for c in calls:
                        tools_used.append(c["name"])
                        tool = by_name.get(c["name"])
                        try:
                            res = await tool.ainvoke({"type": "tool_call", "id": c["id"], "name": c["name"],
                                                      "args": c.get("args") or {}}, cfg) if tool else \
                                f"Lỗi: không có tool '{c['name']}'."
                        except Exception as e:  # noqa: BLE001
                            res = f"Lỗi khi chạy tool: {e}"
                        if not isinstance(res, ToolMessage):
                            res = ToolMessage(content=str(res), name=c["name"], tool_call_id=c["id"])
                        msgs.append(res)
            answer = runner.extract_text(msgs[-1].content).strip()
            err = None if answer else "empty"
        except TimeoutError:
            answer, err = runner.MSG_TIMEOUT, "timeout"
        except Exception as e:  # noqa: BLE001
            answer, err = runner.MSG_ERROR, f"{type(e).__name__}: {e}"
        history += [HumanMessage(content=text), AIMessage(content=answer)]
        if not is_main:
            if err:
                return RunOutput(answer=answer, error=f"setup lỗi: {err}")
            continue
        out = RunOutput(answer=answer, nodes=["single"], tools=tools_used,
                        latency_s=round(time.monotonic() - started, 2), usage=tracker.calls, error=err)
    return out


# ================================================================ số liệu đúng (reference) và LLM chấm

_ref_cache: dict = {}


async def fetch_reference(query: str, period_text: str) -> list[dict]:
    """Chạy file SQL cố định (cùng định nghĩa với báo cáo sáng) bằng role boss để lấy số liệu đúng."""
    from app.agent.periods import resolve_period
    from app.agent.tools._queries import run_query_file

    key = (query, period_text, date.today())
    if key not in _ref_cache:
        period = resolve_period(period_text)
        _ref_cache[key] = await run_query_file(f"{query}.sql", period, role="boss", user_id=None)
    return _ref_cache[key]


_JUDGE_PROMPT = (
    "Bạn chấm câu trả lời của một trợ lý phân tích dữ liệu kinh doanh (tiếng Việt) trên Telegram.\n"
    "Chỉ dựa vào TIÊU CHÍ và (nếu có) SỐ LIỆU ĐÚNG bên dưới. Đạt khi câu trả lời thoả tiêu chí và "
    "không mâu thuẫn số liệu đúng; lỗi trình bày nhỏ không làm trượt. Lý do ngắn gọn, tiếng Việt.\n\n"
    "CÂU HỎI: {question}\nVAI TRÒ NGƯỜI HỎI: {role}\nTIÊU CHÍ: {criterion}\nSỐ LIỆU ĐÚNG: {reference}\n\n"
    "CÂU TRẢ LỜI CẦN CHẤM:\n<<<\n{answer}\n>>>"
)


async def judge(case: Case, answer: str, reference: Any, model: str, tracker) -> tuple[bool, str]:
    from langchain_anthropic import ChatAnthropic
    from pydantic import BaseModel

    class Verdict(BaseModel):
        passed: bool
        reason: str

    s = _settings()
    llm = ChatAnthropic(model=model, api_key=s.anthropic_api_key, max_tokens=400, timeout=60, max_retries=2)
    prompt = _JUDGE_PROMPT.format(
        question=case.question, role=case.role, criterion=case.expect["judge"],
        reference=json.dumps(reference, ensure_ascii=False, default=str)[:3000] if reference else "(không có)",
        answer=answer[:6000],
    )
    v = await llm.with_structured_output(Verdict).ainvoke(prompt, {"callbacks": [tracker]})
    return bool(v.passed), v.reason


# ================================================================ chấm điểm

@dataclass
class Check:
    name: str
    kind: str          # "answer" (đúng/sai câu trả lời) | "route" (đường đi, intent, kỳ)
    ok: Optional[bool]  # None = bỏ qua
    detail: str = ""


def route_of(nodes: list[str]) -> str:
    s = set(nodes)
    if "designer" in s:
        return "visual"
    if "coder" in s:
        return "data"
    if "analyst" in s:
        return "other"
    if "chat" in s:
        return "smalltalk"
    if s == {"supervisor"}:
        return "report"
    return "?"


def check_route(case: Case, out: RunOutput, variant: str) -> list[Check]:
    exp = case.expect
    routes = _as_list(exp.get("route", "any"))
    checks: list[Check] = []
    if variant == "single":  # không có route: chỉ xem có gọi đúng loại tool không
        used = sorted(set(out.tools))
        if exp.get("tools"):
            missing = [t for t in _as_list(exp["tools"]) if t not in out.tools]
            checks.append(Check("tools", "route", not missing, f"thiếu {missing}; đã gọi {used}"))
        elif "any" in routes or (set(routes) & {"smalltalk", "other"} and not set(routes) <= {"smalltalk", "other"}):
            pass  # route mong đợi lẫn có/không dữ liệu: không kết luận được
        elif set(routes) <= {"smalltalk", "other"}:
            data = sorted({"run_select_query", "create_dashboard"} & set(out.tools))
            checks.append(Check("tools", "route", not data, f"không cần tool dữ liệu; đã gọi {used}"))
        else:
            want = {t for r in routes for t in ROUTE_TOOLS.get(r, [])}
            checks.append(Check("tools", "route", bool(want & set(out.tools)),
                                f"cần một trong {sorted(want)}; đã gọi {used}"))
        return checks

    actual = route_of(out.nodes)
    if "any" not in routes:
        checks.append(Check("route", "route", actual in routes, f"{actual} (mong đợi {'/'.join(routes)}); "
                                                               f"nodes={'→'.join(out.nodes)}"))
    brief = out.brief or {}
    if exp.get("intent") and actual not in ("smalltalk", "report"):
        intents = _as_list(exp["intent"])
        checks.append(Check("intent", "route", brief.get("intent") in intents,
                            f"{brief.get('intent')} (mong đợi {'/'.join(intents)})"))
    if exp.get("period") and actual not in ("smalltalk", "report"):
        from app.agent.periods import PeriodError, resolve_period
        try:
            p = resolve_period(exp["period"])
            want = (p.start.isoformat(), p.end.isoformat())
            got = (brief.get("period_start"), brief.get("period_end"))
            checks.append(Check("period", "route", got == want, f"{got[0]}..{got[1]} (mong đợi {want[0]}..{want[1]})"))
        except PeriodError as e:
            checks.append(Check("period", "route", None, f"bỏ qua: {e}"))
    return checks


async def check_answer(case: Case, out: RunOutput, do_judge: bool, judge_model: str,
                       judge_tracker) -> tuple[list[Check], Any]:
    exp, ans = case.expect, out.answer
    n_ans = norm(ans)
    checks: list[Check] = []
    if out.error:
        checks.append(Check("no_error", "answer", False, out.error))

    reference = None
    ref = exp.get("reference")
    if ref:
        try:
            rows = await fetch_reference(ref["query"], exp["period"])
            reference = rows[: max(int(ref.get("rows", 1)), 1)]
        except Exception as e:  # noqa: BLE001
            checks.append(Check("reference", "answer", None, f"không lấy được số liệu đúng: {e}"))
        if reference is not None:
            if not reference:
                checks.append(Check("reference", "answer", None, "truy vấn đúng trả về 0 dòng (kỳ không có dữ liệu)"))
            nums = parse_numbers(ans)
            tol = float(ref.get("tolerance", 0.01))
            for r_i, row in enumerate(reference):
                for col in _as_list(ref.get("numbers")):
                    v = row.get(col)
                    if v is None:
                        checks.append(Check(f"{col}[{r_i}]", "answer", None, "số liệu đúng là NULL"))
                        continue
                    hit = number_in_text(float(v), nums, tol)
                    checks.append(Check(f"{col}[{r_i}]", "answer", hit is not None,
                                        f"đúng={v:,.2f}" + (f", thấy '{hit.raw}'" if hit else ", không thấy trong câu trả lời")))
                for col in _as_list(ref.get("texts")):
                    v = str(row.get(col) or "")
                    checks.append(Check(f"{col}[{r_i}]", "answer", bool(v) and norm(v) in n_ans, f"cần có '{v}'"))

    if exp.get("contains_any"):
        opts = _as_list(exp["contains_any"])
        hit = next((o for o in opts if norm(o) in n_ans), None)
        checks.append(Check("contains_any", "answer", hit is not None, f"thấy '{hit}'" if hit else f"thiếu cả {opts}"))
    for o in _as_list(exp.get("contains_all")):
        checks.append(Check(f"contains:{o}", "answer", norm(o) in n_ans, ""))
    bad = [o for o in _as_list(exp.get("not_contains")) if norm(o) in n_ans]
    if exp.get("not_contains"):
        checks.append(Check("not_contains", "answer", not bad, f"có cụm cấm {bad}" if bad else ""))
    for pat in _as_list(exp.get("not_regex")):
        m = re.search(pat, ans)
        checks.append(Check(f"not_regex", "answer", m is None, f"khớp '{m.group(0)}'" if m else pat))
    if exp.get("dashboard_link"):
        # Đúng dạng dashboard_tools tạo: <DASHBOARD_BASE_URL>/<48 ký tự hex>.html
        base = re.escape((_settings().dashboard_base_url or "").rstrip("/")) or r"https?://\S+?"
        m = re.search(base + r"/[0-9a-f]{48}\.html", ans)
        checks.append(Check("dashboard_link", "answer", m is not None, m.group(0) if m else "không có link"))
    if exp.get("no_money"):
        money = has_money(ans)
        checks.append(Check("no_money", "answer", not money, f"có số tiền {money[:3]}" if money else ""))
    if do_judge and exp.get("judge"):
        try:
            ok, reason = await judge(case, ans, reference, judge_model, judge_tracker)
            checks.append(Check("judge", "answer", ok, reason))
        except Exception as e:  # noqa: BLE001
            checks.append(Check("judge", "answer", None, f"LLM chấm lỗi: {e}"))
    return checks, reference


# ================================================================ báo cáo

@dataclass
class CaseResult:
    case_id: str
    variant: str          # v2 | single
    repeat: int
    role: str
    question: str
    answer: str
    route: str
    nodes: list[str]
    tools: list[str]
    brief: Optional[dict]
    latency_s: float
    usage: dict
    checks: list[dict]
    answer_ok: bool
    route_ok: bool
    passed: bool
    error: Optional[str]


def _ok(checks: list[Check], kind: str) -> bool:
    return all(c.ok is not False for c in checks if c.kind == kind)


def _p95(xs: list[float]) -> float:
    if not xs:
        return 0.0
    xs = sorted(xs)
    return xs[min(len(xs) - 1, math.ceil(0.95 * len(xs)) - 1)]


def aggregate(results: list[CaseResult]) -> dict:
    lat = [r.latency_s for r in results]
    costs = [r.usage.get("cost_usd") for r in results if r.usage.get("cost_usd") is not None]
    n = len(results) or 1
    return {
        "cases": len(results),
        "passed": sum(r.passed for r in results),
        "pass_rate": round(sum(r.passed for r in results) / n, 3),
        "answer_accuracy": round(sum(r.answer_ok for r in results) / n, 3),
        "route_accuracy": round(sum(r.route_ok for r in results) / n, 3),
        "errors": sum(1 for r in results if r.error),
        "latency_avg_s": round(statistics.mean(lat), 2) if lat else 0,
        "latency_p95_s": round(_p95(lat), 2),
        "cost_total_usd": round(sum(costs), 4) if costs else None,
        "cost_avg_usd": round(statistics.mean(costs), 5) if costs else None,
        "llm_calls_avg": round(statistics.mean(r.usage.get("llm_calls", 0) for r in results), 1) if results else 0,
        "tokens_in_avg": round(statistics.mean(r.usage.get("input_tokens", 0) for r in results)) if results else 0,
        "tokens_out_avg": round(statistics.mean(r.usage.get("output_tokens", 0) for r in results)) if results else 0,
    }


def cost_by_agent(results: list[CaseResult]) -> dict:
    out: dict = {}
    for r in results:
        for node, u in (r.usage.get("by_node") or {}).items():
            a = out.setdefault(node, {"calls": 0, "input": 0, "output": 0, "cost_usd": 0.0})
            for k in ("calls", "input", "output"):
                a[k] += u[k]
            a["cost_usd"] = round(a["cost_usd"] + u["cost_usd"], 6)
    return out


def _mark(ok: Optional[bool]) -> str:
    return "–" if ok is None else ("✓" if ok else "✗")


def print_case(r: CaseResult, verbose: bool) -> None:
    cost = r.usage.get("cost_usd")
    head = (f"[{'PASS' if r.passed else 'FAIL'}] {r.variant:<6} {r.case_id:<34} route={r.route:<9} "
            f"{r.latency_s:>6.1f}s  {('$%.4f' % cost) if cost is not None else '$?':>8}")
    print(head)
    for c in r.checks:
        if verbose or c["ok"] is False:
            print(f"        {_mark(c['ok'])} {c['kind']:<6} {c['name']:<18} {c['detail']}")
    if verbose or not r.passed:
        snippet = r.answer.replace("\n", " ⏎ ")
        print(f"        ↳ {snippet[:300]}{'…' if len(snippet) > 300 else ''}")


def print_summary(by_variant: dict[str, list[CaseResult]], judge_usage: dict) -> None:
    print("\n" + "=" * 92)
    rows = [
        ("Số case", "cases", "{}"), ("Đạt (cả câu trả lời + route)", "pass_rate", "{:.0%}"),
        ("Câu trả lời đúng", "answer_accuracy", "{:.0%}"), ("Route / intent / kỳ đúng", "route_accuracy", "{:.0%}"),
        ("Lỗi / timeout", "errors", "{}"), ("Độ trễ TB (giây)", "latency_avg_s", "{}"),
        ("Độ trễ p95 (giây)", "latency_p95_s", "{}"), ("Số lần gọi LLM TB", "llm_calls_avg", "{}"),
        ("Token vào TB", "tokens_in_avg", "{}"), ("Token ra TB", "tokens_out_avg", "{}"),
        ("Chi phí TB / câu (USD)", "cost_avg_usd", "{}"), ("Tổng chi phí (USD)", "cost_total_usd", "{}"),
    ]
    aggs = {v: aggregate(rs) for v, rs in by_variant.items()}
    names = list(aggs)
    print(f"{'Chỉ số':<32}" + "".join(f"{('graph v2' if v == 'v2' else '1 agent'):>16}" for v in names))
    for label, key, fmt in rows:
        cells = []
        for v in names:
            val = aggs[v][key]
            cells.append("n/a" if val is None else fmt.format(val))
        print(f"{label:<32}" + "".join(f"{c:>16}" for c in cells))
    if "single" in by_variant:
        print("  (bản 1 agent không có route: cột 'Route' chỉ kiểm tra có gọi đúng tool dữ liệu hay không)")
    if "v2" in by_variant:
        print("\nChi phí theo agent (graph v2):")
        for node, a in sorted(cost_by_agent(by_variant["v2"]).items(), key=lambda kv: -kv[1]["cost_usd"]):
            print(f"  {node:<12} {a['calls']:>4} lần gọi  in={a['input']:>8}  out={a['output']:>7}  ${a['cost_usd']:.4f}")
    if judge_usage.get("llm_calls"):
        print(f"\nLLM chấm: {judge_usage['llm_calls']} lần, ${judge_usage.get('cost_usd') or 0:.4f} (không tính vào bảng trên)")
    if "v2" in by_variant and "single" in by_variant:
        a, b = aggs["v2"], aggs["single"]
        print("\nSo sánh nhanh (v2 so với 1 agent):")
        print(f"  đúng: {a['answer_accuracy']:.0%} vs {b['answer_accuracy']:.0%}   "
              f"độ trễ TB: {a['latency_avg_s']}s vs {b['latency_avg_s']}s", end="")
        if a["cost_avg_usd"] and b["cost_avg_usd"]:
            print(f"   chi phí TB: ×{a['cost_avg_usd'] / b['cost_avg_usd']:.2f}", end="")
        print()
        diff = {(r.case_id, r.repeat): r.passed for r in by_variant["single"]}
        only_v2 = sorted({r.case_id for r in by_variant["v2"] if r.passed and diff.get((r.case_id, r.repeat)) is False})
        only_single = sorted({r.case_id for r in by_variant["v2"] if not r.passed and diff.get((r.case_id, r.repeat))})
        if only_v2:
            print(f"  chỉ v2 đạt: {', '.join(only_v2)}")
        if only_single:
            print(f"  chỉ 1 agent đạt: {', '.join(only_single)}")
    print("=" * 92)


# ================================================================ main

def select_cases(cases: list[Case], ids: list[str], tags: list[str]) -> list[Case]:
    if ids:
        unknown = set(ids) - {c.id for c in cases}
        if unknown:
            raise SystemExit(f"Không có case: {sorted(unknown)}")
        cases = [c for c in cases if c.id in ids]
    if tags:
        cases = [c for c in cases if set(tags) & set(c.tags)]
    return cases


def dry_run(cases: list[Case], pricing: dict) -> None:
    from app.agent.periods import PeriodError, resolve_period

    print(f"questions.yaml hợp lệ: {len(cases)} case, giá cho {len(pricing)} model.\n")
    for c in cases:
        exp = c.expect
        period = ""
        if exp.get("period"):
            try:
                p = resolve_period(exp["period"])
                period = f" kỳ={p.start:%d/%m}-{p.end:%d/%m}"
            except PeriodError as e:
                period = f" kỳ=LỖI({e})"
        ref = f" ref={exp['reference']['query']}" if exp.get("reference") else ""
        print(f"  {c.id:<34} {c.role:<8} route={'/'.join(_as_list(exp.get('route', 'any'))):<14}"
              f"{period}{ref}{' +judge' if exp.get('judge') else ''}{' setup=' + str(len(c.setup)) if c.setup else ''}")
    print("\nKhông gọi LLM hay database. Bỏ --dry-run để chạy thật (tốn chi phí).")


async def main_async(args) -> int:
    cases, pricing = load_spec(Path(args.questions))
    cases = select_cases(cases, args.case or [], args.tag or [])
    if not cases:
        print("Không có case nào khớp bộ lọc.")
        return 1
    if args.dry_run:
        dry_run(cases, pricing)
        return 0

    from langgraph.checkpoint.memory import MemorySaver

    from app.agent.graph import build_graph
    from app.agent.models import load_agents_config
    from app.agent.tools import ALL_TOOLS
    from app.db.pool import close_pool, init_pool

    agents_cfg = load_agents_config()
    single_model = args.single_model or agents_cfg.get("supervisor").model
    single_steps = args.single_max_steps or (
        agents_cfg.get("coder").max_steps + agents_cfg.get("designer").max_steps
    )
    variants = ["v2"] + (["single"] if args.compare else [])
    print(f"Chạy {len(cases)} case × {args.repeat} lần × {len(variants)} bản ({', '.join(variants)}). "
          f"Model: " + ", ".join(f"{n}={s.model}" for n, s in agents_cfg.agents.items() if s.enabled)
          + (f"; 1 agent={single_model} (tối đa {single_steps} bước tool)" if args.compare else "")
          + (f"; chấm bằng {args.judge_model}" if args.judge else "") + "\n")

    await init_pool()
    graph = build_graph(ALL_TOOLS, MemorySaver())
    judge_tracker = UsageTracker(label="judge")
    by_variant: dict[str, list[CaseResult]] = {v: [] for v in variants}
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    try:
        for rep in range(args.repeat):
            for case in cases:
                for variant in variants:
                    if variant == "v2":
                        out = await run_v2(graph, case, thread=f"eval-{stamp}-{case.id}-{rep}")
                    else:
                        out = await run_single(case, single_model, single_steps)
                    checks = check_route(case, out, variant)
                    a_checks, _ = await check_answer(case, out, args.judge, args.judge_model, judge_tracker)
                    checks += a_checks
                    answer_ok, route_ok = _ok(checks, "answer"), _ok(checks, "route")
                    r = CaseResult(
                        case_id=case.id, variant=variant, repeat=rep, role=case.role, question=case.question,
                        answer=out.answer, route=route_of(out.nodes) if variant == "v2" else "single",
                        nodes=out.nodes, tools=out.tools, brief=out.brief, latency_s=out.latency_s,
                        usage=summarize_usage(out.usage, pricing), checks=[asdict(c) for c in checks],
                        answer_ok=answer_ok, route_ok=route_ok, passed=answer_ok and route_ok, error=out.error,
                    )
                    by_variant[variant].append(r)
                    print_case(r, args.verbose)
    finally:
        await close_pool()

    judge_usage = summarize_usage(judge_tracker.calls, pricing)
    print_summary(by_variant, judge_usage)

    out_path = Path(args.out) if args.out else RESULTS_DIR / f"eval-{stamp}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({
        "started": stamp,
        "questions": str(Path(args.questions).resolve().relative_to(ROOT)) if Path(args.questions).resolve().is_relative_to(ROOT) else args.questions,
        "models": {n: s.model for n, s in agents_cfg.agents.items() if s.enabled},
        "single_model": single_model if args.compare else None,
        "judge_model": args.judge_model if args.judge else None,
        "summary": {v: aggregate(rs) for v, rs in by_variant.items()},
        "cost_by_agent_v2": cost_by_agent(by_variant["v2"]),
        "judge_usage": judge_usage,
        "results": [asdict(r) for rs in by_variant.values() for r in rs],
    }, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"Đã ghi chi tiết: {out_path}")

    rate = aggregate(by_variant["v2"])["pass_rate"]
    if args.fail_under is not None and rate < args.fail_under:
        print(f"Tỉ lệ đạt {rate:.0%} thấp hơn ngưỡng {args.fail_under:.0%}")
        return 2
    return 0


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Đánh giá agent bằng bộ câu hỏi mẫu (có gọi LLM, tốn chi phí).")
    p.add_argument("--questions", default=str(DEFAULT_QUESTIONS), help="đường dẫn file câu hỏi (yaml)")
    p.add_argument("--case", action="append", help="chỉ chạy case này (lặp lại được)")
    p.add_argument("--tag", action="append", help="chỉ chạy case có nhãn này (lặp lại được)")
    p.add_argument("--repeat", type=int, default=1, help="chạy mỗi case N lần (đo độ ổn định)")
    p.add_argument("--compare", action="store_true", help="chạy thêm bản 1 agent để so sánh")
    p.add_argument("--single-model", help="model của bản 1 agent (mặc định = model của supervisor)")
    p.add_argument("--single-max-steps", type=int, help="số bước tool tối đa của bản 1 agent")
    p.add_argument("--judge", action="store_true", help="dùng LLM chấm các case có `judge` (tốn thêm)")
    p.add_argument("--judge-model", default="claude-sonnet-5-5", help="model chấm điểm")
    p.add_argument("--fail-under", type=float, help="thoát mã 2 nếu tỉ lệ đạt của v2 thấp hơn (0-1)")
    p.add_argument("--out", help="file JSON kết quả (mặc định tests/eval/results/eval-<thời điểm>.json)")
    p.add_argument("--dry-run", action="store_true", help="chỉ kiểm tra yaml, không gọi LLM/DB")
    p.add_argument("-v", "--verbose", action="store_true", help="in mọi check và câu trả lời")
    args = p.parse_args(argv)
    if args.repeat < 1:
        p.error("--repeat phải >= 1")
    return args


def main(argv=None) -> int:
    args = parse_args(argv)
    if sys.platform == "win32":  # psycopg async không chạy trên ProactorEventLoop (giống run_windows.py)
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        return asyncio.run(main_async(args))
    except SpecError as e:
        print(e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
