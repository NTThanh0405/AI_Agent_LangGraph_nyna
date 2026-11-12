# app/agent/nodes/reviewer.py
# Reviewer (TUỲ CHỌN): soát câu trả lời cuối trước khi gửi người dùng. Bật/tắt bằng
# config/agents.yaml -> agents.reviewer.enabled (cần restart). Tắt thì graph không có node này.
#
#   ... -> chat -> reviewer -> END
#
# Hai lớp:
#   1. Kiểm tra bằng code (không tốn tiền): mọi con số trong câu trả lời phải đối chiếu được với dữ liệu
#      đã thu thập (kết quả truy vấn, ghi chú của Coder/Designer, bản tóm tắt). Hiểu số rút gọn
#      ("41,5 triệu" ~ 41.488.581), phần trăm, và số tính từ hai số liệu (chênh lệch, tỉ lệ, % thay đổi).
#      Bỏ qua ngày, giờ, link, năm, số nhỏ (<= 10).
#   2. LLM soát lại (tốn tiền, model của reviewer): chỉ chạy khi lớp 1 thấy số đáng ngờ, hoặc câu hỏi thuộc
#      intent trong `llm_intents` (mặc định explain_change), hoặc khi mode: always.
#      Bản sửa của LLM chỉ được dùng nếu chính nó qua được lớp 1.
#
# Kết quả: giữ nguyên câu trả lời, thay bằng bản sửa (cùng id tin nhắn), hoặc giữ nguyên + thêm một dòng
# lưu ý khi vẫn còn số chưa đối chiếu được. Reviewer lỗi / quá giờ thì không chặn câu trả lời.
#
# Tuỳ chọn trong agents.yaml (khối reviewer):
#   mode: suspect | always        suspect (mặc định) = chỉ gọi LLM khi cần; always = soát mọi câu có số liệu
#   llm_intents: [explain_change] intent luôn được LLM soát (câu "vì sao" dễ đoán bừa nguyên nhân)
#   add_note: true                còn số chưa đối chiếu được thì thêm dòng lưu ý cuối câu trả lời

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Iterable, Optional

from langchain_core.exceptions import OutputParserException
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from pydantic import BaseModel, Field

from app.agent.models import load_agents_config
from app.agent.nodes import _common as C
from app.agent.nodes.chat import _data_section
from app.config import get_settings

logger = logging.getLogger(__name__)

NOTE = ("\n\n⚠️ Lưu ý: chưa đối chiếu được {nums} với số liệu truy vấn. "
        "Bạn nên kiểm tra lại hoặc hỏi lại chi tiết.")

# ---------------------------------------------------------------- đọc số

_UNITS = {"tỷ": 1e9, "ty": 1e9, "tỉ": 1e9, "b": 1e9, "triệu": 1e6, "trieu": 1e6, "tr": 1e6, "m": 1e6,
          "nghìn": 1e3, "nghin": 1e3, "ngàn": 1e3, "ngan": 1e3, "k": 1e3}
_NUM_RE = re.compile(
    r"(?<![\w.,])(\d+(?:[.,]\d+)*)(\s?%|\s?(?:tỷ|tỉ|ty|triệu|trieu|tr|nghìn|nghin|ngàn|ngan)\b|(?:[MKBmkb])\b)?",
    re.IGNORECASE,
)
_SKIP_RE = [
    re.compile(r"https?://\S+"),                                   # link (dashboard có tên file hex)
    re.compile(r"\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b"),               # ngày 02/11, 02/11/2026
    re.compile(r"\b\d{4}-\d{2}-\d{2}\b"),                          # ngày ISO
    re.compile(r"\b\d{1,2}[:h]\d{2}\b"),                           # giờ 07:00, 7h30
    re.compile(r"\b(?:19|20)\d{2}\b"),                             # năm
]


@dataclass(frozen=True)
class Num:
    text: str
    values: tuple[float, ...]   # các cách hiểu (vd "1,234" có thể là 1234 hoặc 1.234)
    tol: float                  # sai số do làm tròn khi hiển thị
    percent: bool


def _readings(raw: str) -> list[tuple[float, int]]:
    """Các cách hiểu một chuỗi số -> [(giá trị, số chữ số thập phân hiển thị)]."""
    if "." in raw and "," in raw:
        dec = "," if raw.rfind(",") > raw.rfind(".") else "."
        th = "." if dec == "," else ","
        txt = raw.replace(th, "").replace(dec, ".")
        return [(float(txt), len(txt.split(".")[1]))]
    for sep in (".", ","):
        if sep in raw:
            parts = raw.split(sep)
            grouped = len(parts) > 1 and 1 <= len(parts[0]) <= 3 and all(len(p) == 3 for p in parts[1:])
            out = []
            if grouped:                                   # 41.488.581 / 1,234 -> phân cách hàng nghìn
                out.append((float("".join(parts)), 0))
            if len(parts) == 2:                           # 41,5 / 13.9 -> phần thập phân
                out.append((float(parts[0] + "." + parts[1]), len(parts[1])))
            return out or [(float("".join(parts)), 0)]
    return [(float(raw), 0)]


def extract_numbers(text: str) -> list[Num]:
    for rx in _SKIP_RE:
        text = rx.sub(" ", text or "")
    nums: list[Num] = []
    for m in _NUM_RE.finditer(text):
        raw, unit = m.group(1), (m.group(2) or "").strip().lower()
        percent = unit == "%"
        mult = 1.0 if percent or not unit else _UNITS.get(unit, 1.0)
        vals, tol = [], 0.0
        for v, decimals in _readings(raw):
            vals.append(v * mult)
            tol = max(tol, 0.5 * (10 ** -decimals) * mult)
        nums.append(Num(m.group(0).strip(), tuple(vals), tol, percent))
    return nums


