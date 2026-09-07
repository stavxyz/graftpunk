"""Login derives, scopes, funnels and warns about account identity (#151, #174)."""

from __future__ import annotations

from unittest.mock import patch

import pytest
import requests

from graftpunk.session_identity import GP_ACCOUNT_ATTR
from tests.unit.cli_harness import echo_loaded_session
from tests.unit.cli_harness import invoke_plugin_app as _invoke


class TestFunnel:
    def test_explicit_arguments_win(self) -> None:
        """Engine-controlled paths pass name and identifier explicitly."""
        from graftpunk.plugins.cli_plugin import SitePlugin, cache_login_session

        class P(SitePlugin):
            site_name = "fmtsite"
            base_url = "https://fmt.example.com"
            session_name = "myshop"

        plugin = P()
        session = requests.Session()
        with patch("graftpunk.plugins.cli_plugin.cache_session") as mock_cache:
            used = cache_login_session(
                plugin, session, name="myshop@alice", identifier="alice@example.com"
            )
        assert used == "myshop@alice"
        assert getattr(session, GP_ACCOUNT_ATTR) == "alice@example.com"
        assert mock_cache.call_args[0] == (session, "myshop@alice")

    def test_scope_fallback_for_author_facing_paths(self, fresh_backend) -> None:  # noqa: ANN001
        """browser_session callers pass nothing; the operating scope is the input."""
        from graftpunk.cache import get_session_metadata, list_sessions
        from graftpunk.plugins.cli_plugin import SitePlugin, cache_login_session
        from graftpunk.session_scope import operating_session

        class P(SitePlugin):
            site_name = "fmtsite"
            base_url = "https://fmt.example.com"
            session_name = "myshop"

        plugin = P()
        session = requests.Session()
        with operating_session("myshop@alice", "alice@example.com"):
            used = cache_login_session(plugin, session)

        assert used == "myshop@alice"
        assert getattr(session, GP_ACCOUNT_ATTR) == "alice@example.com"
        assert list_sessions() == ["myshop@alice"]
        assert get_session_metadata("myshop@alice")["account_identifier"] == "alice@example.com"

    def test_a_scope_for_another_base_does_not_steer_the_funnel(self, fresh_backend) -> None:  # noqa: ANN001
        """The base-scoped guard: another plugin's login must not rename this cache write."""
        from graftpunk.cache import list_sessions
        from graftpunk.plugins.cli_plugin import SitePlugin, cache_login_session
        from graftpunk.session_scope import operating_session

        class P(SitePlugin):
            site_name = "fmtsite"
            base_url = "https://fmt.example.com"
            session_name = "myshop"

        plugin = P()
        with operating_session("othersite@bob", "bob@example.com"):
            used = cache_login_session(plugin, requests.Session())

        assert used == "myshop"
        assert list_sessions() == ["myshop"]

    def test_an_explicit_name_does_not_borrow_the_scope_identity(self, fresh_backend) -> None:  # noqa: ANN001
        """A caller naming the slot is naming the account too, or recording none.

        Taking the name from the argument and the identifier from the scope
        would stamp whoever the login command was about onto a session that
        caller never named. The named slot is empty here, so the carry-forward
        has nothing to offer either and no account is recorded.
        """
        from graftpunk.cache import get_session_metadata, list_sessions
        from graftpunk.plugins.cli_plugin import SitePlugin, cache_login_session
        from graftpunk.session_scope import operating_session

        class P(SitePlugin):
            site_name = "fmtsite"
            base_url = "https://fmt.example.com"
            session_name = "myshop"

        plugin = P()
        session = requests.Session()
        with operating_session("myshop@alice", "alice@example.com"):
            used = cache_login_session(plugin, session, name="myshop@bob")

        assert used == "myshop@bob"
        assert not hasattr(session, GP_ACCOUNT_ATTR)
        assert list_sessions() == ["myshop@bob"]
        assert get_session_metadata("myshop@bob")["account_identifier"] is None

    def test_an_explicit_identifier_wins_with_the_scopes_slot(self, fresh_backend) -> None:  # noqa: ANN001
        """Leaving *name* out but naming *identifier* mixes tiers deliberately.

        The slot still comes from the scope (name was left out), but the
        account is the caller's explicit choice, not the scope's. Each field
        is decided on its own rather than as a package deal.
        """
        from graftpunk.cache import get_session_metadata, list_sessions
        from graftpunk.plugins.cli_plugin import SitePlugin, cache_login_session
        from graftpunk.session_scope import operating_session

        class P(SitePlugin):
            site_name = "fmtsite"
            base_url = "https://fmt.example.com"
            session_name = "myshop"

        plugin = P()
        session = requests.Session()
        with operating_session("myshop@alice", "alice@example.com"):
            used = cache_login_session(plugin, session, identifier="bob@example.com")

        assert used == "myshop@alice"
        assert getattr(session, GP_ACCOUNT_ATTR) == "bob@example.com"
        assert list_sessions() == ["myshop@alice"]
        assert get_session_metadata("myshop@alice")["account_identifier"] == "bob@example.com"

    def test_a_command_scope_write_keeps_the_slots_recorded_account(self, fresh_backend) -> None:  # noqa: ANN001
        """A command scope carries no identifier; the write must not erase the account.

        Only a login sets the scope's identifier. Any other dispatch leaves it
        None, so without the carry-forward a refresh write under a command
        scope would record ``account_identifier=None`` over the account the
        slot was cached for.
        """
        from graftpunk.cache import cache_session, get_session_metadata
        from graftpunk.plugins.cli_plugin import SitePlugin, cache_login_session
        from graftpunk.session_scope import operating_session
        from tests.unit.cli_harness import cacheable_browser_session

        class P(SitePlugin):
            site_name = "fmtsite"
            base_url = "https://fmt.example.com"
            session_name = "myshop"

        cache_session(cacheable_browser_session("alice@example.com"), "myshop@alice")

        with operating_session("myshop@alice"):
            used = cache_login_session(P(), cacheable_browser_session())

        assert used == "myshop@alice"
        assert get_session_metadata("myshop@alice")["account_identifier"] == "alice@example.com"

    def test_a_failed_carry_forward_read_still_caches_the_session(self, fresh_backend) -> None:  # noqa: ANN001
        """The read is advisory: a storage failure costs the account, not the write."""
        from structlog.testing import capture_logs

        from graftpunk.cache import get_session_metadata, list_sessions
        from graftpunk.exceptions import StorageError
        from graftpunk.plugins.cli_plugin import SitePlugin, cache_login_session
        from graftpunk.session_scope import operating_session
        from tests.unit.cli_harness import cacheable_browser_session

        class P(SitePlugin):
            site_name = "fmtsite"
            base_url = "https://fmt.example.com"
            session_name = "myshop"

        with (
            capture_logs() as logs,
            patch(
                "graftpunk.plugins.cli_plugin.get_session_metadata",
                side_effect=StorageError("the backend is unreachable"),
            ),
            operating_session("myshop@alice"),
        ):
            used = cache_login_session(P(), cacheable_browser_session())

        assert used == "myshop@alice"
        assert list_sessions() == ["myshop@alice"]
        assert get_session_metadata("myshop@alice")["account_identifier"] is None
        assert "login_identity_carry_forward_failed" in [e["event"] for e in logs]


