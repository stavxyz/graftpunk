"""What a login does with the slots graftpunk 1.24.0 named after a secret (#293).

1.24.0 could label a session with a PIN, a password or a one-time code
(``myshop@4417``). The label now comes only from an identifier field, so the
next login writes ``myshop`` (or ``myshop@<identifier>``). These tests run the
real login command on the real local backend. They check that the old slot
does not stay beside the new one when the credentials name it, that no session
is lost on the way, that the old name never reaches the output or a log line,
and that a slot gp cannot attribute is masked rather than touched.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from graftpunk.cache import cache_session, get_session_metadata, list_sessions, load_session
from graftpunk.session_identity import GP_ACCOUNT_ATTR
from tests.unit.cli_harness import cacheable_browser_session, invoke_plugin_app, strip_ansi

PIN_FIELDS = {"userPin": "#pin", "password": "#password"}
PIN_CREDS = {"FMTSITE_USERPIN": "4417", "FMTSITE_PASSWORD": "pw-x"}
# 1.24.0 picked loginPin (a static secret); the label now comes from user_id.
LABELLED_FIELDS = {"loginPin": "#pin", "user_id": "#u", "password": "#p"}
LABELLED_CREDS = {"FMTSITE_LOGINPIN": "4417", "FMTSITE_USER_ID": "bob", "FMTSITE_PASSWORD": "pw-x"}
CODE_FIELD = "loginCode"
CODE_FIELDS = {"loginCode": "#c", "user_id": "#u", "password": "#p"}
CODE_CREDS = {"FMTSITE_LOGINCODE": "111222", "FMTSITE_USER_ID": "bob", "FMTSITE_PASSWORD": "pw-x"}


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


def _plugin(fields: dict[str, str], *, succeed: bool = True, write: bool = True):  # noqa: ANN202
    """A plugin with *fields* as its login fields and a hand-written login.

    The login caches a fresh session through ``cache_login_session`` when
    *succeed* and *write*; with *succeed* False it returns False, and with
    *write* False it returns True having cached nothing.
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
            if write:
                session = cacheable_browser_session()
                session.cookies.set("fresh", "1")
                cache_login_session(self, session)
            return True

        @command(help="List items", requires_session=True)
        def items(self, ctx):  # noqa: ANN001, ANN202
            return {"cookies": sorted(c.name for c in ctx.session.cookies)}

    return ShopPlugin()


def _cache_legacy(name: str, identifier: str | None, cookie: str = "stale") -> None:
    """A slot as 1.24.0 left it: no account rule recorded."""
    session = cacheable_browser_session(identifier)
    session.cookies.set(cookie, "1")
    cache_session(session, name, account_rule=None)


def _cache_stale(name: str = "myshop@4417", identifier: str | None = "4417") -> None:
    """A slot as 1.24.0 left it: named and recorded after the PIN."""
    _cache_legacy(name, identifier)


def _login(plugin, **env: str):  # noqa: ANN001, ANN202
    return invoke_plugin_app(plugin, ["fmtsite", "login"], **{**PIN_CREDS, **env})


def _cookies(name: str) -> set[str]:
    return {c.name for c in load_session(name).cookies}


def _metadata_path(name: str) -> Path:
    from graftpunk.config import get_settings

    return Path(get_settings().sessions_dir) / name / "metadata.json"


