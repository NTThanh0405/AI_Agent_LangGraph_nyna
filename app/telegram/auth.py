# app/telegram/auth.py
# Xác định người dùng Telegram là ai và có vai trò gì, dựa trên allowlist ở server
# (config/allowlist.yaml). Vai trò KHÔNG bao giờ lấy từ nội dung tin nhắn.
# ID không có trong danh sách -> get_user trả None và bot bỏ qua tin nhắn đó.

import logging
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Optional

import yaml

from app.agent.permissions import ROLE_TABLES, is_known_role
from app.config import get_settings

logger = logging.getLogger(__name__)


class AllowlistError(ValueError):
    """File allowlist thiếu hoặc sai định dạng."""


@dataclass(frozen=True)
class AllowedUser:
    telegram_id: int
    role: str   # chữ thường, luôn là vai trò có trong ROLE_TABLES
    name: str


def _parse(raw: object) -> dict[int, AllowedUser]:
    """Kiểm tra nội dung YAML và đổi thành {telegram_id: AllowedUser}.

    Sai định dạng thì báo lỗi ngay khi khởi động, để gõ nhầm vai trò
    (ví dụ "bosss") không âm thầm khiến người dùng mất quyền.
    """
    if not isinstance(raw, dict) or not isinstance(raw.get("users"), list):
        raise AllowlistError("allowlist.yaml phải có khoá 'users' là một danh sách")

    users: dict[int, AllowedUser] = {}
    for i, item in enumerate(raw["users"], start=1):
        if not isinstance(item, dict):
            raise AllowlistError(f"Mục số {i} phải có dạng khoá: giá trị")

        tid = item.get("telegram_id")
        if isinstance(tid, bool) or not isinstance(tid, int):
            raise AllowlistError(f"Mục số {i}: telegram_id phải là số nguyên")
        if tid in users:
            raise AllowlistError(f"Mục số {i}: telegram_id {tid} bị trùng")

        role = str(item.get("role") or "").strip().lower()
        if not is_known_role(role):
            raise AllowlistError(
                f"Mục số {i}: vai trò '{role}' không có trong ROLE_TABLES "
                f"(app/agent/permissions.py). Các vai trò hợp lệ: "
                f"{', '.join(sorted(ROLE_TABLES))}"
            )

        name = str(item.get("name") or tid)
        users[tid] = AllowedUser(telegram_id=tid, role=role, name=name)
    return users


@lru_cache
def _load() -> dict[int, AllowedUser]:
    path = Path(get_settings().allowlist_path)
    if not path.is_file():
        raise AllowlistError(f"Không tìm thấy file allowlist: {path}")
    with path.open(encoding="utf-8") as f:
        users = _parse(yaml.safe_load(f))
    logger.info("Đã nạp allowlist: %d người dùng", len(users))
    return users


def load_allowlist() -> int:
    """Nạp (hoặc nạp lại) allowlist. Gọi một lần khi khởi động để lỗi cấu hình
    hiện ra ngay. Trả về số người dùng."""
    _load.cache_clear()
    return len(_load())


def get_user(telegram_id: int) -> Optional[AllowedUser]:
    """Trả về người dùng nếu ID nằm trong allowlist, ngược lại None."""
    return _load().get(telegram_id)