def _close(a: float, b: float, tol: float) -> bool:
    return abs(a - b) <= tol + 1e-9 + abs(b) * 1e-9


def _derived(data: list[float]) -> set[float]:
    """Số tính được từ hai số liệu: chênh lệch, thương (vd AOV = doanh thu / số đơn), tỉ lệ %, % thay đổi."""
    vals = sorted({abs(x) for x in data if x})[:300]
    out: set[float] = set()
    for i, x in enumerate(vals):
        for y in vals[i + 1:]:
            out.add(y - x)
            out.add(y / x)
            out.add(100 * x / y)
            out.add(100 * (y / x - 1))
            out.add(100 * (1 - x / y))
    return out


def unsupported_numbers(answer: str, data_text: str, allowed: Iterable[float] = ()) -> list[str]:
    """Các số trong câu trả lời KHÔNG đối chiếu được với dữ liệu (rỗng = ổn). Số nguyên <= 10 bỏ qua."""
    data_vals = [v for n in extract_numbers(data_text) for v in n.values] + list(allowed)
    derived: Optional[set[float]] = None
    bad: list[str] = []
    for n in extract_numbers(answer):
        if not n.percent and all(v.is_integer() and abs(v) <= 10 for v in n.values):
            continue
        ok = any(_close(v, abs(d), n.tol) for v in n.values for d in data_vals)
        if not ok:
            derived = derived if derived is not None else _derived(data_vals)
            ok = any(_close(v, d, max(n.tol, 0.05 if n.percent else 0)) for v in n.values for d in derived)
        if not ok and n.text not in bad:
            bad.append(n.text)
    return bad


# ---------------------------------------------------------------- LLM soát lại

class ReviewResult(BaseModel):
    ok: bool = Field(description="True nếu câu trả lời đúng với dữ liệu, không cần sửa")
    issues: list[str] = Field(default_factory=list, description="Các lỗi tìm thấy, ngắn gọn")
    revised_answer: str = Field(default="", description="Câu trả lời đã sửa (đầy đủ) nếu ok=False; rỗng nếu ok=True")


def _options() -> dict:
    spec = load_agents_config().get("reviewer")
    o = dict(spec.options or {})
    return {
        "mode": str(o.get("mode", "suspect")).lower(),
        "llm_intents": set(o.get("llm_intents", ["explain_change"]) or []),
        "add_note": bool(o.get("add_note", True)),
        "timeout": spec.timeout_seconds,
    }


def _last_answer(messages: list) -> Optional[AIMessage]:
    if not messages or not isinstance(messages[-1], AIMessage):
        return None
    return messages[-1]


async def _llm_review(answer: str, data_text: str, suspects: list[str], config: RunnableConfig,
                      timeout: int) -> Optional[ReviewResult]:
    hint = (f"Các số chưa đối chiếu được bằng máy (có thể là số tính ra hợp lệ): {', '.join(suspects)}"
            if suspects else "Kiểm tra bằng máy không thấy số lạ; soát lập luận và nguyên nhân.")
    system = C.system_message("reviewer", config, (), data_text, hint)
    llm = C.get_llm("reviewer").with_structured_output(ReviewResult)
    try:
        out = await asyncio.wait_for(llm.ainvoke([system, HumanMessage(content=f"Câu trả lời cần soát:\n{answer}")]),
                                     timeout)
    except (asyncio.TimeoutError, OutputParserException, ValueError):
        logger.warning("Reviewer không soát được (quá giờ hoặc sai định dạng), giữ câu trả lời gốc")
        return None
    except Exception:  # lỗi mạng/API: không được chặn câu trả lời
        logger.exception("Reviewer lỗi, giữ câu trả lời gốc")
        return None
    return out if isinstance(out, ReviewResult) else None


async def reviewer_node(state: dict, config: RunnableConfig) -> dict:
    msg = _last_answer(state.get("messages") or [])
    brief = state.get("brief") or {}
    if msg is None or brief.get("intent") == "other":
        return {}
    answer = C.extract_text(msg.content)
    if not extract_numbers(answer):                        # không có số liệu -> không cần soát
        return {}

    opts = _options()
    question = C.last_human_text(state.get("messages") or [])
    data_text = "\n".join([_data_section(state), C.brief_section(brief) or "", question])
    allowed = [get_settings().dashboard_ttl_hours]
    suspects = unsupported_numbers(answer, data_text, allowed)

    need_llm = bool(suspects) or opts["mode"] == "always" or brief.get("intent") in opts["llm_intents"]
    if not need_llm:
        return {}

    result = await _llm_review(answer, data_text, suspects, config, opts["timeout"])
    if result is not None and result.ok:                   # LLM xác nhận (số đáng ngờ là số tính ra hợp lệ)
        return {}
    if result is not None and result.revised_answer.strip():
        revised = result.revised_answer.strip()
        still_bad = unsupported_numbers(revised, data_text, allowed)
        if not still_bad:
            logger.info("Reviewer sửa câu trả lời: %s", "; ".join(result.issues)[:300])
            return {"messages": [AIMessage(content=revised, id=msg.id)]}
        logger.warning("Bản sửa của reviewer vẫn có số lạ %s, không dùng", still_bad)

    if suspects and opts["add_note"]:
        logger.warning("Câu trả lời có số chưa đối chiếu được: %s", suspects)
        nums = ", ".join(f"'{s}'" for s in suspects[:3]) + (" …" if len(suspects) > 3 else "")
        return {"messages": [AIMessage(content=answer + NOTE.format(nums=nums), id=msg.id)]}
    return {}