class TestRenameToTheBareName:
    def test_the_stale_slot_is_renamed_when_only_it_exists(self) -> None:
        """A failed login writes nothing, so what is left is the renamed old session."""
        _cache_stale()
        before = get_session_metadata("myshop@4417")

        result = _login(_plugin(PIN_FIELDS, succeed=False))

        assert result.exit_code != 0  # the login itself failed
        assert list_sessions() == ["myshop"]
        stored = get_session_metadata("myshop")
        assert stored["account_identifier"] is None
        assert stored["account_rule"] == 2
        for kept in ("created_at", "modified_at", "expires_at", "status"):
            assert stored[kept] == before[kept], kept
        moved = load_session("myshop")
        assert getattr(moved, GP_ACCOUNT_ATTR) is None
        assert {c.name for c in moved.cookies} == {"stale"}

    def test_the_pickled_session_name_is_the_new_one(self) -> None:
        """A Selenium login stores its slot name in the pickle (login_engine sets session_name)."""
        session = cacheable_browser_session("4417")
        session._session_name = "myshop@4417"
        cache_session(session, "myshop@4417", account_rule=None)

        _login(_plugin(PIN_FIELDS, succeed=False))

        assert load_session("myshop")._session_name == "myshop"

    def test_the_old_name_is_not_printed(self) -> None:
        _cache_stale()

        result = _login(_plugin(PIN_FIELDS))

        output = strip_ansi(result.output)
        assert result.exit_code == 0, output
        assert "4417" not in output
        assert "Renamed this account's earlier session to 'myshop'" in output
        assert "userPin" in output

    def test_a_successful_login_leaves_one_slot(self) -> None:
        _cache_stale()

        result = _login(_plugin(PIN_FIELDS))

        assert result.exit_code == 0, result.output
        assert list_sessions() == ["myshop"]
        assert _cookies("myshop") == {"fresh"}

    @pytest.mark.parametrize("error", [OSError("disk full"), "storage"])
    def test_a_failed_copy_keeps_the_old_slot(
        self, monkeypatch: pytest.MonkeyPatch, error: object
    ) -> None:
        from graftpunk import cache as cache_mod
        from graftpunk.exceptions import StorageError

        _cache_stale()
        backend = cache_mod._get_session_storage_backend()
        real_save = backend.save_session
        failure = StorageError("transient network error") if error == "storage" else error

        def flaky_save(name, data, meta):  # noqa: ANN001, ANN202
            if name == "myshop":
                raise failure  # type: ignore[misc]
            return real_save(name, data, meta)

        monkeypatch.setattr(backend, "save_session", flaky_save)
        result = _login(_plugin(PIN_FIELDS, succeed=False))

        output = strip_ansi(result.output)
        assert result.exit_code != 0
        assert "myshop@4417" in list_sessions()
        assert _cookies("myshop@4417") == {"stale"}
        assert "Could not move this account's earlier session, named from the userPin field" in (
            output
        )
        assert "4417" not in output

    def test_a_copy_that_reads_back_wrong_keeps_the_old_slot(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from graftpunk import cache as cache_mod

        _cache_stale()
        backend = cache_mod._get_session_storage_backend()
        real_save = backend.save_session

        def corrupting_save(name, data, meta):  # noqa: ANN001, ANN202
            return real_save(name, data[:-4] + b"xxxx" if name == "myshop" else data, meta)

        monkeypatch.setattr(backend, "save_session", corrupting_save)
        result = _login(_plugin(PIN_FIELDS, succeed=False))

        assert result.exit_code != 0
        assert "myshop@4417" in list_sessions()
        assert _cookies("myshop@4417") == {"stale"}

    def test_a_failed_copy_is_still_removed_after_a_successful_login(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from graftpunk import session_migration

        _cache_stale()

        def failing_copy(*_args: object, **_kwargs: object) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(session_migration, "copy_session", failing_copy)
        result = _login(_plugin(PIN_FIELDS))

        assert result.exit_code == 0, result.output
        assert list_sessions() == ["myshop"]
        assert _cookies("myshop") == {"fresh"}

    def test_a_failed_delete_after_the_copy_says_so(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from graftpunk import cache as cache_mod

        _cache_stale()
        backend = cache_mod._get_session_storage_backend()
        real_delete = backend.delete_session
        monkeypatch.setattr(
            backend,
            "delete_session",
            lambda name: False if name == "myshop@4417" else real_delete(name),
        )
        result = _login(_plugin(PIN_FIELDS, succeed=False))

        output = strip_ansi(result.output)
        assert sorted(list_sessions()) == ["myshop", "myshop@4417"]
        assert "Could not remove this account's earlier session, named from the userPin" in output
        assert "Renamed" not in output
        assert "4417" not in output

    def test_a_slot_another_login_already_moved_is_not_reported(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from graftpunk import session_migration
        from graftpunk.exceptions import SessionNotFoundError

        _cache_stale()

        def gone(*_args: object, **_kwargs: object) -> None:
            raise SessionNotFoundError("moved by a concurrent login")

        monkeypatch.setattr(session_migration, "copy_session", gone)
        result = _login(_plugin(PIN_FIELDS, succeed=False))

        output = strip_ansi(result.output)
        assert "earlier session" not in output
        assert list_sessions() == ["myshop@4417"]

    def test_an_expired_stale_slot_is_deleted_without_its_name_in_the_output(self) -> None:
        _cache_stale()
        path = _metadata_path("myshop@4417")
        data = json.loads(path.read_text())
        data["expires_at"] = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        path.write_text(json.dumps(data))

        result = _login(_plugin(PIN_FIELDS, succeed=False))

        output = strip_ansi(result.output)
        assert list_sessions() == []
        assert "Removed this account's earlier session" in output
        assert "4417" not in output


class TestSuperseded:
    def test_the_stale_slot_is_deleted_after_the_login_writes_the_new_one(self) -> None:
        _cache_stale()
        cache_session(cacheable_browser_session(), "myshop")

        result = _login(_plugin(PIN_FIELDS))

        assert result.exit_code == 0, result.output
        assert list_sessions() == ["myshop"]
        assert _cookies("myshop") == {"fresh"}

    def test_a_failed_login_deletes_nothing(self) -> None:
        _cache_stale()
        cache_session(cacheable_browser_session(), "myshop")

        result = _login(_plugin(PIN_FIELDS, succeed=False))

        assert result.exit_code != 0
        assert list_sessions() == ["myshop", "myshop@4417"]

    def test_a_labelled_target_waits_for_the_login(self) -> None:
        """A labelled name claims an account; the old slot is not given it before the login."""
        _cache_stale()

        failed = invoke_plugin_app(
            _plugin(LABELLED_FIELDS, succeed=False), ["fmtsite", "login"], **LABELLED_CREDS
        )
        assert failed.exit_code != 0
        assert list_sessions() == ["myshop@4417"]

        ok = invoke_plugin_app(_plugin(LABELLED_FIELDS), ["fmtsite", "login"], **LABELLED_CREDS)
        output = strip_ansi(ok.output)
        assert ok.exit_code == 0, output
        assert list_sessions() == ["myshop@bob"]
        assert "Removed this account's earlier session" in output
        assert "4417" not in output

    def test_a_login_that_wrote_nothing_keeps_the_old_slot(self) -> None:
        _cache_stale()
        cache_session(cacheable_browser_session(), "myshop")

        result = _login(_plugin(PIN_FIELDS, write=False))

        output = strip_ansi(result.output)
        assert result.exit_code == 0, output
        assert list_sessions() == ["myshop", "myshop@4417"]
        assert "Kept this account's earlier session, named from the userPin field" in output

    def test_an_unreadable_stale_slot_waits_and_never_shows_its_name(self) -> None:
        _cache_stale()
        from graftpunk.config import get_settings

        pickle_path = Path(get_settings().sessions_dir) / "myshop@4417" / "session.pickle"
        pickle_path.write_bytes(b"garbage")

        failed = _login(_plugin(PIN_FIELDS, succeed=False))
        assert "myshop@4417" in list_sessions()
        assert "4417" not in strip_ansi(failed.output)

        ok = _login(_plugin(PIN_FIELDS))
        assert ok.exit_code == 0, ok.output
        assert list_sessions() == ["myshop"]
        assert "4417" not in strip_ansi(ok.output)


class TestCommandsAfterMigration:
    def test_a_command_without_session_is_not_ambiguous(self) -> None:
        _cache_stale()
        login = _login(_plugin(PIN_FIELDS))
        assert login.exit_code == 0, login.output

        result = invoke_plugin_app(_plugin(PIN_FIELDS), ["fmtsite", "items", "--format", "json"])

        assert result.exit_code == 0, result.output
        assert "Several sessions" not in result.output
        assert "fresh" in result.output


class TestOneTimeCodes:
    """A code differs every login, so its slot is never this login's stale slot."""

    def test_the_slot_is_left_and_counted_without_its_name(self) -> None:
        _cache_legacy("myshop@829341", "829341")

        result = invoke_plugin_app(_plugin(CODE_FIELDS), ["fmtsite", "login"], **CODE_CREDS)

        output = strip_ansi(result.output)
        assert result.exit_code == 0, output
        assert sorted(list_sessions()) == ["myshop@829341", "myshop@bob"]
        assert "Another session of myshop cached by graftpunk 1.24.0 or earlier" in output
        assert "loginCode" in output
        assert "829341" not in output

    def test_the_ambiguity_error_masks_its_name(self) -> None:
        _cache_legacy("myshop@829341", "829341")
        login = invoke_plugin_app(_plugin(CODE_FIELDS), ["fmtsite", "login"], **CODE_CREDS)
        assert login.exit_code == 0, login.output

        result = invoke_plugin_app(_plugin(CODE_FIELDS), ["fmtsite", "items"])

        output = strip_ansi(result.output)
        assert result.exit_code != 0
        assert "Several sessions cached for 'myshop'" in output
        assert "myshop@<earlier label>" in output
        assert "myshop@bob" in output
        assert "829341" not in output
        assert "gp session clear" in output

    def test_slots_this_release_wrote_are_not_masked(self) -> None:
        for creds in (CODE_CREDS, {**CODE_CREDS, "FMTSITE_USER_ID": "carol"}):
            login = invoke_plugin_app(_plugin(CODE_FIELDS), ["fmtsite", "login"], **creds)
            assert login.exit_code == 0, login.output

        result = invoke_plugin_app(_plugin(CODE_FIELDS), ["fmtsite", "items"])

        output = strip_ansi(result.output)
        assert "myshop@bob" in output
        assert "myshop@carol" in output
        assert "<earlier label>" not in output

    def test_an_as_slot_recording_a_code_shows_no_account(self) -> None:
        _cache_legacy("myshop@work", "829341")
        _cache_legacy("myshop@home", "551234")

        result = invoke_plugin_app(_plugin(CODE_FIELDS), ["fmtsite", "items"])

        output = strip_ansi(result.output)
        assert "myshop@work" in output
        assert "myshop@home" in output
        assert "829341" not in output
        assert "551234" not in output

    def test_a_pin_on_such_a_slot_is_masked_in_the_pin_hint(self) -> None:
        _cache_legacy("myshop@829341", "829341")

        result = invoke_plugin_app(
            _plugin(CODE_FIELDS),
            ["fmtsite", "login"],
            **{**CODE_CREDS, "GRAFTPUNK_SESSION": "myshop@829341"},
        )

        output = strip_ansi(result.output)
        assert result.exit_code == 0, output
        assert "This shell is pinned to myshop@<earlier label>" in output
        assert "829341" not in output


class TestTheMarkSurvivesWriteBacks:
    """A slot 1.24.0 named stays recognisable after this release refreshes it."""

    def test_a_command_write_back(self) -> None:
        from graftpunk.cache import update_session_cookies

        _cache_legacy("myshop@829341", "829341")
        session = load_session("myshop@829341")
        session.cookies.set("refreshed", "1")

        update_session_cookies(session, "myshop@829341")

        stored = get_session_metadata("myshop@829341")
        assert stored["cookie_count"] == 2
        assert stored["account_rule"] is None

    def test_a_command_scope_write_through_cache_login_session(self) -> None:
        from graftpunk.plugins.cli_plugin import cache_login_session

        _cache_legacy("myshop@829341", "829341")

        cache_login_session(_plugin(CODE_FIELDS), cacheable_browser_session(), name="myshop@829341")

        stored = get_session_metadata("myshop@829341")
        assert stored["account_identifier"] == "829341"
        assert stored["account_rule"] is None

    def test_a_login_marks_its_slot(self) -> None:
        login = invoke_plugin_app(_plugin(CODE_FIELDS), ["fmtsite", "login"], **CODE_CREDS)
        assert login.exit_code == 0, login.output

        assert get_session_metadata("myshop@bob")["account_rule"] == 2


def _session_list() -> str:
    import typer
    from typer.testing import CliRunner

    from graftpunk.cli.session_commands import session_app

    app = typer.Typer()
    app.add_typer(session_app, name="session")
    result = CliRunner().invoke(app, ["session", "list"], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.output
    return strip_ansi(result.output)


def _expire(name: str) -> None:
    path = _metadata_path(name)
    data = json.loads(path.read_text())
    data["expires_at"] = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    path.write_text(json.dumps(data))


class TestTheRecordOfAnAsSlot:
    """A 1.24.0 ``--as`` slot recorded the secret field's value as its account.

    It is replaced on a successful login, never before, whatever value it
    holds: a code from an earlier login, or a PIN that has changed since.
    """

    @pytest.mark.parametrize(
        ("fields", "creds", "recorded", "expired", "account"),
        [
            pytest.param(PIN_FIELDS, PIN_CREDS, "4417", False, None, id="same-pin"),
            pytest.param(PIN_FIELDS, PIN_CREDS, "1111", False, None, id="changed-pin"),
            pytest.param(PIN_FIELDS, PIN_CREDS, "4417", True, None, id="expired"),
            pytest.param(CODE_FIELDS, CODE_CREDS, "829341", False, "bob", id="code"),
            pytest.param(
                {"loginCode": "#c", "password": "#p"},
                {"FMTSITE_LOGINCODE": "111222", "FMTSITE_PASSWORD": "pw-x"},
                "829341",
                False,
                None,
                id="code-no-identifier",
            ),
            pytest.param(
                {"loginCode": "#c", "password": "#p"},
                {"FMTSITE_LOGINCODE": "111222", "FMTSITE_PASSWORD": "pw-x"},
                "829341",
                True,
                None,
                id="code-expired",
            ),
        ],
    )
    def test_a_successful_login_replaces_it(
        self,
        fields: dict[str, str],
        creds: dict[str, str],
        recorded: str,
        expired: bool,
        account: str | None,
    ) -> None:
        _cache_legacy("myshop@work", recorded)
        if expired:
            _expire("myshop@work")

        result = invoke_plugin_app(_plugin(fields), ["fmtsite", "login", "--as", "work"], **creds)

        output = strip_ansi(result.output)
        assert result.exit_code == 0, output
        stored = get_session_metadata("myshop@work")
        assert stored["account_identifier"] == account
        assert stored["account_rule"] == 2
        assert getattr(load_session("myshop@work"), GP_ACCOUNT_ATTR) == account
        assert recorded not in output
        assert "was recorded for" not in output
        assert "Replaced the account recorded on 'myshop@work'" in output
        assert recorded not in _session_list()

    def test_a_login_that_writes_nothing_still_replaces_it(self) -> None:
        _cache_legacy("myshop@work", "829341")
        _expire("myshop@work")

        result = invoke_plugin_app(
            _plugin(CODE_FIELDS, write=False), ["fmtsite", "login", "--as", "work"], **CODE_CREDS
        )

        assert result.exit_code == 0, result.output
        stored = get_session_metadata("myshop@work")
        assert stored["account_identifier"] == "bob"
        assert stored["account_rule"] == 2
        assert "829341" not in strip_ansi(result.output)

    def test_a_refresh_after_a_metadata_only_replacement_keeps_it(self) -> None:
        """The pickle still holds the old value; metadata under the current rule wins."""
        from graftpunk.cache import update_session_cookies

        _cache_legacy("myshop@work", "829341")
        result = invoke_plugin_app(
            _plugin(CODE_FIELDS, write=False), ["fmtsite", "login", "--as", "work"], **CODE_CREDS
        )
        assert result.exit_code == 0, result.output
        session = load_session("myshop@work")
        session.cookies.set("refreshed", "1")

        update_session_cookies(session, "myshop@work")

        assert get_session_metadata("myshop@work")["account_identifier"] == "bob"

    @pytest.mark.parametrize("recorded", ["4417", "1111"])
    def test_a_failed_login_changes_nothing(self, recorded: str) -> None:
        _cache_legacy("myshop@work", recorded)
        before = _metadata_path("myshop@work").read_bytes()

        result = invoke_plugin_app(
            _plugin(PIN_FIELDS, succeed=False), ["fmtsite", "login", "--as", "work"], **PIN_CREDS
        )

        assert result.exit_code != 0
        assert _metadata_path("myshop@work").read_bytes() == before
        assert recorded not in strip_ansi(result.output)

    def test_an_identifier_plugins_record_is_not_touched(self) -> None:
        """The condition: only a plugin 1.24.0 labelled from a secret field."""
        _cache_legacy("myshop@work", "alice")

        result = invoke_plugin_app(
            _plugin({"username": "#u", "password": "#p"}, write=False),
            ["fmtsite", "login", "--as", "work"],
            **{"FMTSITE_USERNAME": "bob", "FMTSITE_PASSWORD": "pw-x"},
        )

        output = strip_ansi(result.output)
        assert result.exit_code == 0, output
        assert get_session_metadata("myshop@work")["account_identifier"] == "alice"
        assert "was recorded for alice; this login replaces it as bob" in output

    def test_a_current_record_is_not_touched(self) -> None:
        """The condition: only a slot with no account rule."""
        cache_session(cacheable_browser_session("carol"), "myshop@work")

        result = invoke_plugin_app(
            _plugin(CODE_FIELDS, write=False), ["fmtsite", "login", "--as", "work"], **CODE_CREDS
        )

        output = strip_ansi(result.output)
        assert result.exit_code == 0, output
        assert get_session_metadata("myshop@work")["account_identifier"] == "carol"
        assert "was recorded for carol; this login replaces it as bob" in output

    def test_the_stale_slot_waits_for_an_as_login(self) -> None:
        _cache_stale()

        failed = invoke_plugin_app(
            _plugin(PIN_FIELDS, succeed=False), ["fmtsite", "login", "--as", "work"], **PIN_CREDS
        )
        assert failed.exit_code != 0
        assert list_sessions() == ["myshop@4417"]

        ok = invoke_plugin_app(
            _plugin(PIN_FIELDS), ["fmtsite", "login", "--as", "work"], **PIN_CREDS
        )
        assert ok.exit_code == 0, ok.output
        assert list_sessions() == ["myshop@work"]


class TestNothingBrokenIsLeft:
    def test_a_half_written_copy_is_removed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from graftpunk import cache as cache_mod

        _cache_stale()
        backend = cache_mod._get_session_storage_backend()
        real_save = backend.save_session

        def half_save(name, data, meta):  # noqa: ANN001, ANN202
            if name == "myshop":
                slot = backend.base_dir / name
                slot.mkdir(parents=True, exist_ok=True)
                (slot / "session.pickle").write_bytes(data)
                raise OSError("disk full writing metadata")
            return real_save(name, data, meta)

        monkeypatch.setattr(backend, "save_session", half_save)
        result = _login(_plugin(PIN_FIELDS, succeed=False))
        monkeypatch.setattr(backend, "save_session", real_save)

        assert result.exit_code != 0
        assert list_sessions() == ["myshop@4417"]
        command = invoke_plugin_app(_plugin(PIN_FIELDS), ["fmtsite", "items", "--format", "json"])
        assert command.exit_code == 0, command.output
        assert "stale" in command.output

    def test_a_copy_that_reads_back_wrong_is_removed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from graftpunk import cache as cache_mod

        _cache_stale()
        backend = cache_mod._get_session_storage_backend()
        real_save = backend.save_session

        def corrupting_save(name, data, meta):  # noqa: ANN001, ANN202
            return real_save(name, data[:-4] + b"xxxx" if name == "myshop" else data, meta)

        monkeypatch.setattr(backend, "save_session", corrupting_save)
        result = _login(_plugin(PIN_FIELDS, succeed=False))

        assert result.exit_code != 0
        assert list_sessions() == ["myshop@4417"]

    def test_a_local_save_that_fails_leaves_the_slot_as_it_was(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from graftpunk.storage import local

        _cache_legacy("myshop@work", None, cookie="before")
        real_write = local._write_private_temp

        def full_disk(target, data):  # noqa: ANN001, ANN202
            if target.name == "metadata.json":
                raise OSError(28, "No space left on device")
            return real_write(target, data)

        monkeypatch.setattr(local, "_write_private_temp", full_disk)
        replacement = cacheable_browser_session()
        replacement.cookies.set("after", "1")
        with pytest.raises(OSError):
            cache_session(replacement, "myshop@work")
        with pytest.raises(OSError):
            cache_session(replacement, "myshop@new")
        monkeypatch.setattr(local, "_write_private_temp", real_write)

        assert _cookies("myshop@work") == {"before"}
        assert list_sessions() == ["myshop@work"]
        leftovers = [p.name for p in _metadata_path("myshop@work").parent.iterdir()]
        assert sorted(leftovers) == ["metadata.json", "session.pickle"]
        assert not _metadata_path("myshop@new").parent.exists()

    @pytest.mark.parametrize("error", [OSError(13, "Permission denied"), KeyboardInterrupt()])
    def test_a_failure_between_the_two_renames_leaves_the_slot_as_it_was(
        self, monkeypatch: pytest.MonkeyPatch, error: BaseException
    ) -> None:
        import os

        from graftpunk.storage import local

        _cache_legacy("myshop@work", None, cookie="before")
        real_replace = os.replace

        def failing_metadata_rename(src, dst):  # noqa: ANN001, ANN202
            if str(dst).endswith("metadata.json"):
                raise error
            return real_replace(src, dst)

        monkeypatch.setattr(local.os, "replace", failing_metadata_rename)
        replacement = cacheable_browser_session()
        replacement.cookies.set("after", "1")
        for name in ("myshop@work", "myshop@new"):
            with pytest.raises(type(error)):
                cache_session(replacement, name)
        monkeypatch.setattr(local.os, "replace", real_replace)

        assert list_sessions() == ["myshop@work"]
        assert _cookies("myshop@work") == {"before"}
        assert sorted(p.name for p in _metadata_path("myshop@work").parent.iterdir()) == [
            "metadata.json",
            "session.pickle",
        ]
        assert not _metadata_path("myshop@new").parent.exists()


class TestTwoAccountsOfAPinPlugin:
    def test_a_failed_login_does_not_put_one_account_where_the_other_lands(self) -> None:
        _cache_legacy("myshop@1111", "1111", cookie="acct_a")
        _cache_legacy("myshop@2222", "2222", cookie="acct_b")

        a = _login(_plugin(PIN_FIELDS, succeed=False), FMTSITE_USERPIN="1111")
        assert a.exit_code != 0
        assert list_sessions() == ["myshop@1111", "myshop@2222"]

        b = _login(_plugin(PIN_FIELDS), FMTSITE_USERPIN="2222")
        output = strip_ansi(b.output)
        assert b.exit_code == 0, output
        assert list_sessions() == ["myshop", "myshop@1111"]
        assert _cookies("myshop@1111") == {"acct_a"}
        assert "1111" not in output
        assert "2222" not in output


class TestUnchanged:
    def test_a_username_plugin_sees_no_change(self) -> None:
        _cache_legacy("myshop@alice", "alice")

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

    def test_an_identifier_field_plugin_sees_no_rename(self) -> None:
        _cache_legacy("myshop@alice-example-com", "alice@example.com")

        result = invoke_plugin_app(
            _plugin({"identifier": "#id", "password": "#p"}),
            ["fmtsite", "login"],
            **{"FMTSITE_IDENTIFIER": "alice@example.com", "FMTSITE_PASSWORD": "pw-x"},
        )

        output = strip_ansi(result.output)
        assert result.exit_code == 0, output
        assert list_sessions() == ["myshop@alice-example-com"]
        assert "earlier session" not in output

    def test_a_user_name_and_email_plugin_keeps_its_email_label(self) -> None:
        _cache_legacy("myshop@alice-example-com", "alice@example.com")

        result = invoke_plugin_app(
            _plugin({"user_name": "#u", "email": "#e", "password": "#p"}),
            ["fmtsite", "login"],
            **{
                "FMTSITE_USER_NAME": "alice",
                "FMTSITE_EMAIL": "alice@example.com",
                "FMTSITE_PASSWORD": "pw-x",
            },
        )

        assert result.exit_code == 0, result.output
        assert list_sessions() == ["myshop@alice-example-com"]

    @pytest.mark.parametrize("succeed", [False, True])
    def test_a_bare_slot_of_an_account_plugin_is_never_moved(self, succeed: bool) -> None:
        """1.24.0 cached every account of an ``account`` plugin bare; it still does."""
        _cache_legacy("myshop", None, cookie="account_a")

        result = invoke_plugin_app(
            _plugin({"account_id": "#a", "password": "#p"}, succeed=succeed),
            ["fmtsite", "login"],
            **{"FMTSITE_ACCOUNT_ID": "acct-b", "FMTSITE_PASSWORD": "pw-x"},
        )

        assert list_sessions() == ["myshop"]
        assert _cookies("myshop") == ({"fresh"} if succeed else {"account_a"})
        assert "earlier session" not in strip_ansi(result.output)

    def test_a_stale_slot_recorded_for_another_account_is_left_alone(self) -> None:
        _cache_stale(identifier="someone@example.com")

        result = _login(_plugin(PIN_FIELDS))

        assert result.exit_code == 0, result.output
        assert list_sessions() == ["myshop", "myshop@4417"]

    def test_a_slot_this_release_wrote_is_not_taken_for_a_stale_one(self) -> None:
        cache_session(cacheable_browser_session("4417"), "myshop@4417")

        result = _login(_plugin(PIN_FIELDS))

        assert result.exit_code == 0, result.output
        assert list_sessions() == ["myshop", "myshop@4417"]


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

    @pytest.mark.parametrize("cached_target", [False, True])
    def test_an_env_pin_on_the_old_slot_is_reported_without_its_name(
        self, cached_target: bool
    ) -> None:
        _cache_stale()
        if cached_target:
            cache_session(cacheable_browser_session(), "myshop")

        result = _login(_plugin(PIN_FIELDS), GRAFTPUNK_SESSION="myshop@4417")

        output = strip_ansi(result.output)
        assert result.exit_code == 0, output
        assert "GRAFTPUNK_SESSION names the old session; set it to myshop." in output
        assert "4417" not in output

    def test_the_pin_hint_masks_the_old_name_when_finishing_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from graftpunk import session_migration

        _cache_stale()
        cache_session(cacheable_browser_session(), "myshop")

        def boom(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("storage went away")

        monkeypatch.setattr(session_migration, "clear_session_cache", boom)
        result = _login(_plugin(PIN_FIELDS), GRAFTPUNK_SESSION="myshop@4417")

        output = strip_ansi(result.output)
        assert result.exit_code == 0, output
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

    def test_a_colliding_run_stays_and_the_rest_still_move(self, _isolated: Path) -> None:
        for base in ("myshop-4417", "myshop"):
            run = _isolated / base / "20261001-000000-aaaa"
            run.mkdir(parents=True)
            (run / "network.har").write_text(base)
        later = _isolated / "myshop-4417" / "20261002-000000-bbbb"
        later.mkdir()
        (later / "network.har").write_text("later")
        _cache_stale()

        result = _login(_plugin(PIN_FIELDS))

        assert "legacy_observe_run_not_moved" not in strip_ansi(result.output)
        assert (_isolated / "myshop" / "20261001-000000-aaaa" / "network.har").read_text() == (
            "myshop"
        )
        assert (_isolated / "myshop-4417" / "20261001-000000-aaaa").is_dir()
        assert (_isolated / "myshop" / "20261002-000000-bbbb" / "network.har").read_text() == (
            "later"
        )


class TestLogsAtTheDefaultLevel:
    """The storage layer logs slot names on failures; under the migration they are masked."""

    def _assert_masked(self, output: str) -> None:
        assert "4417" not in output
        assert "myshop@<earlier label>" in output

    def test_a_corrupt_pickle(self) -> None:
        _cache_stale()
        from graftpunk.config import get_settings

        (Path(get_settings().sessions_dir) / "myshop@4417" / "session.pickle").write_bytes(b"x")

        result = _login(_plugin(PIN_FIELDS, succeed=False))

        self._assert_masked(strip_ansi(result.output))

    def test_a_corrupt_metadata_file(self) -> None:
        _cache_stale()
        _metadata_path("myshop@4417").write_text("{not json")

        result = _login(_plugin(PIN_FIELDS, succeed=False))

        output = strip_ansi(result.output)
        assert "4417" not in output

    def test_a_failed_delete(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import shutil

        _cache_stale()
        cache_session(cacheable_browser_session(), "myshop")
        real_rmtree = shutil.rmtree

        def refusing_rmtree(path, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
            if "4417" in str(path):
                raise OSError(f"permission denied: {path}")
            return real_rmtree(path, *args, **kwargs)

        monkeypatch.setattr("graftpunk.storage.local.shutil.rmtree", refusing_rmtree)
        result = _login(_plugin(PIN_FIELDS))

        self._assert_masked(strip_ansi(result.output))

    def test_the_redaction_ends_with_the_migration(self) -> None:
        from structlog.testing import capture_logs

        from graftpunk.logging import get_logger
        from graftpunk.session_migration import hidden_name

        log = get_logger("probe")
        with capture_logs() as logs:
            with hidden_name("myshop@4417"):
                log.warning("inside", name="myshop@4417", path="/x/myshop-4417/y")
            log.warning("outside", name="myshop@4417")
        assert logs[0]["name"] == "myshop@<earlier label>"
        assert logs[0]["path"] == "/x/myshop-<earlier label>/y"
        assert logs[1]["name"] == "myshop@4417"


class TestElsewhere:
    def test_gp_http_masks_a_code_slot(self) -> None:
        from graftpunk.plugins.cli_plugin import SitePlugin

        class OtherBase(SitePlugin):
            site_name = "fmtsite"
            base_url = "https://fmt.example.com"
            session_name = "shopbase"
            login_config = _plugin(CODE_FIELDS).login_config

        _cache_legacy("shopbase@829341", "829341")
        cache_session(cacheable_browser_session("bob"), "shopbase@bob")
        from unittest.mock import patch

        import typer
        from typer.testing import CliRunner

        from graftpunk.cli.http_commands import http_app
        from graftpunk.cli.plugin_commands import register_plugin_commands

        app = typer.Typer()
        with patch(
            "graftpunk.cli.plugin_commands.discover_all_plugins", return_value=(OtherBase(),)
        ):
            register_plugin_commands(app, notify_errors=False)
        app.add_typer(http_app, name="http")
        result = CliRunner().invoke(
            app,
            ["http", "get", "--session", "shopbase", "https://fmt.example.com/x"],
            env={"COLUMNS": "200"},
        )

        output = strip_ansi(result.output)
        assert result.exit_code != 0
        assert "829341" not in output
        assert "shopbase@<earlier label>" in output

    def test_an_unreadable_code_slot_is_masked_and_its_read_logged_masked(self) -> None:
        _cache_legacy("myshop@829341", "829341")
        cache_session(cacheable_browser_session("bob"), "myshop@bob")
        _metadata_path("myshop@829341").write_text("{not json")

        result = invoke_plugin_app(_plugin(CODE_FIELDS), ["fmtsite", "items"])

        output = strip_ansi(result.output)
        assert result.exit_code != 0
        assert "829341" not in output
        assert "myshop@<earlier label>" in output
        assert "myshop@bob" in output

    def test_another_plugins_slots_are_not_counted(self) -> None:
        _cache_legacy("othershop@829341", "829341")

        result = invoke_plugin_app(_plugin(CODE_FIELDS), ["fmtsite", "login"], **CODE_CREDS)

        assert result.exit_code == 0, result.output
        assert "Another session" not in strip_ansi(result.output)

    def test_a_login_the_migration_cannot_affect_lists_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from graftpunk.cli import login_commands

        calls = []
        real = login_commands.list_sessions
        monkeypatch.setattr(
            login_commands, "list_sessions", lambda *a, **k: calls.append(1) or real(*a, **k)
        )
        result = invoke_plugin_app(
            _plugin({"username": "#u", "password": "#p"}),
            ["fmtsite", "login"],
            **{"FMTSITE_USERNAME": "alice", "FMTSITE_PASSWORD": "pw-x"},
        )

        assert result.exit_code == 0, result.output
        assert calls == []


class _DictPickled:
    """A session class that pickles its whole ``__dict__``, slot stamp included."""

    def __init__(self) -> None:
        import requests

        self.cookies = requests.cookies.RequestsCookieJar()
        self.headers: dict[str, str] = {}


class TestCopyStamps:
    def test_a_dict_pickled_session_carries_the_new_slot_name(self) -> None:
        from graftpunk.cache import copy_session
        from graftpunk.session_identity import GP_SESSION_NAME_ATTR

        cache_session(_DictPickled(), "myshop@4417", account_rule=None)
        copy_session("myshop@4417", "myshop", account_identifier=None)

        import dill

        from graftpunk import cache as cache_mod
        from graftpunk.encryption import decrypt_data

        data, _meta = cache_mod._get_session_storage_backend().load_session("myshop")
        restored = dill.loads(decrypt_data(data))  # noqa: S301 (written by this test)
        assert restored.__dict__[GP_SESSION_NAME_ATTR] == "myshop"


class TestRedaction:
    def test_it_composes_with_log_secrets(self) -> None:
        """A secret option's scrub (#289) and the old-name placeholder apply together."""
        from structlog.testing import capture_logs

        from graftpunk.logging import get_logger, log_secrets
        from graftpunk.session_migration import hidden_name

        log = get_logger("probe")
        with capture_logs() as logs, log_secrets(["tok-secret-99"]), hidden_name("myshop@4417"):
            log.bind(slot="myshop@4417").warning(
                "x", name="myshop@4417", option="tok-secret-99", both="myshop@4417 tok-secret-99"
            )

        line = logs[0]
        assert line["slot"] == "myshop@<earlier label>"
        assert line["name"] == "myshop@<earlier label>"
        assert "tok-secret-99" not in repr(line)
        assert "4417" not in repr(line)

    def _capture(self, emit):  # noqa: ANN001, ANN202
        from structlog.testing import capture_logs

        from graftpunk.logging import get_logger
        from graftpunk.session_migration import hidden_name

        log = get_logger("probe")
        with capture_logs() as logs, hidden_name("myshop@4417"):
            emit(log)
        return logs

    def test_bound_context(self) -> None:
        logs = self._capture(lambda log: log.bind(name="myshop@4417").warning("x"))
        assert logs[0]["name"] == "myshop@<earlier label>"

    def test_positional_arguments(self) -> None:
        from structlog.testing import capture_logs

        from graftpunk.logging import _redacting_call, redacted_values

        seen = []
        with capture_logs(), redacted_values(("myshop@4417", "P")):
            _redacting_call(lambda *a, **k: seen.append(a))("event %s", "at myshop@4417")
        assert seen == [("event %s", "at P")]

    def test_lists_tuples_and_dict_keys(self) -> None:
        logs = self._capture(
            lambda log: log.warning(
                "x",
                names=["myshop@4417"],
                pair=("a", "myshop@4417"),
                by_name={"myshop@4417": 1},
            )
        )
        assert logs[0]["names"] == ["myshop@<earlier label>"]
        assert logs[0]["pair"] == ("a", "myshop@<earlier label>")
        assert logs[0]["by_name"] == {"myshop@<earlier label>": 1}


class TestReplaceLegacyRecordDirectly:
    """``replace_legacy_record`` checks its own conditions, whoever calls it."""

    def test_no_secret_field_means_no_change(self) -> None:
        from graftpunk.session_migration import replace_legacy_record

        _cache_legacy("myshop@work", "alice")

        assert not replace_legacy_record("myshop@work", "bob", secret_field=None)
        assert get_session_metadata("myshop@work")["account_identifier"] == "alice"

    def test_a_current_record_is_left(self) -> None:
        from graftpunk.session_migration import replace_legacy_record

        cache_session(cacheable_browser_session("carol"), "myshop@work")

        assert not replace_legacy_record("myshop@work", "bob", secret_field=CODE_FIELD)
        assert get_session_metadata("myshop@work")["account_identifier"] == "carol"

    def test_a_slot_named_after_its_secret_keeps_its_record(self) -> None:
        from graftpunk.session_migration import replace_legacy_record

        _cache_legacy("myshop@829341", "829341")

        assert not replace_legacy_record("myshop@829341", "bob", secret_field=CODE_FIELD)
        assert get_session_metadata("myshop@829341")["account_identifier"] == "829341"

    def test_a_1_24_record_is_replaced(self) -> None:
        from graftpunk.session_migration import replace_legacy_record

        _cache_legacy("myshop@work", "829341")

        assert replace_legacy_record("myshop@work", "bob", secret_field=CODE_FIELD)
        stored = get_session_metadata("myshop@work")
        assert (stored["account_identifier"], stored["account_rule"]) == ("bob", 2)


class TestLeftoverReads:
    def test_a_leftover_whose_metadata_fails_to_read_is_logged_masked(self) -> None:
        _cache_legacy("myshop@829341", "829341")
        _metadata_path("myshop@829341").write_text("{not json")

        result = invoke_plugin_app(_plugin(CODE_FIELDS), ["fmtsite", "login"], **CODE_CREDS)

        output = strip_ansi(result.output)
        assert result.exit_code == 0, output
        assert "829341" not in output
        assert "myshop@<earlier label>" in output


class TestRound3:
    def test_a_failed_metadata_write_is_reported_not_claimed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from structlog.testing import capture_logs

        from graftpunk import cache as cache_mod

        _cache_legacy("myshop@work", "829341")
        backend = cache_mod._get_session_storage_backend()
        monkeypatch.setattr(backend, "update_session_metadata", lambda *a, **k: False)

        with capture_logs() as logs:
            result = invoke_plugin_app(
                _plugin(CODE_FIELDS, write=False),
                ["fmtsite", "login", "--as", "work"],
                **CODE_CREDS,
            )

        output = strip_ansi(result.output)
        assert result.exit_code == 0, output
        assert "Could not replace the account recorded on 'myshop@work'" in output
        assert "Replaced the account" not in output
        assert "829341" not in output
        events = [entry["event"] for entry in logs]
        assert "legacy_account_record_replaced" not in events
        assert "legacy_account_record_not_replaced" in events
        assert "829341" not in repr(logs)
        assert get_session_metadata("myshop@work")["account_identifier"] == "829341"

    def test_a_library_refresh_after_a_metadata_only_replacement_keeps_it(self) -> None:
        _cache_legacy("myshop@work", "829341")
        login = invoke_plugin_app(
            _plugin(CODE_FIELDS, write=False), ["fmtsite", "login", "--as", "work"], **CODE_CREDS
        )
        assert login.exit_code == 0, login.output

        loaded = load_session("myshop@work")
        assert getattr(loaded, GP_ACCOUNT_ATTR) == "bob"
        meta = get_session_metadata("myshop@work")
        cache_session(loaded, "myshop@work", account_rule=meta["account_rule"])

        stored = get_session_metadata("myshop@work")
        assert (stored["account_identifier"], stored["account_rule"]) == ("bob", 2)
        assert getattr(load_session("myshop@work"), GP_ACCOUNT_ATTR) == "bob"

    def test_a_slot_named_after_its_record_is_not_named_in_a_warning(self) -> None:
        _cache_legacy("myshop@829341", "829341")

        result = invoke_plugin_app(
            _plugin(CODE_FIELDS, write=False),
            ["fmtsite", "login", "--as", "829341"],
            **CODE_CREDS,
        )

        output = strip_ansi(result.output)
        assert result.exit_code == 0, output
        assert "was recorded for" not in output
        assert "Could not replace" not in output
        assert "Replaced the account" not in output

    def test_a_failed_check_withholds_the_warning(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from graftpunk.cli import login_commands

        _cache_legacy("myshop@work", "829341")

        def broken(_plugin_obj: object) -> None:
            raise RuntimeError("field lookup failed")

        monkeypatch.setattr(login_commands, "legacy_secret_field_of", broken)
        result = invoke_plugin_app(
            _plugin(CODE_FIELDS, write=False), ["fmtsite", "login", "--as", "work"], **CODE_CREDS
        )

        output = strip_ansi(result.output)
        assert result.exit_code == 0, output
        assert "829341" not in output
        assert "was recorded for" not in output

    def test_the_source_field_is_the_one_the_old_rule_picked(self) -> None:
        """An identifier field holding the same value must not stop the migration."""
        fields = {"account": "#a", "loginPin": "#p", "user_id": "#u"}
        creds = {"FMTSITE_ACCOUNT": "4417", "FMTSITE_LOGINPIN": "4417", "FMTSITE_USER_ID": "bob"}
        _cache_stale()

        result = invoke_plugin_app(_plugin(fields), ["fmtsite", "login"], **creds)

        output = strip_ansi(result.output)
        assert result.exit_code == 0, output
        assert list_sessions() == ["myshop@bob"]
        assert "loginPin field" in output


def _raw_pickled_account(name: str) -> object:
    """The account a slot's pickle holds, read without gp's loader."""
    import dill

    from graftpunk import cache as cache_mod
    from graftpunk.encryption import decrypt_data

    data, _meta = cache_mod._get_session_storage_backend().load_session(name)
    restored = dill.loads(decrypt_data(data))  # noqa: S301 (written by this test)
    return getattr(restored, GP_ACCOUNT_ATTR, None)


class TestThePickleLosesTheSecret:
    def test_a_login_that_writes_the_slot_pickles_no_secret(self) -> None:
        _cache_legacy("myshop@work", "829341")
        assert _raw_pickled_account("myshop@work") == "829341"

        result = invoke_plugin_app(
            _plugin({"loginCode": "#c", "password": "#p"}),
            ["fmtsite", "login", "--as", "work"],
            **{"FMTSITE_LOGINCODE": "111222", "FMTSITE_PASSWORD": "pw-x"},
        )

        assert result.exit_code == 0, result.output
        assert _raw_pickled_account("myshop@work") is None

    def test_a_refresh_after_a_metadata_only_replacement_rewrites_the_pickle(self) -> None:
        from graftpunk.cache import update_session_cookies

        _cache_legacy("myshop@work", "829341")
        login = invoke_plugin_app(
            _plugin(CODE_FIELDS, write=False), ["fmtsite", "login", "--as", "work"], **CODE_CREDS
        )
        assert login.exit_code == 0, login.output
        assert _raw_pickled_account("myshop@work") == "829341"

        update_session_cookies(load_session("myshop@work"), "myshop@work")

        assert _raw_pickled_account("myshop@work") == "bob"
