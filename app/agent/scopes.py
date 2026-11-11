# app/agent/scopes.py
# Quyền theo KHỐI dữ liệu (coffee / house / misa...), khai báo trong config/scopes.yaml.
#   - Khối postgres: cho xem một nhóm bảng (permissions.py cộng các bảng này vào quyền của staff).
#   - Khối api: cho gọi các endpoint của một connector (app/agent/tools/api_tools.py).
# Người dùng có khối nào = khối theo role (role_scopes) + khối được cấp riêng còn hạn (grants.py, chỉ staff).
# File cấu hình sai thì báo lỗi ngay khi khởi động (load_scopes_config gọi trong app/main.py).

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Optional

import yaml

SCOPES_CONFIG_PATH = Path(os.environ.get("SCOPES_CONFIG", "config/scopes.yaml"))
STAFF_ROLE = "staff"

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_TABLE_RE = re.compile(r"^(\*|[a-z_][a-z0-9_]*\.(\*|[a-z_][a-z0-9_]*))$")
_PLACEHOLDERS = {"start", "end", "start_dmy", "end_dmy"}


class ScopeConfigError(ValueError):
    """config/scopes.yaml sai."""


@dataclass(frozen=True)
class Endpoint:
    name: str
    description: str
    method: str
    path: str
    params: dict[str, str]
    result_path: Optional[str] = None


@dataclass(frozen=True)
class Connector:
    name: str
    base_url_env: str
    auth_type: str                 # header | query | none
    auth_name: str = ""            # tên header hoặc tên tham số URL
    auth_value_env: str = ""
    auth_prefix: str = ""
    timeout_seconds: int = 20
    max_days: int = 366
    endpoints: dict[str, Endpoint] = field(default_factory=dict)


@dataclass(frozen=True)
class Scope:
    name: str
    title: str
    source: str                    # postgres | api
    tables: frozenset[str] = frozenset()
    connector: Optional[str] = None


@dataclass(frozen=True)
class ScopesConfig:
    scopes: dict[str, Scope]
    role_scopes: dict[str, frozenset[str]]
    connectors: dict[str, Connector]

    def names(self) -> list[str]:
        return sorted(self.scopes)

    def tables_for(self, scope_names) -> set[str]:
        out: set[str] = set()
        for n in scope_names:
            s = self.scopes.get(n)
            if s is not None and s.source == "postgres":
                out |= set(s.tables)
        return out

    def api_scopes(self) -> list[str]:
        return sorted(n for n, s in self.scopes.items() if s.source == "api")

    def db_scopes(self) -> list[str]:
        return sorted(n for n, s in self.scopes.items() if s.source == "postgres")

    def for_role(self, role: Optional[str]) -> set[str]:
        granted = self.role_scopes.get((role or "").lower(), frozenset())
        return set(self.scopes) if "*" in granted else set(granted)


def _req_str(raw: dict, key: str, where: str) -> str:
    v = raw.get(key)
    if not isinstance(v, str) or not v.strip():
        raise ScopeConfigError(f"{where}: thiếu {key}")
    return v.strip()


def _parse_connector(name: str, raw: dict) -> Connector:
    where = f"connectors.{name}"
    if not isinstance(raw, dict):
        raise ScopeConfigError(f"{where} phải là mục cấu hình")
    auth = raw.get("auth") or {"type": "none"}
    a_type = str(auth.get("type", "none")).lower()
    if a_type not in ("header", "query", "none"):
        raise ScopeConfigError(f"{where}.auth.type phải là header, query hoặc none")
    if a_type != "none":
        _req_str(auth, "value_env", f"{where}.auth")
    a_name = str(auth.get("header" if a_type == "header" else "param", "") or "")
    if a_type != "none" and not a_name:
        raise ScopeConfigError(f"{where}.auth cần {'header' if a_type == 'header' else 'param'}")
    endpoints: dict[str, Endpoint] = {}
    for ep_name, ep in (raw.get("endpoints") or {}).items():
        ew = f"{where}.endpoints.{ep_name}"
        if not _NAME_RE.match(str(ep_name)) or not isinstance(ep, dict):
            raise ScopeConfigError(f"{ew}: tên phải là chữ thường/số/gạch dưới")
        method = str(ep.get("method", "GET")).upper()
        if method not in ("GET", "POST"):
            raise ScopeConfigError(f"{ew}.method chỉ được GET hoặc POST")
        path = _req_str(ep, "path", ew)
        if not path.startswith("/") or "://" in path or ".." in path:
            raise ScopeConfigError(f"{ew}.path phải là đường dẫn bắt đầu bằng '/' (không chứa URL hay '..')")
        params = {str(k): str(v) for k, v in (ep.get("params") or {}).items()}
        for v in params.values():
            unknown = set(re.findall(r"\{(\w+)\}", v)) - _PLACEHOLDERS
            if unknown:
                raise ScopeConfigError(f"{ew}.params dùng biến lạ {sorted(unknown)}; chỉ có {sorted(_PLACEHOLDERS)}")
        endpoints[ep_name] = Endpoint(ep_name, _req_str(ep, "description", ew), method, path, params,
                                      ep.get("result_path"))
    if not endpoints:
        raise ScopeConfigError(f"{where}: chưa khai báo endpoint nào")
    timeout = int(raw.get("timeout_seconds", 20))
    max_days = int(raw.get("max_days", 366))
    if not 1 <= timeout <= 120 or not 1 <= max_days <= 3660:
        raise ScopeConfigError(f"{where}: timeout_seconds 1-120, max_days 1-3660")
    return Connector(name, _req_str(raw, "base_url_env", where), a_type, a_name,
                     str(auth.get("value_env", "")), str(auth.get("prefix", "") or ""),
                     timeout, max_days, endpoints)


