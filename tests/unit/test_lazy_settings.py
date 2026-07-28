"""Lazy settings proxy: bounded allowlist, source-aware precedence.

Spec: § Consumption point (b).
"""

import os
import subprocess

import pytest
from structlog.testing import capture_logs

from graftpunk import config as gp_config
from graftpunk import workstation_env


@pytest.fixture(autouse=True)
def _fresh(tmp_path, monkeypatch):
    monkeypatch.setenv("GRAFTPUNK_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("GRAFTPUNK_BROWSER_EXECUTABLE_PATH", raising=False)
    workstation_env._invalidate_cache()
    gp_config.reset_settings()
    yield tmp_path
    workstation_env._invalidate_cache()
    gp_config.reset_settings()
    # inject_static() (exercised by the static-entry tests below) writes
    # straight into os.environ, bypassing monkeypatch bookkeeping — and
    # monkeypatch.delenv(..., raising=False) above registers no undo when
    # the var was already absent (pytest only records undo for keys that
    # existed at delenv-time). Without this, a leaked
    # GRAFTPUNK_BROWSER_EXECUTABLE_PATH survives into later, unrelated
    # tests in the same worker process. Scrub explicitly rather than rely
    # on monkeypatch here.
    os.environ.pop("GRAFTPUNK_BROWSER_EXECUTABLE_PATH", None)


def _write_env(tmp_path, content):
    (tmp_path / "env").write_text(content, encoding="utf-8")


def test_static_visible_via_bootstrap(_fresh):
    _write_env(_fresh, "GRAFTPUNK_BROWSER_EXECUTABLE_PATH=/file/chrome\n")
    settings = gp_config.get_settings()
    assert settings.browser_executable_path == "/file/chrome"


def test_no_command_entries_returns_raw_model(_fresh):
    _write_env(_fresh, "GRAFTPUNK_BROWSER_EXECUTABLE_PATH=/file/chrome\n")
    settings = gp_config.get_settings()
    assert isinstance(settings, gp_config.GraftpunkSettings)  # not a proxy


def test_command_value_not_evaluated_at_get_settings(_fresh, monkeypatch):
    _write_env(_fresh, "GRAFTPUNK_BROWSER_EXECUTABLE_PATH=$(echo /lazy/chrome)\n")

    def boom(*a, **kw):
        raise AssertionError("evaluated too early")

    monkeypatch.setattr(subprocess, "run", boom)
    gp_config.get_settings()  # construction must not evaluate


def test_command_value_resolves_on_first_access_once(_fresh, monkeypatch):
    _write_env(_fresh, "GRAFTPUNK_BROWSER_EXECUTABLE_PATH=$(echo /lazy/chrome)\n")
    settings = gp_config.get_settings()
    calls = []
    real_run = subprocess.run

    def counting_run(*a, **kw):
        calls.append(a)
        return real_run(*a, **kw)

    monkeypatch.setattr(subprocess, "run", counting_run)
    assert settings.browser_executable_path == "/lazy/chrome"
    assert settings.browser_executable_path == "/lazy/chrome"
    assert len(calls) == 1


def test_real_env_beats_command_value(_fresh, monkeypatch):
    _write_env(_fresh, "GRAFTPUNK_BROWSER_EXECUTABLE_PATH=$(echo /file/chrome)\n")
    monkeypatch.setenv("GRAFTPUNK_BROWSER_EXECUTABLE_PATH", "/env/chrome")
    settings = gp_config.get_settings()
    assert settings.browser_executable_path == "/env/chrome"


def test_dotenv_provided_field_yields_to_command_value(_fresh, monkeypatch, tmp_path):
    # Source-aware gate: cwd .env is BELOW the workstation file for both
    # value kinds (documented precedence).
    _write_env(_fresh, "GRAFTPUNK_BROWSER_EXECUTABLE_PATH=$(echo /file/chrome)\n")
    project = tmp_path / "project"
    project.mkdir()
    (project / ".env").write_text("GRAFTPUNK_BROWSER_EXECUTABLE_PATH=/dotenv/chrome\n")
    monkeypatch.chdir(project)
    settings = gp_config.get_settings()
    assert settings.browser_executable_path == "/file/chrome"


def test_non_allowlisted_command_warns_and_is_ignored(_fresh):
    _write_env(_fresh, "GRAFTPUNK_SUPABASE_URL=$(echo https://x)\n")
    with capture_logs() as cap:
        settings = gp_config.get_settings()
    assert settings.supabase_url is None  # overlay never applies
    assert any(
        e["event"] == "workstation_env_command_unsupported_for_field"
        and e.get("envvar") == "GRAFTPUNK_SUPABASE_URL"
        for e in cap
    )


def test_failed_command_falls_back_to_default(_fresh):
    _write_env(_fresh, "GRAFTPUNK_BROWSER_EXECUTABLE_PATH=$(exit 5)\n")
    settings = gp_config.get_settings()
    with capture_logs() as cap:
        assert settings.browser_executable_path is None
    assert any(e["event"] == "workstation_env_command_failed" for e in cap)


def test_proxy_delegates_methods_to_model(_fresh):
    _write_env(_fresh, "GRAFTPUNK_BROWSER_EXECUTABLE_PATH=$(echo /lazy/chrome)\n")
    settings = gp_config.get_settings()
    # Model-internal reads bind to the raw model (bounded contract) — the
    # local storage config still works through the proxy.
    assert settings.get_storage_config()["base_dir"] == settings.sessions_dir