class TestBoundaryWarning:
    """The pure compare/emit half consumes get_session_metadata's REAL shape: dict | None."""

    def test_warns_only_when_both_present_and_unequal(self) -> None:
        from graftpunk.cli.login_commands import _warn_if_slot_changes_hands_post

        stored = {"name": "myshop", "account_identifier": "bob@example.com"}
        with patch("graftpunk.cli.login_commands.gp_console") as mock_console:
            _warn_if_slot_changes_hands_post(stored, "alice@example.com", "myshop")
        assert mock_console.warn.called

    @pytest.mark.parametrize(
        ("stored", "incoming"),
        [
            ({"name": "myshop"}, "alice@example.com"),  # key absent (legacy metadata)
            ({"name": "myshop", "account_identifier": None}, "alice@example.com"),
            ({"name": "myshop", "account_identifier": "bob@example.com"}, None),
            (None, "alice@example.com"),  # no stored session at all
            ({"name": "myshop", "account_identifier": "a"}, "a"),  # same account
        ],
    )
    def test_silent_otherwise(self, stored, incoming) -> None:  # noqa: ANN001
        from graftpunk.cli.login_commands import _warn_if_slot_changes_hands_post

        with patch("graftpunk.cli.login_commands.gp_console") as mock_console:
            _warn_if_slot_changes_hands_post(stored, incoming, "myshop")
        assert not mock_console.warn.called

    def test_no_metadata_read_on_refresh_writes(self, fresh_backend, monkeypatch) -> None:  # noqa: ANN001
        """cache_session/update_session_cookies never fetch stored metadata (spec)."""
        import requests

        from graftpunk import cache as cache_mod
        from graftpunk.cache import cache_session, update_session_cookies

        cache_session(requests.Session(), "myshop@alice")
        backend = cache_mod._get_session_storage_backend()
        calls = {"n": 0}
        real = backend.get_session_metadata

        def counting(name):  # noqa: ANN001, ANN202
            calls["n"] += 1
            return real(name)

        monkeypatch.setattr(backend, "get_session_metadata", counting)
        update_session_cookies(requests.Session(), "myshop@alice")
        assert calls["n"] == 0, "a refresh write fetched stored metadata"