def parse_scopes_config(data: dict) -> ScopesConfig:
    if not isinstance(data, dict) or not isinstance(data.get("scopes"), dict) or not data["scopes"]:
        raise ScopeConfigError("thiếu mục 'scopes'")
    connectors = {n: _parse_connector(n, c) for n, c in (data.get("connectors") or {}).items()}
    scopes: dict[str, Scope] = {}
    for name, raw in data["scopes"].items():
        where = f"scopes.{name}"
        if not _NAME_RE.match(str(name)) or name in ("all",):
            raise ScopeConfigError(f"{where}: tên khối phải là chữ thường/số/gạch dưới và khác 'all'")
        raw = raw or {}
        source = str(raw.get("source", "")).lower()
        if source == "postgres":
            tables = {str(t).lower() for t in (raw.get("tables") or [])}
            if not tables or any(not _TABLE_RE.match(t) for t in tables):
                raise ScopeConfigError(f"{where}.tables phải là danh sách 'schema.table' hoặc 'schema.*'")
            scopes[name] = Scope(name, str(raw.get("title") or name), source, frozenset(tables))
        elif source == "api":
            conn = _req_str(raw, "connector", where)
            if conn not in connectors:
                raise ScopeConfigError(f"{where}.connector '{conn}' không có trong 'connectors'")
            scopes[name] = Scope(name, str(raw.get("title") or name), source, connector=conn)
        else:
            raise ScopeConfigError(f"{where}.source phải là postgres hoặc api")
    role_scopes: dict[str, frozenset[str]] = {}
    for role, names in (data.get("role_scopes") or {}).items():
        names = {str(n) for n in (names or [])}
        unknown = names - set(scopes) - {"*"}
        if unknown:
            raise ScopeConfigError(f"role_scopes.{role}: khối không tồn tại {sorted(unknown)}")
        role_scopes[str(role).lower()] = frozenset(names)
    return ScopesConfig(scopes, role_scopes, connectors)


@lru_cache(maxsize=1)
def load_scopes_config(path: Optional[str] = None) -> ScopesConfig:
    p = Path(path) if path else SCOPES_CONFIG_PATH
    with p.open(encoding="utf-8") as f:
        return parse_scopes_config(yaml.safe_load(f))


def scope_names() -> list[str]:
    return load_scopes_config().names()


def is_scope(name: str) -> bool:
    return name in load_scopes_config().scopes


async def user_scopes(role: Optional[str], user_id) -> set[str]:
    """Các khối người dùng đang được xem: theo role + (staff) khối được cấp riêng còn hạn.
    Lỗi đọc quyền cấp riêng thì grants trả tập rỗng (an toàn: không thêm quyền)."""
    from app.agent import grants  # import tại chỗ: grants import psycopg_pool, test cấu hình không cần

    cfg = load_scopes_config()
    role_l = (role or "").lower()
    out = cfg.for_role(role_l)
    if role_l == STAFF_ROLE and user_id:
        out |= {s for s in await grants.get_grants(str(user_id)) if s in cfg.scopes}
    return out


def describe_scopes(names) -> str:
    """'coffee (Nyna Coffee (iPOS)), house (Nyna House)' hoặc '(chưa có)'."""
    cfg = load_scopes_config()
    items = [f"{n} ({cfg.scopes[n].title})" for n in sorted(names) if n in cfg.scopes]
    return ", ".join(items) or "(chưa có)"
