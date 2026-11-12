# tests/test_reports_local_config.py
# Telegram ID thật của manager nằm trong config/reports.local.yaml (không commit);
# config/reports.yaml trong git chỉ là mẫu và phải luôn đọc được.

from pathlib import Path

import pytest

from app.reports import subscriptions as S


def test_template_in_git_is_valid_and_has_no_manager():
    cfg = S.load_config(S._CONFIG_DIR / "reports.yaml")
    assert cfg.managers == {}


def test_local_file_is_gitignored():
    assert "config/reports.local.yaml" in Path(".gitignore").read_text(encoding="utf-8").splitlines()


@pytest.fixture
def config_dir(tmp_path, monkeypatch):
    monkeypatch.delenv("REPORTS_CONFIG", raising=False)
    monkeypatch.setattr(S, "_CONFIG_DIR", tmp_path)
    (tmp_path / "reports.yaml").write_text("managers: {}\n", encoding="utf-8")
    return tmp_path


def test_uses_template_when_no_local_file(config_dir):
    assert S._default_config_path() == config_dir / "reports.yaml"


def test_prefers_local_file(config_dir):
    (config_dir / "reports.local.yaml").write_text("managers: {}\n", encoding="utf-8")
    assert S._default_config_path() == config_dir / "reports.local.yaml"


def test_env_var_wins(config_dir, monkeypatch):
    (config_dir / "reports.local.yaml").write_text("managers: {}\n", encoding="utf-8")
    monkeypatch.setenv("REPORTS_CONFIG", str(config_dir / "khac.yaml"))
    assert S._default_config_path() == config_dir / "khac.yaml"