class TestEngineChainThreading:
    """The generated flows get the operating identity as EXPLICIT arguments."""

    def test_make_login_body_hands_both_values_to_a_generated_login(self, monkeypatch) -> None:  # noqa: ANN001
        from types import SimpleNamespace

        from graftpunk.cli.login_commands import make_login_body

        monkeypatch.setenv("ACME_USERNAME", "alice@example.com")
        monkeypatch.setenv("ACME_PASSWORD", "x")
        seen: dict = {}

        def login(  # a generated login's signature
            credentials: dict[str, str],
            *,
            headless: bool | None = None,
            observe_mode: str = "off",
            session_name: str | None = None,
            account_identifier: str | None = None,
        ) -> bool:
            seen.update(session_name=session_name, account_identifier=account_identifier)
            return True

        plugin = SimpleNamespace(site_name="acme", session_name="myshop")
        body = make_login_body(plugin, login, {"username": "#u", "password": "#p"})
        body(SimpleNamespace())

        assert seen == {
            "session_name": "myshop@alice-example-com",
            "account_identifier": "alice@example.com",
        }

    @pytest.mark.asyncio
    async def test_nodriver_flow_caches_under_the_threaded_name(self, _fast_login_timings) -> None:  # noqa: ANN001
        from unittest.mock import AsyncMock, MagicMock

        from graftpunk.plugins.cli_plugin import LoginConfig, LoginStep, SitePlugin
        from graftpunk.plugins.login_engine import generate_login_method

        class NodriverPlugin(SitePlugin):
            site_name = "fmtsite"
            session_name = "myshop"
            base_url = "https://fmt.example.com"
            backend = "nodriver"
            login_config = LoginConfig(
                steps=[LoginStep(fields={"username": "#u"}, submit="#go")],
                url="/login",
            )

        login = generate_login_method(NodriverPlugin())

        mock_tab = MagicMock()
        mock_tab.select = AsyncMock(return_value=AsyncMock())
        mock_tab.get_content = AsyncMock(return_value="<html>Welcome</html>")
        mock_tab.send = AsyncMock()

        mock_bs = MagicMock()
        instance = mock_bs.return_value
        instance.__aenter__ = AsyncMock(return_value=instance)
        instance.__aexit__ = AsyncMock(return_value=False)
        instance.driver = MagicMock()
        instance.driver.get = AsyncMock(return_value=mock_tab)
        instance.transfer_nodriver_cookies_to_session = AsyncMock()

        with (
            patch("graftpunk.BrowserSession", mock_bs),
            patch("graftpunk.plugins.cli_plugin.cache_session") as mock_cache,
        ):
            result = await login(
                {"username": "alice"},
                session_name="myshop@alice",
                account_identifier="alice",
            )

        assert result is True
        assert mock_cache.call_args[0][1] == "myshop@alice"
        assert getattr(mock_cache.call_args[0][0], GP_ACCOUNT_ATTR) == "alice"

    def test_selenium_flow_caches_under_the_threaded_name(self) -> None:
        from unittest.mock import MagicMock

        from graftpunk.plugins.cli_plugin import LoginConfig, LoginStep, SitePlugin
        from graftpunk.plugins.login_engine import generate_login_method

        class QuotesPlugin(SitePlugin):
            site_name = "fmtsite"
            session_name = "myshop"
            base_url = "https://fmt.example.com"
            backend = "selenium"
            login_config = LoginConfig(
                steps=[LoginStep(fields={"username": "#u"}, submit="#go")],
                url="/login",
                success="#ok",
            )

        login = generate_login_method(QuotesPlugin())

        mock_bs = MagicMock()
        instance = mock_bs.return_value
        instance.__enter__ = MagicMock(return_value=instance)
        instance.__exit__ = MagicMock(return_value=False)
        instance.driver = MagicMock()

        with (
            patch("graftpunk.BrowserSession", mock_bs),
            patch("graftpunk.plugins.login_engine.time"),
            patch("graftpunk.plugins.cli_plugin.cache_session") as mock_cache,
        ):
            result = login(
                {"username": "alice"},
                session_name="myshop@alice",
                account_identifier="alice",
            )

        assert result is True
        assert mock_cache.call_args[0][1] == "myshop@alice"
        assert getattr(mock_cache.call_args[0][0], GP_ACCOUNT_ATTR) == "alice"


