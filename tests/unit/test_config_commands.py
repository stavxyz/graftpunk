"""gp config command family (spec § gp config command family)."""

import os
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


def test_get_resolve_failed_command_surfaces_stderr(_isolated):
    runner.invoke(app, ["config", "set", "K", "$(echo custom-error-message 1>&2; exit 3)"])
    result = runner.invoke(app, ["config", "get", "K", "--resolve"])
    assert result.exit_code == 1
    assert "custom-error-message" in (result.stderr or "")


def test_get_resolve_failed_command_stderr_survives_rich_markup(_isolated):
    # Reviewer finding: interpolating foreign stderr into a "[red]...[/red]"
    # markup string ate "[flags]"-style tokens (and raised MarkupError on
    # unbalanced "[/...]" tokens). markup=False must let it through verbatim.
    runner.invoke(app, ["config", "set", "K", "$(echo '[flags] boom' 1>&2; exit 3)"])
    result = runner.invoke(app, ["config", "get", "K", "--resolve"])
    assert result.exit_code == 1
    assert "[flags] boom" in (result.stderr or "")


def test_edit_uses_visual_over_editor_when_both_set(monkeypatch, _isolated):
    calls = []
    monkeypatch.setenv("VISUAL", "visual-editor")
    monkeypatch.setenv("EDITOR", "other-editor")
    monkeypatch.setattr("subprocess.call", lambda argv: calls.append(argv) or 0)
    result = runner.invoke(app, ["config", "edit"])
    assert result.exit_code == 0
    assert calls == [["visual-editor", str(_isolated / "env")]]


def test_edit_uses_editor_when_visual_unset(monkeypatch, _isolated):
    # Reviewer finding (A2/B1): a multi-word $EDITOR like "code --wait" was
    # exec'd as one literal binary name (FileNotFoundError). shlex.split
    # must break it into ["code", "--wait", path].
    calls = []
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.setenv("EDITOR", "code --wait")
    monkeypatch.setattr("subprocess.call", lambda argv: calls.append(argv) or 0)
    result = runner.invoke(app, ["config", "edit"])
    assert result.exit_code == 0
    assert calls == [["code", "--wait", str(_isolated / "env")]]


def test_edit_defaults_to_vi_when_neither_set(monkeypatch, _isolated):
    calls = []
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.delenv("EDITOR", raising=False)
    monkeypatch.setattr("subprocess.call", lambda argv: calls.append(argv) or 0)
    result = runner.invoke(app, ["config", "edit"])
    assert result.exit_code == 0
    assert calls == [["vi", str(_isolated / "env")]]


def test_edit_missing_editor_binary_exits_1_with_message(monkeypatch, _isolated):
    # Reviewer finding (A2/B1): subprocess.call raises FileNotFoundError
    # (rather than returning a nonzero exit code) when the editor binary
    # doesn't exist -- edit_cmd must turn that into a clean CLI error, not
    # an uncaught traceback.
    monkeypatch.setenv("EDITOR", "definitely-not-a-real-editor-binary")
    monkeypatch.delenv("VISUAL", raising=False)

    def _raise(argv):
        raise FileNotFoundError(2, "No such file or directory", argv[0])

    monkeypatch.setattr("subprocess.call", _raise)
    result = runner.invoke(app, ["config", "edit"])
    assert result.exit_code == 1
    assert result.stderr
    assert not isinstance(result.exception, FileNotFoundError)


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses permission bits")
def test_set_unwritable_config_dir_exits_1_with_message(_isolated):
    # Prime settings construction (creates config_dir/sessions) while the
    # directory is still writable, so the read-only chmod below exercises
    # set_entry()'s own write failure rather than an unrelated PermissionError
    # from GraftpunkSettings.__init__'s directory creation in main_callback.
    runner.invoke(app, ["config", "path"])
    _isolated.chmod(0o500)
    try:
        result = runner.invoke(app, ["config", "set", "K", "v"])
        assert result.exit_code == 1
        assert result.stderr
    finally:
        _isolated.chmod(0o700)


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses permission bits")
def test_unset_unwritable_config_dir_exits_1_with_message(_isolated):
    runner.invoke(app, ["config", "set", "K", "v"])
    _isolated.chmod(0o500)
    try:
        result = runner.invoke(app, ["config", "unset", "K"])
        assert result.exit_code == 1
        assert result.stderr
    finally:
        _isolated.chmod(0o700)


def test_edit_reasserts_0600_after_editor_exits(monkeypatch, _isolated):
    # Simulate an editor that writes via replace-on-save, leaving the file
    # at the process umask's permissions (e.g. 0644) rather than 0600.
    def fake_editor_call(argv):
        (_isolated / "env").chmod(0o644)
        return 0

    monkeypatch.setenv("EDITOR", "some-editor")
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.setattr("subprocess.call", fake_editor_call)
    result = runner.invoke(app, ["config", "edit"])
    assert result.exit_code == 0
    assert stat.S_IMODE((_isolated / "env").stat().st_mode) == 0o600


def test_unset_non_utf8_file_exits_1_clean(_isolated):
    # Reviewer finding (A6): unset_entry() reads the file directly (unlike
    # load(), which already degrades a non-UTF-8 file to "empty"), so
    # unset_cmd must catch ValueError (UnicodeDecodeError) too, not just
    # OSError -- otherwise this is an uncaught traceback.
    (_isolated / "env").write_bytes(b"K=\xff\xfevalue\n")
    result = runner.invoke(app, ["config", "unset", "K"])
    assert result.exit_code == 1
    assert result.stderr
    assert not isinstance(result.exception, UnicodeDecodeError)


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses permission bits")
def test_config_path_survives_unreadable_env_file(_isolated):
    # Reviewer finding (A1): load() (run via ensure_bootstrap() in
    # main_callback for EVERY command) used to let stat()/read_text()
    # crash the whole CLI on an unreadable file -- including `gp config
    # path`, which needs no file access at all. (The stdout-stays-clean /
    # warning-goes-to-stderr half of this claim is covered by a spawned-
    # process test in test_workstation_env_lifecycle.py, matching how
    # test_bootstrap_warning_goes_to_stderr_not_stdout there already
    # covers the analogous permissive-mode warning -- CliRunner captures
    # stdout/stderr in a way that doesn't reflect real structlog routing.)
    (_isolated / "env").write_text("K=v\n")
    (_isolated / "env").chmod(0o000)
    try:
        result = runner.invoke(app, ["config", "path"])
    finally:
        (_isolated / "env").chmod(0o600)
    assert result.exit_code == 0
    assert str(_isolated / "env") in result.stdout
