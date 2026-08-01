"""gp config command family (spec § gp config command family)."""

import stat

import pytest
from typer.testing import CliRunner

from graftpunk import workstation_env
from graftpunk.cli.main import app

# Plain CliRunner: click 8.2+ removed mix_stderr — stderr is always captured
# separately and result.stderr works (verified against the venv's typer 0.21.1
# / click 8.3.1; passing mix_stderr raises TypeError at collection).
runner = CliRunner()


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("GRAFTPUNK_CONFIG_DIR", str(tmp_path))
    workstation_env._invalidate_cache()
    yield tmp_path
    workstation_env._invalidate_cache()


# Every field label the old panel rendered — the migration constraint is
# "unchanged behavior", so the test asserts the full label set, not one word.
_PANEL_LABELS = (
    "Config directory:",
    "Sessions directory:",
    "Storage backend:",
    "Session TTL:",
    "Log level:",
    "Log format:",
)


def test_bare_config_renders_settings_panel(_isolated):
    result = runner.invoke(app, ["config"])
    assert result.exit_code == 0
    assert "Configuration" in result.stdout
    for label in _PANEL_LABELS:
        assert label in result.stdout  # migrated display, unchanged behavior


def test_config_show_same_panel(_isolated):
    result = runner.invoke(app, ["config", "show"])
    assert result.exit_code == 0
    for label in _PANEL_LABELS:
        assert label in result.stdout


def test_config_path(_isolated):
    result = runner.invoke(app, ["config", "path"])
    assert result.exit_code == 0
    assert result.stdout.strip() == str(_isolated / "env")


def test_set_get_list_unset_roundtrip(_isolated):
    assert (
        runner.invoke(app, ["config", "set", "SHOPKEEP_STORE_NAME", "the-french-co"]).exit_code == 0
    )
    got = runner.invoke(app, ["config", "get", "SHOPKEEP_STORE_NAME"])
    assert got.stdout.strip() == "the-french-co"
    listed = runner.invoke(app, ["config", "list"])
    assert "SHOPKEEP_STORE_NAME=the-french-co" in listed.stdout
    assert runner.invoke(app, ["config", "unset", "SHOPKEEP_STORE_NAME"]).exit_code == 0
    assert runner.invoke(app, ["config", "get", "SHOPKEEP_STORE_NAME"]).exit_code == 1


def test_set_stores_command_verbatim_and_get_resolve_evaluates(_isolated):
    runner.invoke(app, ["config", "set", "K", "$(echo hi)"])
    raw = runner.invoke(app, ["config", "get", "K"])
    assert raw.stdout.strip() == "$(echo hi)"
    resolved = runner.invoke(app, ["config", "get", "K", "--resolve"])
    assert resolved.stdout.strip() == "hi"
    listed = runner.invoke(app, ["config", "list"])
    assert "K=$(echo hi)  [command]" in listed.stdout  # list surfaces the kind


def test_set_secret_literal_guardrail_warns(_isolated):
    result = runner.invoke(app, ["config", "set", "BEK_PASSWORD", "plaintext-oops"])
    assert result.exit_code == 0  # warn, don't refuse
    assert (
        "warning" in (result.stderr or "").lower() or "plaintext" in (result.stderr or "").lower()
    )


def test_set_bad_name_fails(_isolated):
    assert runner.invoke(app, ["config", "set", "9BAD", "v"]).exit_code != 0


def test_unset_absent_is_idempotent(_isolated):
    assert runner.invoke(app, ["config", "unset", "NEVER_SET"]).exit_code == 0


def test_file_mode_after_cli_write(_isolated):
    runner.invoke(app, ["config", "set", "K", "v"])
    assert stat.S_IMODE((_isolated / "env").stat().st_mode) == 0o600


def test_get_resolve_failed_command_exits_1(_isolated):
    runner.invoke(app, ["config", "set", "K", "$(exit 7)"])
    result = runner.invoke(app, ["config", "get", "K", "--resolve"])
    assert result.exit_code == 1


def test_edit_uses_visual_then_editor(monkeypatch, _isolated):
    calls = []
    monkeypatch.setenv("VISUAL", "visual-editor")
    monkeypatch.setenv("EDITOR", "other-editor")
    monkeypatch.setattr("subprocess.call", lambda argv: calls.append(argv) or 0)
    runner.invoke(app, ["config", "edit"])
    assert calls and calls[0][0] == "visual-editor"