def _hand_written_login_plugin(seen: dict | None = None):  # noqa: ANN001, ANN202
    """A plugin whose ``login(credentials)`` records, caches through the funnel, succeeds."""
    from graftpunk.plugins.cli_plugin import SitePlugin, cache_login_session

    class ShopPlugin(SitePlugin):
        site_name = "fmtsite"
        base_url = "https://fmt.example.com"
        session_name = "myshop"

        def login(self, credentials: dict[str, str]) -> bool:
            """Sign in to fmtsite."""
            if seen is not None:
                seen["credentials"] = dict(credentials)
                # The class attribute, always: nothing mutates the instance
                # for the length of a login flow any more (#174).
                seen["session_name"] = self.session_name
            cache_login_session(self, requests.Session())
            return True

    return ShopPlugin()


def _by_hand_login_plugin():  # noqa: ANN202
    """A login that caches under ``self.session_name`` by hand: the migration case.

    With the stamp gone this writes the bare ``myshop`` while the CLI targeted
    ``myshop@bob``, which is exactly the silent divergence the post-login
    advisory has to catch.
    """
    from graftpunk.cache import cache_session
    from graftpunk.plugins.cli_plugin import SitePlugin

    class ShopPlugin(SitePlugin):
        site_name = "fmtsite"
        base_url = "https://fmt.example.com"
        session_name = "myshop"

        def login(self, credentials: dict[str, str]) -> bool:
            """Sign in to fmtsite."""
            cache_session(requests.Session(), self.session_name)
            return True

    return ShopPlugin()


def _no_write_login_plugin():  # noqa: ANN202
    """A login that succeeds without caching anything: the advisory's other case."""
    from graftpunk.plugins.cli_plugin import SitePlugin

    class ShopPlugin(SitePlugin):
        site_name = "fmtsite"
        base_url = "https://fmt.example.com"
        session_name = "myshop"

        def login(self, credentials: dict[str, str]) -> bool:
            """Sign in to fmtsite."""
            return True

    return ShopPlugin()


def _scope_reading_login_plugin(seen: dict):  # noqa: ANN001, ANN202
    """A hand-written login that loads the session the scope names, then caches it."""
    from graftpunk.plugins.cli_plugin import SitePlugin, cache_login_session

    class ShopPlugin(SitePlugin):
        site_name = "fmtsite"
        base_url = "https://fmt.example.com"
        session_name = "myshop"

        def login(self, credentials: dict[str, str]) -> bool:
            """Sign in to fmtsite."""
            loaded = self.get_session()
            seen["account"] = getattr(loaded, GP_ACCOUNT_ATTR, None)
            cache_login_session(self, loaded)
            return True

    return ShopPlugin()


