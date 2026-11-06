# tests/conftest.py
# Cấu hình dùng chung cho mọi test. Không kết nối DB, không đọc .env.

from types import SimpleNamespace

import pytest


@pytest.fixture(autouse=True)
def fake_settings(monkeypatch):
    """guard.check_select cần db_schema; thay get_settings bằng giá trị giả."""
    monkeypatch.setattr(
        "app.db.guard.get_settings",
        lambda: SimpleNamespace(db_schema="dwh"),
    )