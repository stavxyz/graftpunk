"""Login derives, stamps, funnels and warns about account identity (#151)."""

from __future__ import annotations

from unittest.mock import patch

import pytest
import requests

from graftpunk.session_identity import GP_ACCOUNT_ATTR
from tests.unit.cli_harness import invoke_plugin_app as _invoke


class TestStamp:
    def test_stamp_sets_and_restores(self) -> None:
        from graftpunk.cli.login_commands import _stamp_login_identity
        from graftpunk.plugins.cli_plugin import SitePlugin

        class P(SitePlugin):
            site_name = "fmtsite"
            base_url = "https://fmt.example.com"
            session_name = "myshop"

        plugin = P()
        with _stamp_login_identity(plugin, "alice", "alice@example.com"):
            assert plugin.session_name == "myshop@alice"
            assert getattr(plugin, GP_ACCOUNT_ATTR) == "alice@example.com"
        assert plugin.session_name == "myshop"  # class attr visible again
        assert not hasattr(plugin, GP_ACCOUNT_ATTR)

    def test_stamp_restores_when_the_login_raises(self) -> None:
        """The restore is in a finally: a failed login must not leak the stamp."""
        from graftpunk.cli.login_commands import _stamp_login_identity
        from graftpunk.plugins.cli_plugin import SitePlugin

        class P(SitePlugin):
            site_name = "fmtsite"
            base_url = "https://fmt.example.com"
            session_name = "myshop"

        plugin = P()
        with pytest.raises(RuntimeError), _stamp_login_identity(plugin, "alice", "a@example.com"):
            assert plugin.session_name == "myshop@alice"
            raise RuntimeError("login blew up")
        assert plugin.session_name == "myshop"
        assert not hasattr(plugin, GP_ACCOUNT_ATTR)

    def test_stamp_survives_a_read_only_session_name(self) -> None:
        """A protocol-literal plugin declares session_name as a read-only property.

        The stamp is a convenience for author-facing paths; it must degrade to
        a warning rather than break the login (the identifier still travels
        explicitly to the generated flows).
        """
        from graftpunk.cli.login_commands import _stamp_login_identity

        class ReadOnlyPlugin:
            site_name = "fmtsite"

            @property
            def session_name(self) -> str:
                return "myshop"

        plugin = ReadOnlyPlugin()
        with _stamp_login_identity(plugin, "alice", "a@example.com"):  # type: ignore[arg-type]
            assert plugin.session_name == "myshop"
            assert getattr(plugin, GP_ACCOUNT_ATTR) == "a@example.com"
        assert plugin.session_name == "myshop"

    def test_stamp_records_the_identifier_without_a_label(self) -> None:
        """No label (nothing identifier-shaped to slugify) still records who logged in."""
        from graftpunk.cli.login_commands import _stamp_login_identity
        from graftpunk.plugins.cli_plugin import SitePlugin

        class P(SitePlugin):
            site_name = "fmtsite"
            base_url = "https://fmt.example.com"
            session_name = "myshop"

        plugin = P()
        with _stamp_login_identity(plugin, None, "alice@example.com"):
            assert plugin.session_name == "myshop"
            assert getattr(plugin, GP_ACCOUNT_ATTR) == "alice@example.com"
        assert not hasattr(plugin, GP_ACCOUNT_ATTR)

    def test_stamp_without_label_is_a_no_op(self) -> None:
        from graftpunk.cli.login_commands import _stamp_login_identity
        from graftpunk.plugins.cli_plugin import SitePlugin

        class P(SitePlugin):
            site_name = "fmtsite"
            base_url = "https://fmt.example.com"
            session_name = "myshop"

        plugin = P()
        with _stamp_login_identity(plugin, None, None):
            assert plugin.session_name == "myshop"


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
        mock_cache.assert_called_once_with(session, "myshop@alice")

    def test_instance_fallback_for_author_facing_paths(self) -> None:
        """browser_session callers pass nothing; the stamp's state is the input."""
        from graftpunk.plugins.cli_plugin import SitePlugin, cache_login_session

        class P(SitePlugin):
            site_name = "fmtsite"
            base_url = "https://fmt.example.com"
            session_name = "myshop"

        plugin = P()
        setattr(plugin, GP_ACCOUNT_ATTR, "alice@example.com")
        session = requests.Session()
        with patch("graftpunk.plugins.cli_plugin.cache_session") as mock_cache:
            cache_login_session(plugin, session)
        assert getattr(session, GP_ACCOUNT_ATTR) == "alice@example.com"
        mock_cache.assert_called_once_with(session, "myshop")


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
    """A plugin whose ``login(credentials)`` is a fake that records and succeeds."""
    from graftpunk.plugins.cli_plugin import SitePlugin

    class ShopPlugin(SitePlugin):
        site_name = "fmtsite"
        base_url = "https://fmt.example.com"
        session_name = "myshop"

        def login(self, credentials: dict[str, str]) -> bool:
            """Sign in to fmtsite."""
            if seen is not None:
                seen["credentials"] = dict(credentials)
                seen["session_name"] = self.session_name
                seen["identifier"] = getattr(self, GP_ACCOUNT_ATTR, None)
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
    def test_derived_label_names_the_cached_session(self) -> None:
        seen: dict = {}
        result = _invoke(_hand_written_login_plugin(seen), ["fmtsite", "login"], **_CREDS)
        assert result.exit_code == 0, result.output
        assert "Cached session: myshop@alice" in result.output
        # The stamp is visible to the hand-written login while it runs.
        assert seen["session_name"] == "myshop@alice"
        assert seen["identifier"] == "alice"
        assert seen["credentials"] == {"username": "alice", "password": "x"}

    def test_as_flag_overrides_the_label(self) -> None:
        seen: dict = {}
        result = _invoke(
            _hand_written_login_plugin(seen), ["fmtsite", "login", "--as", "work"], **_CREDS
        )
        assert result.exit_code == 0, result.output
        assert "Cached session: myshop@work" in result.output
        assert seen["session_name"] == "myshop@work"

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

    def test_advisory_failure_never_fails_a_successful_login(self) -> None:
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
                "graftpunk.cli.plugin_runtime.load_session_for_api",
                return_value=requests.Session(),
            ),
            patch("graftpunk.cli.plugin_runtime.list_sessions", return_value=[]),
        ):
            result = _invoke(plugin, ["fmtsite", "login"], **_CREDS)
        assert result.exit_code == 0, result.output
        assert mock_update.call_args[0][1] == "myshop"
        assert "Cached session:" not in result.output
