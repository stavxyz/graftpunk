"""Credential resolution: env -> workstation file -> prompt (spec § point (a))."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest
import typer

from graftpunk import workstation_env
from graftpunk.cli.login_commands import make_login_body


@pytest.fixture(autouse=True)
def _fresh(tmp_path, monkeypatch):
    monkeypatch.setenv("GRAFTPUNK_CONFIG_DIR", str(tmp_path))
    workstation_env._invalidate_cache()
    yield tmp_path
    workstation_env._invalidate_cache()


def _plugin(site="acme", **attrs):
    # session_name is part of CLIPluginProtocol (SitePlugin defaults it to
    # site_name); make_login_body reads it to name the cached session.
    attrs.setdefault("session_name", site)
    return SimpleNamespace(site_name=site, **attrs)


def test_file_static_used_when_env_missing(_fresh, monkeypatch):
    (_fresh / "env").write_text("ACME_USERNAME=filed-user\nACME_PASSWORD=filed-pass\n")
    monkeypatch.delenv("ACME_USERNAME", raising=False)
    monkeypatch.delenv("ACME_PASSWORD", raising=False)
    captured = {}

    def login(credentials):
        captured.update(credentials)
        return True

    body = make_login_body(_plugin(), login, {"username": "#u", "password": "#p"})
    body(SimpleNamespace())
    assert captured == {"username": "filed-user", "password": "filed-pass"}


def test_env_beats_file(_fresh, monkeypatch):
    (_fresh / "env").write_text("ACME_USERNAME=filed-user\nACME_PASSWORD=filed-pass\n")
    monkeypatch.setenv("ACME_USERNAME", "env-user")
    monkeypatch.setenv("ACME_PASSWORD", "env-pass")
    captured = {}

    def login(credentials):
        captured.update(credentials)
        return True

    body = make_login_body(_plugin(), login, {"username": "#u", "password": "#p"})
    body(SimpleNamespace())
    assert captured["username"] == "env-user"


def test_command_failure_falls_through_to_prompt(_fresh, monkeypatch):
    (_fresh / "env").write_text("ACME_PASSWORD=$(exit 9)\n")
    monkeypatch.delenv("ACME_PASSWORD", raising=False)
    monkeypatch.setenv("ACME_USERNAME", "env-user")
    captured = {}

    def login(credentials):
        captured.update(credentials)
        return True

    body = make_login_body(_plugin(), login, {"username": "#u", "password": "#p"})
    with patch.object(typer, "prompt", return_value="typed-pass") as prompt:
        body(SimpleNamespace())
    assert captured["password"] == "typed-pass"  # noqa: S105
    prompt.assert_called_once()


def test_envvar_override_resolves_from_file(_fresh, monkeypatch):
    (_fresh / "env").write_text("CUSTOM_USER_VAR=overridden-user\nACME_PASSWORD=pw\n")
    monkeypatch.delenv("CUSTOM_USER_VAR", raising=False)
    monkeypatch.delenv("ACME_PASSWORD", raising=False)
    captured = {}

    def login(credentials):
        captured.update(credentials)
        return True

    plugin = _plugin(username_envvar="CUSTOM_USER_VAR")
    body = make_login_body(plugin, login, {"username": "#u", "password": "#p"})
    body(SimpleNamespace())
    assert captured["username"] == "overridden-user"


def test_false_result_message_is_cause_neutral(_fresh, monkeypatch):
    """login() returning False means 'did not complete', not 'bad credentials'.

    The engine already logged what it detected (failure text, missing success
    element, rate limiting, empty field); the CLI must not name a cause it did
    not measure (issue #148 item 2).
    """
    monkeypatch.setenv("ACME_USERNAME", "u")
    monkeypatch.setenv("ACME_PASSWORD", "p")

    body = make_login_body(
        _plugin(), lambda credentials: False, {"username": "#u", "password": "#p"}
    )
    with (
        patch("graftpunk.cli.login_commands.gp_console.error") as mock_error,
        pytest.raises(SystemExit),
    ):
        body(SimpleNamespace())

    message = mock_error.call_args.args[0]
    assert "acme" in message
    assert "did not complete" in message
    assert "credentials" not in message.lower()
    assert "--observe=full" in message
