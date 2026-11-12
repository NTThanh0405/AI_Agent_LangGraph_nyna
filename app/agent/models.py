# app/agent/models.py
# Chọn model theo từng agent từ config/agents.yaml. File sai (thiếu agent, số bước <= 0,
# timeout <= 0, agent không biết) thì báo lỗi ngay khi khởi động, không đợi đến lúc có người hỏi.

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Optional

import yaml

AGENT_NAMES = ("supervisor", "analyst", "coder", "designer", "reviewer")
REQUIRED_AGENTS = ("supervisor", "analyst", "coder", "designer")  # reviewer là tuỳ chọn

AGENTS_CONFIG_PATH = Path(os.environ.get("AGENTS_CONFIG", "config/agents.yaml"))


class AgentConfigError(ValueError):
    """config/agents.yaml sai."""


@dataclass(frozen=True)
class AgentSpec:
    name: str
    enabled: bool
    model: str
    max_steps: int
    timeout_seconds: int
    temperature: Optional[float]
    max_tokens: int
    # Khoá riêng của từng agent ngoài các khoá chung ở trên (vd reviewer.mode); không kiểm tra ở đây
    options: dict = field(default_factory=dict)


@dataclass(frozen=True)
class AgentsConfig:
    agents: dict[str, AgentSpec]
    max_handoffs: int

    def get(self, name: str) -> AgentSpec:
        try:
            return self.agents[name]
        except KeyError:
            raise AgentConfigError(f"agent không có trong config: {name!r}") from None


_COMMON_KEYS = {"enabled", "model", "max_steps", "timeout_seconds", "temperature", "max_tokens"}


def _pos_int(value, name: str, agent: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise AgentConfigError(f"{agent}.{name} phải là số nguyên dương, nhận {value!r}")
    return value


def parse_agents_config(data: dict) -> AgentsConfig:
    if not isinstance(data, dict) or not isinstance(data.get("agents"), dict):
        raise AgentConfigError("thiếu mục 'agents'")
    default = data.get("default") or {}
    unknown = set(data["agents"]) - set(AGENT_NAMES)
    if unknown:
        raise AgentConfigError(f"agent không hợp lệ: {sorted(unknown)}")
    specs: dict[str, AgentSpec] = {}
    for name, raw in data["agents"].items():
        raw = {**default, **(raw or {})}
        model = raw.get("model")
        if not isinstance(model, str) or not model.strip():
            raise AgentConfigError(f"{name}.model bị thiếu")
        temp = raw.get("temperature")
        if temp is not None and (isinstance(temp, bool) or not isinstance(temp, (int, float)) or not 0 <= temp <= 1):
            raise AgentConfigError(f"{name}.temperature phải trong khoảng 0-1")
        specs[name] = AgentSpec(
            name=name,
            enabled=bool(raw.get("enabled", True)),
            model=model.strip(),
            max_steps=_pos_int(raw.get("max_steps"), "max_steps", name),
            timeout_seconds=_pos_int(raw.get("timeout_seconds"), "timeout_seconds", name),
            temperature=None if temp is None else float(temp),
            max_tokens=_pos_int(raw.get("max_tokens", 4096), "max_tokens", name),
            options={k: v for k, v in raw.items() if k not in _COMMON_KEYS},
        )
    for name in REQUIRED_AGENTS:
        if name not in specs:
            raise AgentConfigError(f"thiếu agent bắt buộc: {name}")
        if not specs[name].enabled:
            raise AgentConfigError(f"agent {name} là bắt buộc, không được tắt")
    return AgentsConfig(agents=specs, max_handoffs=_pos_int(data.get("max_handoffs", 8), "max_handoffs", "root"))


@lru_cache(maxsize=1)
def load_agents_config(path: Optional[str] = None) -> AgentsConfig:
    p = Path(path) if path else AGENTS_CONFIG_PATH
    with p.open(encoding="utf-8") as f:
        return parse_agents_config(yaml.safe_load(f))


def get_model(agent: str):
    """Trả ChatAnthropic cho agent. Import muộn để test cấu hình không cần cài langchain."""
    from langchain_anthropic import ChatAnthropic

    from app.config import get_settings

    spec = load_agents_config().get(agent)
    s = get_settings()
    kwargs = dict(
        model=spec.model,
        api_key=s.anthropic_api_key,
        max_tokens=spec.max_tokens,
        timeout=spec.timeout_seconds,
        max_retries=2,
    )
    if spec.temperature is not None:
        kwargs["temperature"] = spec.temperature
    return ChatAnthropic(**kwargs)


def recursion_limit(cfg: Optional[AgentsConfig] = None) -> int:
    """Số node tối đa LangGraph cho phép chạy trong một lượt hỏi.
    Mỗi lượt chuyển giao tính 1 node supervisor + 1 node agent; mỗi bước tool của worker tính 2 node
    (LLM + chạy tool). Cộng thêm chỗ cho node chat. Vượt thì runner trả "câu hỏi quá phức tạp"."""
    cfg = cfg or load_agents_config()
    tool_steps = sum(s.max_steps for n, s in cfg.agents.items() if n in ("coder", "designer") and s.enabled)
    return cfg.max_handoffs * 2 + tool_steps * 2 + 6
