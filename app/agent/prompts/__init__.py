# app/agent/prompts/__init__.py
# Nạp system prompt của từng agent từ file .md cùng thư mục (supervisor, analyst, coder, designer).
from functools import lru_cache
from pathlib import Path

_DIR = Path(__file__).resolve().parent


@lru_cache(maxsize=None)
def load_prompt(name: str) -> str:
    if not name.replace("_", "").isalnum():
        raise ValueError(f"tên prompt không hợp lệ: {name!r}")
    return (_DIR / f"{name}.md").read_text(encoding="utf-8").strip()
