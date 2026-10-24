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


class TestAsLabel:
    def test_a_1_24_as_slot_recording_the_secret_is_cleared_before_the_login(self) -> None:
        _cache_legacy("myshop@work", "4417")

        result = invoke_plugin_app(
            _plugin(PIN_FIELDS, succeed=False), ["fmtsite", "login", "--as", "work"], **PIN_CREDS
        )

        output = strip_ansi(result.output)
        assert result.exit_code != 0
        assert get_session_metadata("myshop@work")["account_identifier"] is None
        assert getattr(load_session("myshop@work"), GP_ACCOUNT_ATTR) is None
        assert "Cleared the account recorded on 'myshop@work'" in output
        assert "4417" not in output

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
        _cache_legacy("myshop@alice-x-com", "alice@x.com")

        result = invoke_plugin_app(
            _plugin({"user_name": "#u", "email": "#e", "password": "#p"}),
            ["fmtsite", "login"],
            **{
                "FMTSITE_USER_NAME": "alice",
                "FMTSITE_EMAIL": "alice@x.com",
                "FMTSITE_PASSWORD": "pw-x",
            },
        )

        assert result.exit_code == 0, result.output
        assert list_sessions() == ["myshop@alice-x-com"]

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