def _raising_login_plugin():  # noqa: ANN202
    """A plugin whose ``login(credentials)`` raises."""
    from graftpunk.plugins.cli_plugin import SitePlugin

    class ShopPlugin(SitePlugin):
        site_name = "fmtsite"
        base_url = "https://fmt.example.com"
        session_name = "myshop"

        def login(self, credentials: dict[str, str]) -> bool:
            """Sign in to fmtsite."""
            raise RuntimeError("the site was down")

    return ShopPlugin()


def _browser_login_plugin():  # noqa: ANN202
    """A hand-written login that caches through ``browser_session_sync()``."""
    from graftpunk.plugins.cli_plugin import SitePlugin

    class ShopPlugin(SitePlugin):
        site_name = "fmtsite"
        base_url = "https://fmt.example.com"
        session_name = "myshop"
        backend = "selenium"

        def login(self, credentials: dict[str, str]) -> bool:
            """Sign in to fmtsite."""
            with self.browser_session_sync() as (session, driver):
                driver.get(f"{self.base_url}/login")
            return True

    return ShopPlugin()


def _command_login_plugin():  # noqa: ANN202
    """A plugin whose ``login`` is @command-decorated: no generated login command."""
    from graftpunk.plugins.cli_plugin import SitePlugin, command

    class ShopPlugin(SitePlugin):
        site_name = "fmtsite"
        base_url = "https://fmt.example.com"
        session_name = "myshop"
        requires_session = True
        saves_session = True

        @command(help="Log in by hand", saves_session=True)
        def login(self, ctx):  # noqa: ANN001, ANN202
            return {"ok": True}

    return ShopPlugin()


_CREDS = {"FMTSITE_USERNAME": "alice", "FMTSITE_PASSWORD": "x"}


