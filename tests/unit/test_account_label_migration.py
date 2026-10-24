"""A login moves the slot graftpunk 1.24.0 named after a secret (#293).

1.24.0 could label a session with a PIN or a one-time code (``myshop@4417``).
The label now comes only from an allowlisted identifier field, so the next
login writes ``myshop`` (or ``myshop@<identifier>``). These tests run the real
login command on the real local backend and check that the old slot does not
survive beside the new one, which would make every command without
``--session`` ambiguous.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from graftpunk.cache import cache_session, get_session_metadata, list_sessions, load_session
from graftpunk.session_identity import GP_ACCOUNT_ATTR
from tests.unit.cli_harness import cacheable_browser_session, invoke_plugin_app, strip_ansi

PIN_FIELDS = {"userPin": "#pin", "password": "#password"}
PIN_CREDS = {"FMTSITE_USERPIN": "4417", "FMTSITE_PASSWORD": "pw-x"}
ACCOUNT_CREDS = {"FMTSITE_ACCOUNT": "acct-1", "FMTSITE_PASSWORD": "pw-x"}


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fresh_backend) -> Path:  # noqa: ANN001
    """No ambient pin, a scratch working directory and a scratch observe tree."""
    monkeypatch.delenv("GRAFTPUNK_SESSION", raising=False)
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    observe = tmp_path / "observe"
    observe.mkdir()
    monkeypatch.setattr("graftpunk.cli.login_commands.OBSERVE_BASE_DIR", observe)
    return observe


def _plugin(fields: dict[str, str], *, succeed: bool = True):  # noqa: ANN202
    """A plugin with *fields* as its login fields and a hand-written login.

    The login caches a fresh session through ``cache_login_session`` when
    *succeed*, and otherwise returns False without writing anything.
    """
    from graftpunk.plugins.cli_plugin import (
        LoginConfig,
        LoginStep,
        SitePlugin,
        cache_login_session,
        command,
    )

    class ShopPlugin(SitePlugin):
        site_name = "fmtsite"
        base_url = "https://fmt.example.com"
        session_name = "myshop"
        login_config = LoginConfig(steps=[LoginStep(fields=fields, submit="#go")])

        def login(self, credentials: dict[str, str]) -> bool:
            """Sign in to fmtsite."""
            if not succeed:
                return False
            session = cacheable_browser_session()
            session.cookies.set("fresh", "1")
            cache_login_session(self, session)
            return True

        @command(help="List items", requires_session=True)
        def items(self, ctx):  # noqa: ANN001, ANN202
            return {"cookies": sorted(c.name for c in ctx.session.cookies)}

    return ShopPlugin()


def _cache_stale(name: str = "myshop@4417", identifier: str | None = "4417") -> None:
    """A slot as 1.24.0 left it: named and recorded after the PIN."""
    session = cacheable_browser_session(identifier)
    session.cookies.set("stale", "1")
    cache_session(session, name)


def _login(plugin, **env: str):  # noqa: ANN001, ANN202
    return invoke_plugin_app(plugin, ["fmtsite", "login"], **{**PIN_CREDS, **env})


class TestRename:
    def test_the_stale_slot_is_renamed_when_only_it_exists(self) -> None:
        """A failed login writes nothing, so what is left is the renamed old session."""
        _cache_stale()

        result = _login(_plugin(PIN_FIELDS, succeed=False))

        assert result.exit_code != 0  # the login itself failed
        assert list_sessions() == ["myshop"]
        stored = get_session_metadata("myshop")
        assert stored is not None
        assert stored["account_identifier"] is None
        moved = load_session("myshop")
        assert getattr(moved, GP_ACCOUNT_ATTR) is None
        assert {c.name for c in moved.cookies} == {"stale"}

    def test_the_old_name_is_not_printed(self) -> None:
        _cache_stale()

        result = _login(_plugin(PIN_FIELDS))

        output = strip_ansi(result.output)
        assert result.exit_code == 0, output
        assert "4417" not in output
        assert "userPin" in output
        assert "myshop" in output

    def test_a_successful_login_leaves_one_slot(self) -> None:
        _cache_stale()

        result = _login(_plugin(PIN_FIELDS))

        assert result.exit_code == 0, result.output
        assert list_sessions() == ["myshop"]
        moved = load_session("myshop")
        assert {c.name for c in moved.cookies} == {"fresh"}

    def test_an_unreadable_stale_slot_is_deleted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from graftpunk.exceptions import SessionExpiredError

        _cache_stale()

        def expired(*_args: object, **_kwargs: object) -> None:
            raise SessionExpiredError("expired")

        monkeypatch.setattr("graftpunk.session_migration.move_session", expired)
        result = _login(_plugin(PIN_FIELDS, succeed=False))

        assert result.exit_code != 0
        assert list_sessions() == []


class TestDelete:
    def test_the_stale_slot_is_deleted_when_the_new_one_exists(self) -> None:
        _cache_stale()
        cache_session(cacheable_browser_session(), "myshop")

        result = _login(_plugin(PIN_FIELDS))

        assert result.exit_code == 0, result.output
        assert list_sessions() == ["myshop"]
        assert {c.name for c in load_session("myshop").cookies} == {"fresh"}

    def test_a_failed_login_deletes_nothing(self) -> None:
        """The stale slot goes only once the login has written the new one."""
        _cache_stale()
        cache_session(cacheable_browser_session(), "myshop")

        result = _login(_plugin(PIN_FIELDS, succeed=False))

        assert result.exit_code != 0
        assert list_sessions() == ["myshop", "myshop@4417"]

    def test_a_deleted_slots_runs_and_pin_follow(self, _isolated: Path) -> None:
        _cache_stale()
        cache_session(cacheable_browser_session(), "myshop")
        (_isolated / "myshop-4417" / "20261001-000000-aaaa").mkdir(parents=True)
        pin = Path.cwd() / ".gp-session"
        pin.write_text("myshop@4417")

        result = _login(_plugin(PIN_FIELDS))

        output = strip_ansi(result.output)
        assert result.exit_code == 0, output
        assert "Removed this account's earlier session" in output
        assert "4417" not in output
        assert (_isolated / "myshop" / "20261001-000000-aaaa").is_dir()
        assert pin.read_text() == "myshop"

    def test_a_stale_slot_recorded_for_another_account_is_left_alone(self) -> None:
        _cache_stale(identifier="someone@example.com")

        result = _login(_plugin(PIN_FIELDS))

        assert result.exit_code == 0, result.output
        assert list_sessions() == ["myshop", "myshop@4417"]


class TestCommandsAfterMigration:
    def test_a_command_without_session_is_not_ambiguous(self) -> None:
        _cache_stale()
        login = _login(_plugin(PIN_FIELDS))
        assert login.exit_code == 0, login.output

        result = invoke_plugin_app(_plugin(PIN_FIELDS), ["fmtsite", "items", "--format", "json"])

        assert result.exit_code == 0, result.output
        assert "Several sessions" not in result.output
        assert "fresh" in result.output

    def test_without_migration_the_same_state_is_ambiguous(self) -> None:
        """The failure the migration prevents: both slots cached under one base."""
        _cache_stale()
        cache_session(cacheable_browser_session(), "myshop")

        result = invoke_plugin_app(_plugin(PIN_FIELDS), ["fmtsite", "items"])

        assert result.exit_code != 0
        assert "myshop@4417" in strip_ansi(result.output)


class TestSecretIdentifierCleared:
    def test_an_identifier_that_named_no_slot_is_cleared(self) -> None:
        """A secret 1.24.0 recorded without a label (it slugified to nothing)."""
        cache_session(cacheable_browser_session("!!!"), "myshop")

        result = _login(_plugin(PIN_FIELDS, succeed=False), FMTSITE_USERPIN="!!!")

        assert result.exit_code != 0
        assert list_sessions() == ["myshop"]
        assert get_session_metadata("myshop")["account_identifier"] is None
        assert getattr(load_session("myshop"), GP_ACCOUNT_ATTR) is None


class TestUnchanged:
    def test_a_username_plugin_sees_no_change(self) -> None:
        cache_session(cacheable_browser_session("alice"), "myshop@alice")

        result = invoke_plugin_app(
            _plugin({"username": "#u", "password": "#p"}),
            ["fmtsite", "login"],
            **{"FMTSITE_USERNAME": "alice", "FMTSITE_PASSWORD": "pw-x"},
        )

        output = strip_ansi(result.output)
        assert result.exit_code == 0, output
        assert list_sessions() == ["myshop@alice"]
        assert get_session_metadata("myshop@alice")["account_identifier"] == "alice"
        assert "earlier session" not in output

    def test_an_explicit_label_leaves_a_bare_slot_alone(self) -> None:
        """``account`` was never derived from, so 1.24.0 named that login bare."""
        cache_session(cacheable_browser_session(), "myshop")

        result = invoke_plugin_app(
            _plugin({"account": "#a", "password": "#p"}),
            ["fmtsite", "login", "--as", "work"],
            **ACCOUNT_CREDS,
        )

        assert result.exit_code == 0, result.output
        assert list_sessions() == ["myshop", "myshop@work"]

    def test_a_bare_slot_moves_to_a_newly_derived_label(self) -> None:
        cache_session(cacheable_browser_session(), "myshop")

        result = invoke_plugin_app(
            _plugin({"account": "#a", "password": "#p"}),
            ["fmtsite", "login"],
            **ACCOUNT_CREDS,
        )

        assert result.exit_code == 0, result.output
        assert list_sessions() == ["myshop@acct-1"]


class TestPinsAndRuns:
    def test_a_directory_pin_on_the_old_slot_is_rewritten(self) -> None:
        _cache_stale()
        pin = Path.cwd() / ".gp-session"
        pin.write_text("myshop@4417\n")

        result = _login(_plugin(PIN_FIELDS))

        assert result.exit_code == 0, result.output
        assert pin.read_text() == "myshop"

    def test_a_directory_pin_on_another_slot_is_kept(self) -> None:
        _cache_stale()
        pin = Path.cwd() / ".gp-session"
        pin.write_text("othersite@bob")

        _login(_plugin(PIN_FIELDS))

        assert pin.read_text() == "othersite@bob"

    def test_an_env_pin_on_the_old_slot_is_reported(self) -> None:
        _cache_stale()

        result = _login(_plugin(PIN_FIELDS), GRAFTPUNK_SESSION="myshop@4417")

        output = strip_ansi(result.output)
        assert result.exit_code == 0, output
        assert "GRAFTPUNK_SESSION names the old session" in output
        assert "set it to myshop." in output
        assert "4417" not in output

    def test_observe_runs_move_to_the_new_session(self, _isolated: Path) -> None:
        old_run = _isolated / "myshop-4417" / "20261001-000000-aaaa"
        old_run.mkdir(parents=True)
        (old_run / "network.har").write_text("{}")
        _cache_stale()

        result = _login(_plugin(PIN_FIELDS))

        assert result.exit_code == 0, result.output
        assert (_isolated / "myshop" / "20261001-000000-aaaa" / "network.har").read_text() == "{}"
        assert not (_isolated / "myshop-4417").exists()

    def test_a_colliding_run_stays_where_it_was(self, _isolated: Path) -> None:
        for base in ("myshop-4417", "myshop"):
            run = _isolated / base / "20261001-000000-aaaa"
            run.mkdir(parents=True)
            (run / "network.har").write_text(base)
        _cache_stale()

        _login(_plugin(PIN_FIELDS))

        assert (_isolated / "myshop" / "20261001-000000-aaaa" / "network.har").read_text() == (
            "myshop"
        )
        assert (_isolated / "myshop-4417" / "20261001-000000-aaaa").is_dir()