class TestLoginWrapperOrchestration:
    def test_derived_label_names_the_cached_session(self, fresh_backend) -> None:  # noqa: ANN001
        from graftpunk.cache import get_session_metadata, list_sessions

        seen: dict = {}
        result = _invoke(_hand_written_login_plugin(seen), ["fmtsite", "login"], **_CREDS)

        assert result.exit_code == 0, result.output
        assert "Session name: myshop@alice" in result.output
        # The bare base name, inside the plugin's own login(): the stamp is gone.
        assert seen["session_name"] == "myshop"
        assert seen["credentials"] == {"username": "alice", "password": "x"}
        # What the scope carried is what the cache received.
        assert list_sessions() == ["myshop@alice"]
        assert get_session_metadata("myshop@alice")["account_identifier"] == "alice"
        # The happy path says nothing extra.
        assert "Nothing was written to" not in result.output

    def test_as_flag_overrides_the_label(self, fresh_backend) -> None:  # noqa: ANN001
        from graftpunk.cache import list_sessions

        seen: dict = {}
        result = _invoke(
            _hand_written_login_plugin(seen), ["fmtsite", "login", "--as", "work"], **_CREDS
        )

        assert result.exit_code == 0, result.output
        assert "Session name: myshop@work" in result.output
        assert seen["session_name"] == "myshop"
        assert list_sessions() == ["myshop@work"]

    def test_bad_as_label_names_the_label_the_user_typed(self) -> None:
        result = _invoke(
            _hand_written_login_plugin(), ["fmtsite", "login", "--as", "Work!"], **_CREDS
        )
        assert result.exit_code != 0
        assert "Work!" in result.output

    def test_slot_recorded_for_another_account_warns_after_success(self, fresh_backend) -> None:  # noqa: ANN001
        """End to end: the ONE fetch reads real metadata; the emit follows success."""
        from graftpunk.cache import cache_session

        stored = requests.Session()
        setattr(stored, GP_ACCOUNT_ATTR, "bob@example.com")
        cache_session(stored, "myshop@work")

        result = _invoke(
            _hand_written_login_plugin(), ["fmtsite", "login", "--as", "work"], **_CREDS
        )
        assert result.exit_code == 0, result.output
        assert "was recorded for bob@example.com" in result.output
        assert "replaces it as alice" in result.output

    def test_advisory_failure_never_fails_a_successful_login(self, fresh_backend) -> None:  # noqa: ANN001
        """The verdict is sealed before the advisory block: a raise there is a hint lost."""
        with patch(
            "graftpunk.cli.login_commands.get_active_session",
            side_effect=FileNotFoundError("cwd was removed"),
        ):
            result = _invoke(_hand_written_login_plugin(), ["fmtsite", "login"], **_CREDS)

        assert result.exit_code == 0, result.output
        assert "Logged in to fmtsite (session cached)" in result.output
        assert "Login failed" not in result.output

    def test_command_decorated_login_bypasses_derivation(self) -> None:
        plugin = _command_login_plugin()
        rejected = _invoke(plugin, ["fmtsite", "login", "--as", "work"], **_CREDS)
        assert rejected.exit_code != 0, rejected.output
        assert "No such option" in rejected.output

        with (
            patch("graftpunk.cli.plugin_runtime.update_session_cookies") as mock_update,
            patch(
                "graftpunk.cli.plugin_runtime.load_session_for_api_resolved",
                side_effect=echo_loaded_session(),
            ),
            patch("graftpunk.cli.plugin_runtime.list_sessions", return_value=[]),
        ):
            result = _invoke(plugin, ["fmtsite", "login"], **_CREDS)
        assert result.exit_code == 0, result.output
        assert mock_update.call_args[0][1] == "myshop"
        assert "Session name:" not in result.output

    def test_browser_session_sync_login_lands_on_the_scoped_slot(self) -> None:
        """A hand-written login caching through the helper reaches myshop@bob (#174)."""
        from unittest.mock import MagicMock

        with (
            patch("graftpunk.BrowserSession") as mock_bs,
            patch("graftpunk.plugins.cli_plugin.cache_session") as mock_cache,
        ):
            instance = mock_bs.return_value
            instance.driver = MagicMock()
            instance.transfer_driver_cookies_to_session = MagicMock()
            instance.quit = MagicMock()
            result = _invoke(
                _browser_login_plugin(),
                ["fmtsite", "login", "--as", "bob"],
                FMTSITE_USERNAME="bob@example.com",
                FMTSITE_PASSWORD="x",  # noqa: S106
            )

        assert result.exit_code == 0, result.output
        cached_session, cached_name = mock_cache.call_args[0]
        assert cached_name == "myshop@bob"
        assert getattr(cached_session, GP_ACCOUNT_ATTR) == "bob@example.com"

    def test_a_by_hand_cache_under_the_bare_name_warns_after_success(
        self,
        fresh_backend,  # noqa: ANN001
    ) -> None:
        """#174: the login succeeded, but nothing landed on the labelled slot."""
        from structlog.testing import capture_logs

        from graftpunk.cache import list_sessions

        with capture_logs() as logs:
            result = _invoke(
                _by_hand_login_plugin(),
                ["fmtsite", "login", "--as", "bob"],
                FMTSITE_USERNAME="bob@example.com",
                FMTSITE_PASSWORD="x",  # noqa: S106
            )

        # Advisory only: the login itself still succeeded.
        assert result.exit_code == 0, result.output
        assert "Logged in to fmtsite (session cached)" in result.output
        # It wrote the bare slot, not the one the CLI targeted.
        assert list_sessions() == ["myshop"]
        assert "Nothing was written to 'myshop@bob'" in result.output
        assert "cache_login_session" in result.output
        assert "login_cached_outside_target_slot" in [e["event"] for e in logs]

    def test_a_by_hand_write_warns_even_when_the_target_slot_already_exists(
        self,
        fresh_backend,  # noqa: ANN001
    ) -> None:
        """Existence is not the question; movement is.

        An earlier login left ``myshop@bob`` cached. This login targets the
        same slot, writes the bare base instead, and leaves that slot stale. A
        check that only asked whether the slot exists would say nothing, and
        the user would go on using a session this login never refreshed.
        """
        from graftpunk.cache import cache_session, get_session_metadata
        from tests.unit.cli_harness import cacheable_browser_session

        cache_session(cacheable_browser_session("bob@example.com"), "myshop@bob")
        before = get_session_metadata("myshop@bob")["modified_at"]

        result = _invoke(
            _by_hand_login_plugin(),
            ["fmtsite", "login", "--as", "bob"],
            FMTSITE_USERNAME="bob@example.com",
            FMTSITE_PASSWORD="x",  # noqa: S106
        )

        assert result.exit_code == 0, result.output
        assert "Nothing was written to 'myshop@bob'" in result.output
        # The pre-existing slot really was left untouched.
        assert get_session_metadata("myshop@bob")["modified_at"] == before

    def test_an_unlabelled_target_does_not_warn_when_the_funnel_wrote_it(
        self,
        fresh_backend,  # noqa: ANN001
    ) -> None:
        """No label means the bare base IS the target, so a by-hand write is correct."""
        from graftpunk.cache import list_sessions

        result = _invoke(
            _by_hand_login_plugin(),
            ["fmtsite", "login"],
            FMTSITE_USERNAME="!!!",
            FMTSITE_PASSWORD="x",  # noqa: S106
        )

        assert result.exit_code == 0, result.output
        assert "Session name: myshop" in result.output
        assert list_sessions() == ["myshop"]
        assert "Nothing was written to" not in result.output

    def test_an_unlabelled_target_does_not_warn_when_the_login_caches_by_hand(
        self,
        fresh_backend,  # noqa: ANN001
    ) -> None:
        """The funnel writes the bare base the CLI named, so there is nothing to report."""
        from graftpunk.cache import list_sessions

        result = _invoke(
            _hand_written_login_plugin(),
            ["fmtsite", "login"],
            FMTSITE_USERNAME="!!!",
            FMTSITE_PASSWORD="x",  # noqa: S106
        )

        assert result.exit_code == 0, result.output
        assert list_sessions() == ["myshop"]
        assert "Nothing was written to" not in result.output

    def test_an_unlabelled_target_warns_when_nothing_was_written(self, fresh_backend) -> None:  # noqa: ANN001
        """A login that caches nothing at all is reported on a bare target too."""
        from structlog.testing import capture_logs

        from graftpunk.cache import list_sessions

        with capture_logs() as logs:
            result = _invoke(
                _no_write_login_plugin(),
                ["fmtsite", "login"],
                FMTSITE_USERNAME="!!!",
                FMTSITE_PASSWORD="x",  # noqa: S106
            )

        assert result.exit_code == 0, result.output
        assert list_sessions() == []
        assert "Nothing was written to 'myshop'" in result.output
        assert "cache_login_session" in result.output
        assert "login_cached_outside_target_slot" in [e["event"] for e in logs]

    def test_a_label_less_login_can_read_the_cached_account(self, fresh_backend) -> None:  # noqa: ANN001
        """#174: a bare scope must resolve, or self.get_session() raises inside login().

        Credentials that derive no label make the CLI target the bare
        ``myshop`` and set a bare scope. Loading that exact-only missed the one
        cached account and raised ``SessionNotFoundError``.
        """
        from graftpunk.cache import cache_session
        from tests.unit.cli_harness import cacheable_browser_session

        cache_session(cacheable_browser_session("alice@example.com"), "myshop@alice")
        seen: dict = {}

        result = _invoke(
            _scope_reading_login_plugin(seen),
            ["fmtsite", "login"],
            FMTSITE_USERNAME="!!!",
            FMTSITE_PASSWORD="x",  # noqa: S106
        )

        assert result.exit_code == 0, result.output
        assert seen["account"] == "alice@example.com"

    def test_a_failed_login_leaves_no_scope_behind(self) -> None:
        from graftpunk.session_scope import current_operating_session

        result = _invoke(_raising_login_plugin(), ["fmtsite", "login"], **_CREDS)

        assert result.exit_code != 0
        assert "Login failed: the site was down" in result.output
        assert current_operating_session() is None
