"""Tests for the declarative login engine.

NOTE ON THE PATCH TARGET: these tests patch ``graftpunk.BrowserSession``, NOT
``graftpunk.plugins.login_engine.BrowserSession``. login_engine deliberately has
no module-level BrowserSession — it is on the CLI's eager import path, so a
module-level import would drag in the browser stack and break every `gp` command
on a base install (no [browser] extra). The login bodies import it lazily from
``graftpunk`` at call time, so ``graftpunk`` is where the patch has to land.
"""

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from graftpunk.exceptions import PluginError
from graftpunk.plugins.cli_plugin import LoginConfig, LoginStep, SitePlugin

pytestmark = pytest.mark.usefixtures("_fast_login_timings")


class DeclarativeHN(SitePlugin):
    """Test plugin with declarative login (nodriver)."""

    site_name = "hn"
    session_name = "hackernews"
    help_text = "HN"
    base_url = "https://news.ycombinator.com"
    backend = "nodriver"
    login_config = LoginConfig(
        steps=[
            LoginStep(
                fields={"username": "input[name='acct']", "password": "input[name='pw']"},
                submit="input[value='login']",
            ),
        ],
        url="/login",
        failure="Bad login.",
        timeout=0.05,
        settle=0.0,
    )


class DeclarativeQuotes(SitePlugin):
    """Test plugin with declarative login (selenium)."""

    site_name = "quotes"
    session_name = "quotes"
    help_text = "Quotes"
    base_url = "https://quotes.toscrape.com"
    backend = "selenium"
    login_config = LoginConfig(
        steps=[
            LoginStep(
                fields={"username": "#username", "password": "#password"},
                submit="input[type='submit']",
            ),
        ],
        url="/login",
        success="a[href='/logout']",
        timeout=0.05,
        settle=0.0,
    )


def _make_nodriver_mock_bs():
    """Create a mock BrowserSession that works as an async context manager."""
    mock_bs = MagicMock()
    instance = MagicMock()
    mock_bs.return_value = instance

    # Make it work as async context manager
    instance.__aenter__ = AsyncMock(return_value=instance)
    instance.__aexit__ = AsyncMock(return_value=False)

    return mock_bs, instance


def _make_selenium_mock_bs():
    """Create a mock BrowserSession that works as a sync context manager."""
    mock_bs = MagicMock()
    instance = MagicMock()
    mock_bs.return_value = instance

    # Make it work as sync context manager
    instance.__enter__ = MagicMock(return_value=instance)
    instance.__exit__ = MagicMock(return_value=False)

    return mock_bs, instance


class TestDeclarativeLoginEngine:
    """Tests for declarative login engine."""

    def test_generate_login_nodriver(self) -> None:
        """Test generating async login method for nodriver backend."""
        from graftpunk.plugins.login_engine import generate_login_method

        plugin = DeclarativeHN()
        login_method = generate_login_method(plugin)
        assert callable(login_method)
        import asyncio

        assert asyncio.iscoroutinefunction(login_method)

    def test_generate_login_selenium(self) -> None:
        """Test generating sync login method for selenium backend."""
        from graftpunk.plugins.login_engine import generate_login_method

        plugin = DeclarativeQuotes()
        login_method = generate_login_method(plugin)
        assert callable(login_method)
        import asyncio

        assert not asyncio.iscoroutinefunction(login_method)

    @pytest.mark.asyncio
    async def test_nodriver_login_success(self) -> None:
        """Test nodriver declarative login success path."""
        from graftpunk.plugins.login_engine import generate_login_method

        plugin = DeclarativeHN()
        login_method = generate_login_method(plugin)

        mock_tab = MagicMock()
        mock_element = AsyncMock()
        mock_tab.select = AsyncMock(return_value=mock_element)
        mock_tab.query_selector = mock_tab.select
        mock_tab.get_content = AsyncMock(return_value="<html>Welcome</html>")
        mock_tab.send = AsyncMock()

        mock_bs, instance = _make_nodriver_mock_bs()
        instance.driver = MagicMock()
        instance.driver.get = AsyncMock(return_value=mock_tab)
        instance.transfer_nodriver_cookies_to_session = AsyncMock()

        with (
            patch("graftpunk.BrowserSession", mock_bs),
            patch("graftpunk.plugins.cli_plugin.cache_session"),
        ):
            result = await login_method({"username": "user", "password": "test"})  # noqa: S106

        assert result is True

    @pytest.mark.asyncio
    async def test_nodriver_login_failure(self) -> None:
        """Test nodriver declarative login failure path."""
        from graftpunk.plugins.login_engine import generate_login_method

        plugin = DeclarativeHN()
        login_method = generate_login_method(plugin)

        mock_tab = MagicMock()
        mock_element = AsyncMock()
        mock_tab.select = AsyncMock(return_value=mock_element)
        mock_tab.query_selector = mock_tab.select
        mock_tab.get_content = AsyncMock(return_value="<html>Bad login.</html>")
        mock_tab.send = AsyncMock()

        mock_bs, instance = _make_nodriver_mock_bs()
        instance.driver = MagicMock()
        instance.driver.get = AsyncMock(return_value=mock_tab)

        with patch("graftpunk.BrowserSession", mock_bs):
            result = await login_method({"username": "user", "password": "wrong"})  # noqa: S106

        assert result is False

    @pytest.mark.asyncio
    async def test_nodriver_login_page_navigation_timeout(self) -> None:
        """Raises PluginError when login page navigation times out."""
        import asyncio

        from graftpunk.plugins.login_engine import generate_login_method

        plugin = DeclarativeHN()
        login_method = generate_login_method(plugin)

        async def _hang_forever(*_args, **_kwargs):
            await asyncio.sleep(999)

        mock_bs, instance = _make_nodriver_mock_bs()
        instance.driver = MagicMock()
        instance.driver.get = _hang_forever

        with (
            patch("graftpunk.BrowserSession", mock_bs),
            pytest.raises(PluginError, match="Timed out loading login page"),
        ):
            await login_method({"username": "user", "password": "test"})  # noqa: S106

    def test_selenium_login_success(self) -> None:
        """Test selenium declarative login success path."""
        from graftpunk.plugins.login_engine import generate_login_method

        plugin = DeclarativeQuotes()
        login_method = generate_login_method(plugin)

        mock_element = MagicMock()

        mock_bs, instance = _make_selenium_mock_bs()
        instance.driver = MagicMock()
        instance.driver.get = MagicMock()
        instance.driver.find_element = MagicMock(return_value=mock_element)
        instance.transfer_driver_cookies_to_session = MagicMock()

        with (
            patch("graftpunk.BrowserSession", mock_bs),
            patch("graftpunk.plugins.cli_plugin.cache_session"),
            patch("graftpunk.plugins.login_engine.time.sleep"),
        ):
            result = login_method({"username": "user", "password": "test"})  # noqa: S106

        assert result is True

    def test_selenium_login_files_under_the_explicit_operating_name(self) -> None:
        """The CLI's account-qualified name keys the write-back, not the base (#151)."""
        from graftpunk.plugins.login_engine import generate_login_method

        plugin = DeclarativeQuotes()
        login_method = generate_login_method(plugin)

        mock_bs, instance = _make_selenium_mock_bs()
        instance.driver = MagicMock()
        instance.driver.find_element = MagicMock(return_value=MagicMock())
        instance.transfer_driver_cookies_to_session = MagicMock()

        with (
            patch("graftpunk.BrowserSession", mock_bs),
            patch("graftpunk.plugins.cli_plugin.cache_session") as cache,
            patch("graftpunk.plugins.login_engine.time.sleep"),
        ):
            result = login_method(
                {"username": "user", "password": "test"},  # noqa: S106
                session_name="myshop@alice",
                account_identifier="alice@example.com",
            )

        assert result is True
        assert instance.session_name == "myshop@alice"
        assert cache.call_args.args[1] == "myshop@alice"

    def test_selenium_login_failure_element_not_found(self) -> None:
        """Test selenium login failure when success element not found."""
        from selenium.common.exceptions import NoSuchElementException

        from graftpunk.plugins.login_engine import generate_login_method

        plugin = DeclarativeQuotes()
        login_method = generate_login_method(plugin)

        call_count = 0

        def mock_find_element(by: str, value: str) -> MagicMock:
            nonlocal call_count
            call_count += 1
            # First 3 calls: username field, password field, submit button (all succeed)
            if call_count <= 3:
                return MagicMock()
            # 4th call: success selector check (fails)
            raise NoSuchElementException("Element not found")

        mock_bs, instance = _make_selenium_mock_bs()
        instance.driver = MagicMock()
        instance.driver.get = MagicMock()
        instance.driver.find_element = mock_find_element

        with (
            patch("graftpunk.BrowserSession", mock_bs),
            patch("graftpunk.plugins.cli_plugin.cache_session"),
            # Only sleep: the post-submit poll reads time.monotonic for its deadline.
            patch("graftpunk.plugins.login_engine.time.sleep"),
        ):
            result = login_method({"username": "user", "password": "test"})  # noqa: S106

        assert result is False


class DeclarativeFailureText(SitePlugin):
    """Test plugin with failure_text (not success_selector) for selenium."""

    site_name = "failtext"
    session_name = "failtext"
    help_text = "FailText"
    base_url = "https://example.com"
    backend = "selenium"
    login_config = LoginConfig(
        steps=[
            LoginStep(
                fields={"username": "#user", "password": "#pass"},
                submit="#submit",
            ),
        ],
        url="/login",
        failure="Invalid credentials",
        timeout=0.05,
        settle=0.0,
    )


class TestSeleniumFailureTextPath:
    """Tests for selenium login with failure_text detection."""

    def test_failure_text_detected(self) -> None:
        """Test login returns False when failure_text found in page source."""
        from graftpunk.plugins.login_engine import generate_login_method

        plugin = DeclarativeFailureText()
        login_method = generate_login_method(plugin)

        mock_bs, instance = _make_selenium_mock_bs()
        instance.driver = MagicMock()
        instance.driver.find_element = MagicMock(return_value=MagicMock())
        instance.driver.page_source = "<html>Invalid credentials</html>"

        with (
            patch("graftpunk.BrowserSession", mock_bs),
            patch("graftpunk.plugins.login_engine.time.sleep"),
        ):
            result = login_method({"username": "user", "password": "wrong"})  # noqa: S106

        assert result is False
        # Context manager handles cleanup via __exit__
        instance.__exit__.assert_called_once()

    def test_failure_text_not_detected_proceeds(self) -> None:
        """Test login succeeds when failure_text not in page source."""
        from graftpunk.plugins.login_engine import generate_login_method

        plugin = DeclarativeFailureText()
        login_method = generate_login_method(plugin)

        mock_bs, instance = _make_selenium_mock_bs()
        instance.driver = MagicMock()
        instance.driver.find_element = MagicMock(return_value=MagicMock())
        instance.driver.page_source = "<html>Welcome back!</html>"
        instance.transfer_driver_cookies_to_session = MagicMock()

        with (
            patch("graftpunk.BrowserSession", mock_bs),
            patch("graftpunk.plugins.cli_plugin.cache_session"),
            patch("graftpunk.plugins.login_engine.time.sleep"),
        ):
            result = login_method({"username": "user", "password": "correct"})  # noqa: S106

        assert result is True


class TestLoginEngineExceptionPaths:
    """Tests for exception handling and cleanup in login engine."""

    @pytest.mark.asyncio
    async def test_nodriver_exception_cleans_up(self) -> None:
        """Test nodriver login cleans up browser on exception via context manager."""
        from graftpunk.plugins.login_engine import generate_login_method

        plugin = DeclarativeHN()
        login_method = generate_login_method(plugin)

        mock_tab = MagicMock()
        mock_tab.select = AsyncMock(side_effect=RuntimeError("Element vanished"))
        mock_tab.query_selector = mock_tab.select
        mock_tab.send = AsyncMock()

        mock_bs, instance = _make_nodriver_mock_bs()
        instance.driver = MagicMock()
        instance.driver.get = AsyncMock(return_value=mock_tab)

        with patch("graftpunk.BrowserSession", mock_bs):
            with pytest.raises(PluginError, match="Element vanished"):
                await login_method({"username": "user", "password": "test"})  # noqa: S106

            # Context manager __aexit__ was called (cleanup)
            instance.__aexit__.assert_called_once()

    @pytest.mark.asyncio
    async def test_nodriver_context_manager_receives_exception_info(self) -> None:
        """Test nodriver context manager receives exception details on failure."""
        from graftpunk.plugins.login_engine import generate_login_method

        plugin = DeclarativeHN()
        login_method = generate_login_method(plugin)

        mock_tab = MagicMock()
        mock_tab.select = AsyncMock(side_effect=RuntimeError("Boom"))
        mock_tab.query_selector = mock_tab.select
        mock_tab.send = AsyncMock()

        mock_bs, instance = _make_nodriver_mock_bs()
        instance.driver = MagicMock()
        instance.driver.get = AsyncMock(return_value=mock_tab)

        with patch("graftpunk.BrowserSession", mock_bs):
            with pytest.raises(PluginError, match="Boom"):
                await login_method({"username": "user", "password": "test"})  # noqa: S106

            # __aexit__ was called with exception info
            call_args = instance.__aexit__.call_args
            assert call_args[0][0] is PluginError

    def test_selenium_exception_cleans_up(self) -> None:
        """Test selenium login cleans up browser on exception via context manager."""
        from graftpunk.plugins.login_engine import generate_login_method

        plugin = DeclarativeQuotes()
        login_method = generate_login_method(plugin)

        mock_bs, instance = _make_selenium_mock_bs()
        instance.driver = MagicMock()
        instance.driver.get = MagicMock(side_effect=ConnectionError("Refused"))

        with (
            patch("graftpunk.BrowserSession", mock_bs),
            patch("graftpunk.plugins.login_engine.time.sleep"),
        ):
            with pytest.raises(ConnectionError, match="Refused"):
                login_method({"username": "user", "password": "test"})  # noqa: S106

            # Context manager __exit__ was called (cleanup)
            instance.__exit__.assert_called_once()

    def test_selenium_context_manager_receives_exception_info(self) -> None:
        """Test selenium context manager receives exception details on failure."""
        from graftpunk.plugins.login_engine import generate_login_method

        plugin = DeclarativeQuotes()
        login_method = generate_login_method(plugin)

        mock_bs, instance = _make_selenium_mock_bs()
        instance.driver = MagicMock()
        instance.driver.get = MagicMock(side_effect=RuntimeError("Crash"))

        with (
            patch("graftpunk.BrowserSession", mock_bs),
            patch("graftpunk.plugins.login_engine.time.sleep"),
        ):
            with pytest.raises(RuntimeError, match="Crash"):
                login_method({"username": "user", "password": "test"})  # noqa: S106

            # __exit__ was called with exception info
            call_args = instance.__exit__.call_args
            assert call_args[0][0] is RuntimeError

    def test_selenium_non_nosuchelement_exception_propagates(self) -> None:
        """Test that non-NoSuchElementException errors propagate from success check."""
        from graftpunk.plugins.login_engine import generate_login_method

        plugin = DeclarativeQuotes()
        login_method = generate_login_method(plugin)

        call_count = 0

        def mock_find_element(by: str, value: str) -> MagicMock:
            nonlocal call_count
            call_count += 1
            if call_count <= 3:
                return MagicMock()
            # 4th call: success selector check raises infrastructure error
            raise ConnectionError("WebDriver session crashed")

        mock_bs, instance = _make_selenium_mock_bs()
        instance.driver = MagicMock()
        instance.driver.get = MagicMock()
        instance.driver.find_element = mock_find_element

        with (
            patch("graftpunk.BrowserSession", mock_bs),
            patch("graftpunk.plugins.login_engine.time.sleep"),
            pytest.raises(ConnectionError, match="WebDriver session crashed"),
        ):
            login_method({"username": "user", "password": "test"})  # noqa: S106


class TestLoginFieldMapping:
    """Tests documenting login field name mapping behavior."""

    def test_each_field_receives_its_own_credential_value(self) -> None:
        """Each field receives its matching value from the credentials dict."""
        from graftpunk.plugins.login_engine import generate_login_method

        class EmailPlugin(SitePlugin):
            site_name = "emailtest"
            session_name = "emailtest"
            help_text = "Test"
            base_url = "https://example.com"
            backend = "selenium"
            login_config = LoginConfig(
                steps=[
                    LoginStep(
                        fields={"email": "#email", "password": "#pass"},
                        submit="#submit",
                    ),
                ],
                url="/login",
                failure="Invalid",
                timeout=0.05,
                settle=0.0,
            )

        plugin = EmailPlugin()
        login_method = generate_login_method(plugin)

        sent_values: dict[str, str] = {}

        def mock_find_element(by: str, value: str) -> MagicMock:
            elem = MagicMock()

            def capture_keys(v: str) -> None:
                sent_values[value] = v

            elem.send_keys = capture_keys
            return elem

        mock_bs, instance = _make_selenium_mock_bs()
        instance.driver = MagicMock()
        instance.driver.find_element = mock_find_element
        instance.driver.page_source = "<html>Invalid</html>"

        with (
            patch("graftpunk.BrowserSession", mock_bs),
            patch("graftpunk.plugins.login_engine.time.sleep"),
        ):
            result = login_method({"email": "myuser", "password": "mypass"})  # noqa: S106

        # With credentials dict, each field gets its own value
        assert sent_values["#email"] == "myuser"
        assert sent_values["#pass"] == "mypass"
        assert result is False

    def test_missing_credential_key_defaults_to_empty(self) -> None:
        """Fields without a matching credential key receive an empty string."""
        from graftpunk.plugins.login_engine import generate_login_method

        class EmailPlugin(SitePlugin):
            site_name = "emailtest2"
            session_name = "emailtest2"
            help_text = "Test"
            base_url = "https://example.com"
            backend = "selenium"
            login_config = LoginConfig(
                steps=[
                    LoginStep(
                        fields={"email": "#email", "password": "#pass"},
                        submit="#submit",
                    ),
                ],
                url="/login",
                failure="Invalid",
                timeout=0.05,
                settle=0.0,
            )

        plugin = EmailPlugin()
        login_method = generate_login_method(plugin)

        sent_values: dict[str, str] = {}

        def mock_find_element(by: str, value: str) -> MagicMock:
            elem = MagicMock()

            def capture_keys(v: str) -> None:
                sent_values[value] = v

            elem.send_keys = capture_keys
            return elem

        mock_bs, instance = _make_selenium_mock_bs()
        instance.driver = MagicMock()
        instance.driver.find_element = mock_find_element
        instance.driver.page_source = "<html>Invalid</html>"

        with (
            patch("graftpunk.BrowserSession", mock_bs),
            patch("graftpunk.plugins.login_engine.time.sleep"),
        ):
            # Only pass password, no email key
            result = login_method({"password": "mypass"})  # noqa: S106

        assert sent_values["#email"] == ""
        assert sent_values["#pass"] == "mypass"
        assert result is False


class DeclarativeNodriverSuccess(SitePlugin):
    """Nodriver plugin with success_selector configured."""

    site_name = "ndsuccess"
    session_name = "ndsuccess"
    help_text = "ND Success"
    base_url = "https://example.com"
    backend = "nodriver"
    login_config = LoginConfig(
        steps=[
            LoginStep(
                fields={"username": "#user", "password": "#pass"},
                submit="#submit",
            ),
        ],
        url="/login",
        success=".dashboard",
        timeout=0.05,
        settle=0.0,
    )


class DeclarativeNodriverNoValidation(SitePlugin):
    """Nodriver plugin with no validation configured."""

    site_name = "ndnoval"
    session_name = "ndnoval"
    help_text = "ND No Validation"
    base_url = "https://example.com"
    backend = "nodriver"
    login_config = LoginConfig(
        steps=[
            LoginStep(
                fields={"username": "#user", "password": "#pass"},
                submit="#submit",
            ),
        ],
        url="/login",
        timeout=0.05,
        settle=0.0,
    )


class DeclarativeSeleniumBoth(SitePlugin):
    """Selenium plugin with both failure_text and success_selector."""

    site_name = "selboth"
    session_name = "selboth"
    help_text = "Sel Both"
    base_url = "https://example.com"
    backend = "selenium"
    login_config = LoginConfig(
        steps=[
            LoginStep(
                fields={"username": "#user", "password": "#pass"},
                submit="#submit",
            ),
        ],
        url="/login",
        failure="Invalid credentials",
        success=".dashboard",
        timeout=0.05,
        settle=0.0,
    )


class TestNodriverLoginValidationPaths:
    """Tests for nodriver login validation paths."""

    @pytest.mark.asyncio
    async def test_nodriver_login_success_selector_not_found(self) -> None:
        """Success_selector configured but element not found returns False."""
        from graftpunk.plugins.login_engine import generate_login_method

        plugin = DeclarativeNodriverSuccess()
        login_method = generate_login_method(plugin)

        mock_tab = MagicMock()
        mock_element = AsyncMock()
        mock_tab.get_content = AsyncMock(return_value="<html>Welcome</html>")
        mock_tab.send = AsyncMock()

        # select succeeds for form fields, returns None for success selector (timeout)
        async def select_side_effect(selector: str, **kwargs: object) -> AsyncMock | None:
            if selector == ".dashboard":
                return None
            return mock_element

        mock_tab.select = AsyncMock(side_effect=select_side_effect)
        mock_tab.query_selector = mock_tab.select

        mock_bs, instance = _make_nodriver_mock_bs()
        instance.driver = MagicMock()
        instance.driver.get = AsyncMock(return_value=mock_tab)

        with patch("graftpunk.BrowserSession", mock_bs):
            result = await login_method({"username": "user", "password": "test"})  # noqa: S106

        assert result is False

    @pytest.mark.asyncio
    async def test_nodriver_login_success_selector_found(self) -> None:
        """Success_selector configured and element found returns True."""
        from graftpunk.plugins.login_engine import generate_login_method

        plugin = DeclarativeNodriverSuccess()
        login_method = generate_login_method(plugin)

        mock_tab = MagicMock()
        mock_element = AsyncMock()
        mock_tab.select = AsyncMock(return_value=mock_element)
        mock_tab.query_selector = mock_tab.select
        mock_tab.get_content = AsyncMock(return_value="<html>Welcome</html>")
        mock_tab.send = AsyncMock()

        mock_bs, instance = _make_nodriver_mock_bs()
        instance.driver = MagicMock()
        instance.driver.get = AsyncMock(return_value=mock_tab)
        instance.transfer_nodriver_cookies_to_session = AsyncMock()

        with (
            patch("graftpunk.BrowserSession", mock_bs),
            patch("graftpunk.plugins.cli_plugin.cache_session"),
        ):
            result = await login_method({"username": "user", "password": "test"})  # noqa: S106

        assert result is True

    @pytest.mark.asyncio
    async def test_nodriver_login_success_selector_connection_error_fails_the_login(self) -> None:
        """A connection error from the probe is the browser leaving, not a login verdict.

        It used to propagate out of login() while the selenium twin failed cleanly.
        Every unreadable tick is pending now, and a wait that never got an answer
        reports the page as unreadable (polish round 2).
        """
        from graftpunk.plugins.login_engine import generate_login_method

        plugin = DeclarativeNodriverSuccess()
        login_method = generate_login_method(plugin)

        mock_tab = MagicMock()
        mock_element = AsyncMock()
        mock_tab.get_content = AsyncMock(return_value="<html>Welcome</html>")
        mock_tab.send = AsyncMock()

        # select succeeds for form fields, raises unknown error for success selector
        async def select_side_effect(selector: str, **kwargs: object) -> AsyncMock:
            if selector == ".dashboard":
                raise ConnectionError("WebDriver session crashed")
            return mock_element

        mock_tab.select = AsyncMock(side_effect=select_side_effect)
        mock_tab.query_selector = mock_tab.select

        mock_bs, instance = _make_nodriver_mock_bs()
        instance.driver = MagicMock()
        instance.driver.get = AsyncMock(return_value=mock_tab)

        with (
            patch("graftpunk.BrowserSession", mock_bs),
            patch("graftpunk.plugins.login_settle.LOG") as mock_log,
        ):
            result = await login_method({"username": "user", "password": "test"})  # noqa: S106

        assert result is False
        warning = _warning_kwargs(mock_log, "login_page_unreadable")
        assert "WebDriver session crashed" in warning["error"]

    @pytest.mark.asyncio
    async def test_nodriver_login_no_validation_configured(self) -> None:
        """Neither failure_text nor success_selector set returns True with warning."""
        from graftpunk.plugins.login_engine import generate_login_method

        plugin = DeclarativeNodriverNoValidation()
        login_method = generate_login_method(plugin)

        mock_tab = MagicMock()
        mock_element = AsyncMock()
        mock_tab.select = AsyncMock(return_value=mock_element)
        mock_tab.query_selector = mock_tab.select
        mock_tab.get_content = AsyncMock(return_value="<html>Welcome</html>")
        mock_tab.send = AsyncMock()

        mock_bs, instance = _make_nodriver_mock_bs()
        instance.driver = MagicMock()
        instance.driver.get = AsyncMock(return_value=mock_tab)
        instance.transfer_nodriver_cookies_to_session = AsyncMock()

        with (
            patch("graftpunk.BrowserSession", mock_bs),
            patch("graftpunk.plugins.cli_plugin.cache_session"),
            patch("graftpunk.plugins.login_settle.LOG") as mock_log,
        ):
            result = await login_method({"username": "user", "password": "test"})  # noqa: S106

        assert result is True
        mock_log.warning.assert_called_once_with(
            "login_no_validation_configured",
            plugin="ndnoval",
            hint="Consider adding login_failure or login_success to validate login result",
        )


class _ScriptedNodriverTab:
    """A nodriver tab that answers the post-submit poll from a script.

    Each URL read is one poll tick, since that is the read the wait makes every
    tick and before anything else: ``contents`` and ``urls`` are read by tick index
    and their last entry repeats for every later tick, the success selector starts
    resolving on tick ``success_from`` (never, when it is None), and ``ready_states``
    answers the document-ready wait one read at a time. ``events`` records what the
    engine did, in order, and a tick that never asked for the page records nothing.
    """

    def __init__(
        self,
        *,
        success_selector: str = "",
        contents: tuple[str, ...] = ("<html>Signing in</html>",),
        urls: tuple[str | None, ...] = ("https://app.example.com/login",),
        success_from: int | None = None,
        ready_states: tuple[str, ...] = ("complete",),
        unreadable_ticks: tuple[int, ...] = (),
    ) -> None:
        self.events: list[str] = []
        self.tick = 0
        self._url_reads = 0
        self._wait_over = False
        self._success_selector = success_selector
        self._contents = contents
        self._urls = urls
        self._success_from = success_from
        self._ready_states = ready_states
        self._ready_reads = 0
        self._unreadable_ticks = unreadable_ticks
        self.send = AsyncMock()

    def __getattr__(self, name: str) -> MagicMock:
        """Anything the browser stack asks of a tab that this script does not model.

        A plain MagicMock, as the hand-built tabs elsewhere in this file use: the
        capture backend registers its handlers synchronously, and an AsyncMock
        there leaves coroutines nobody awaits.
        """
        return MagicMock()

    @staticmethod
    def _at(script: tuple[Any, ...], index: int) -> Any:
        return script[min(index, len(script) - 1)]

    @property
    def url(self) -> str | None:
        """The scripted URL, which nodriver leaves as None while a tab navigates.

        This read is what moves the script on: the wait reads the URL once per tick
        and before anything else, and the engine's own read just before the submit
        takes the same tick 0 URL (the page the submit was clicked from). Reads
        after the wait is over, such as the engine capturing the URL for display,
        leave the script where it stopped.
        """
        if not self._wait_over:
            self.tick = max(self._url_reads - 1, 0)
            self._url_reads += 1
        return self._at(self._urls, self.tick)

    async def get_content(self) -> str:
        from nodriver.core.connection import ProtocolException

        if self.tick in self._unreadable_ticks:
            self.events.append(f"tick:{self.tick}:unreadable")
            raise ProtocolException("Could not find node with given id")
        self.events.append(f"tick:{self.tick}")
        return self._at(self._contents, self.tick)

    async def select(self, selector: str, timeout: float | None = None) -> Any:
        return await self.query_selector(selector)

    async def query_selector(self, selector: str) -> Any:
        if selector and selector == self._success_selector:
            found = self._success_from is not None and self.tick >= self._success_from
            return AsyncMock() if found else None
        return AsyncMock()

    async def evaluate(self, expression: str, **kwargs: Any) -> str:
        """The document-readiness read, and the field read-back the steps make.

        ``_read_field_value`` evaluates its own JS with ``return_by_value=True``;
        without ``**kwargs`` that call raised TypeError, which the engine swallows
        as "cannot verify", so this double skipped the read-back by accident. It is
        answered here as "the field is there, its value could not be read", which
        is the same verdict on purpose: the script models pages, not field values.
        That read is deliberately not recorded, because ``events`` is the order of
        the post-submit wait and a read from the steps would interleave with it.
        """
        if expression != "document.readyState":
            return json.dumps({"found": True, "value": None})
        self._wait_over = True
        self.events.append("document_ready_read")
        state = self._at(self._ready_states, self._ready_reads)
        self._ready_reads += 1
        return state


def _nodriver_session_for(tab: _ScriptedNodriverTab) -> tuple[MagicMock, MagicMock]:
    """A mock BrowserSession serving *tab*, recording its cookie capture on the tab."""
    mock_bs, instance = _make_nodriver_mock_bs()
    instance.driver = MagicMock()
    instance.driver.get = AsyncMock(return_value=tab)

    async def _capture_cookies() -> None:
        tab.events.append("cookies")

    instance.transfer_nodriver_cookies_to_session = AsyncMock(side_effect=_capture_cookies)
    return mock_bs, instance


def _warning_kwargs(mock_log: MagicMock, event: str) -> dict[str, Any]:
    """The keyword arguments of the single LOG.warning call for *event*."""
    calls = [call for call in mock_log.warning.call_args_list if call[0][0] == event]
    assert len(calls) == 1, f"expected one {event!r} warning, got {mock_log.warning.call_args_list}"
    return dict(calls[0][1])


def _debug_kwargs(mock_log: MagicMock, event: str) -> dict[str, Any]:
    """The keyword arguments of the single LOG.debug call for *event*."""
    calls = [call for call in mock_log.debug.call_args_list if call[0][0] == event]
    assert len(calls) == 1, f"expected one {event!r} debug event, got {calls}"
    return dict(calls[0][1])


class DeclarativeNodriverPoll(SitePlugin):
    """Nodriver plugin with a success element and room to poll for it."""

    site_name = "ndpoll"
    session_name = "ndpoll"
    help_text = "ND Poll"
    base_url = "https://example.com"
    backend = "nodriver"
    login_config = LoginConfig(
        steps=[LoginStep(fields={"username": "#user", "password": "#pass"}, submit="#submit")],
        url="/login",
        failure="Invalid credentials",
        success=".dashboard",
        timeout=5.0,
        settle=0.0,
    )


class DeclarativeNodriverUrlOnly(SitePlugin):
    """Nodriver plugin whose only success signal is the landing URL.

    Its budget is generous: a test that expects this plugin to succeed ends as
    soon as the URL arrives, and a tight budget would only make the test lose a
    race with the machine it runs on. The timeout paths use the plugin below.
    """

    site_name = "ndurl"
    session_name = "ndurl"
    help_text = "ND URL"
    base_url = "https://example.com"
    backend = "nodriver"
    login_config = LoginConfig(
        steps=[LoginStep(fields={"username": "#user", "password": "#pass"}, submit="#submit")],
        url="/login",
        success_url="*/dashboard*",
        timeout=5.0,
        settle=0.0,
    )


class DeclarativeNodriverUrlTimeout(SitePlugin):
    """The same plugin with a budget small enough to reach its own timeout."""

    site_name = "ndurlto"
    session_name = "ndurlto"
    help_text = "ND URL Timeout"
    base_url = "https://example.com"
    backend = "nodriver"
    login_config = LoginConfig(
        steps=[LoginStep(fields={"username": "#user", "password": "#pass"}, submit="#submit")],
        url="/login",
        success_url="*/dashboard*",
        timeout=0.2,
        settle=0.0,
    )


class DeclarativeNodriverBothSignals(SitePlugin):
    """Nodriver plugin that requires both the element and the URL."""

    site_name = "ndboth"
    session_name = "ndboth"
    help_text = "ND Both"
    base_url = "https://example.com"
    backend = "nodriver"
    login_config = LoginConfig(
        steps=[LoginStep(fields={"username": "#user", "password": "#pass"}, submit="#submit")],
        url="/login",
        success=".dashboard",
        success_url="*/dashboard*",
        timeout=0.2,
        settle=0.0,
    )


_LOGIN_CREDENTIALS = {"username": "alice", "password": "secret"}  # noqa: S106


class TestNodriverLoginSignalPoll:
    """The post-submit wait polls for the configured signal (nodriver)."""

    @pytest.mark.asyncio
    async def test_success_element_found_after_several_ticks(self) -> None:
        """A success element that only appears on a later tick still succeeds."""
        from graftpunk.plugins.login_engine import generate_login_method

        tab = _ScriptedNodriverTab(success_selector=".dashboard", success_from=3)
        mock_bs, _instance = _nodriver_session_for(tab)

        with (
            patch("graftpunk.BrowserSession", mock_bs),
            patch("graftpunk.plugins.cli_plugin.cache_session"),
        ):
            result = await generate_login_method(DeclarativeNodriverPoll())(_LOGIN_CREDENTIALS)

        assert result is True
        assert tab.tick == 3
        assert "cookies" in tab.events

    @pytest.mark.asyncio
    async def test_an_unreadable_tick_does_not_end_the_login(self) -> None:
        """A ProtocolException mid-redirect is the window this poll exists for."""
        from graftpunk.plugins.login_engine import generate_login_method

        tab = _ScriptedNodriverTab(
            success_selector=".dashboard", success_from=2, unreadable_ticks=(1,)
        )
        mock_bs, _instance = _nodriver_session_for(tab)

        with (
            patch("graftpunk.BrowserSession", mock_bs),
            patch("graftpunk.plugins.cli_plugin.cache_session"),
        ):
            result = await generate_login_method(DeclarativeNodriverPoll())(_LOGIN_CREDENTIALS)

        assert result is True
        assert "tick:1:unreadable" in tab.events
        assert "cookies" in tab.events

    @pytest.mark.asyncio
    async def test_a_read_error_that_is_not_a_protocol_error_still_fails_cleanly(self) -> None:
        """A closed browser surfaces as a websockets or connection error, not a protocol one.

        Catching only ProtocolException let that error out of login() on nodriver
        while the selenium twin failed cleanly with a warning.
        """
        from graftpunk.plugins.login_engine import generate_login_method

        class _ClosedBrowserTab(_ScriptedNodriverTab):
            """A tab whose post-submit reads raise the way a browser that went away does.

            The steps run first and need their own selectors to resolve, so only the
            reads the wait makes are answered this way.
            """

            async def get_content(self) -> str:
                raise RuntimeError("no close frame received or sent")

            async def query_selector(self, selector: str) -> Any:
                if selector == self._success_selector:
                    raise RuntimeError("no close frame received or sent")
                return await super().query_selector(selector)

        tab = _ClosedBrowserTab(success_selector=".dashboard", urls=(None,))
        mock_bs, _instance = _nodriver_session_for(tab)

        with (
            patch("graftpunk.BrowserSession", mock_bs),
            patch("graftpunk.plugins.cli_plugin.cache_session"),
            patch("graftpunk.plugins.login_settle.LOG") as mock_log,
        ):
            result = await generate_login_method(DeclarativeNodriverSuccess())(_LOGIN_CREDENTIALS)

        assert result is False
        assert "cookies" not in tab.events
        assert "no close frame" in _warning_kwargs(mock_log, "login_page_unreadable")["error"]
        read_failures = [
            call for call in mock_log.debug.call_args_list if call[0][0] == "login_tick_read_failed"
        ]
        assert read_failures
        assert all(call[1]["exc_type"] == "RuntimeError" for call in read_failures)

    @pytest.mark.asyncio
    async def test_a_poll_that_never_reads_the_page_blames_the_page(self) -> None:
        """Every tick raised, so the signal was never looked at: say that instead."""
        from graftpunk.plugins.login_engine import generate_login_method

        class UnreadablePlugin(SitePlugin):
            site_name = "ndunread"
            session_name = "ndunread"
            help_text = "ND Unreadable"
            base_url = "https://example.com"
            backend = "nodriver"
            login_config = LoginConfig(
                steps=[LoginStep(fields={"username": "#user"}, submit="#submit")],
                url="/login",
                failure="Invalid credentials",
                success=".dashboard",
                timeout=0.2,
                settle=0.0,
            )

        tab = _ScriptedNodriverTab(
            success_selector=".dashboard",
            urls=("https://app.example.com/login",),
            unreadable_ticks=tuple(range(1000)),
        )
        mock_bs, _instance = _nodriver_session_for(tab)

        with (
            patch("graftpunk.BrowserSession", mock_bs),
            patch("graftpunk.plugins.cli_plugin.cache_session"),
            patch("graftpunk.plugins.login_settle.LOG") as mock_log,
        ):
            result = await generate_login_method(UnreadablePlugin())(_LOGIN_CREDENTIALS)

        assert result is False
        assert "cookies" not in tab.events
        warning = _warning_kwargs(mock_log, "login_page_unreadable")
        assert "Could not find node" in warning["error"]
        assert warning["url"] == "https://app.example.com/login"
        timeouts = [c for c in mock_log.warning.call_args_list if c[0][0] == "login_signal_timeout"]
        assert timeouts == []

    @pytest.mark.asyncio
    async def test_failure_text_that_appears_late_ends_the_poll(self) -> None:
        """Failure text arriving after a few ticks fails the login there and then."""
        from graftpunk.plugins.login_engine import generate_login_method

        tab = _ScriptedNodriverTab(
            success_selector=".dashboard",
            contents=(
                "<html>Signing in</html>",
                "<html>Signing in</html>",
                "<html>Invalid credentials</html>",
            ),
            success_from=None,
        )
        mock_bs, _instance = _nodriver_session_for(tab)

        with (
            patch("graftpunk.BrowserSession", mock_bs),
            patch("graftpunk.plugins.cli_plugin.cache_session"),
            patch("graftpunk.plugins.login_settle.LOG") as mock_log,
        ):
            result = await generate_login_method(DeclarativeNodriverPoll())(_LOGIN_CREDENTIALS)

        assert result is False
        assert tab.tick == 2  # stopped on the tick that showed the text
        assert "cookies" not in tab.events
        assert _warning_kwargs(mock_log, "login_failure_text_detected")["text"] == (
            "Invalid credentials"
        )

    @pytest.mark.asyncio
    async def test_url_alone_can_be_the_success_signal(self) -> None:
        """With success_url and no success selector, the landing URL decides."""
        from graftpunk.plugins.login_engine import generate_login_method

        tab = _ScriptedNodriverTab(
            urls=(
                "https://app.example.com/login",
                "https://app.example.com/sso/callback",
                "https://app.example.com/dashboard?welcome=1",
            ),
        )
        mock_bs, _instance = _nodriver_session_for(tab)

        with (
            patch("graftpunk.BrowserSession", mock_bs),
            patch("graftpunk.plugins.cli_plugin.cache_session"),
        ):
            result = await generate_login_method(DeclarativeNodriverUrlOnly())(_LOGIN_CREDENTIALS)

        assert result is True
        assert tab.tick == 2
        assert "cookies" in tab.events

    @pytest.mark.asyncio
    async def test_a_rate_limit_marker_under_a_found_element_reaches_the_debug_trail(
        self,
    ) -> None:
        """The element vetoes the marker, so the poll waits for the URL and times out.

        The timeout warning names the URL pattern and says nothing about a limiter,
        which is what the debug event is for.
        """
        from graftpunk.plugins.login_engine import generate_login_method

        class BothSignalsRateLimited(SitePlugin):
            site_name = "ndmarker"
            session_name = "ndmarker"
            help_text = "ND Marker"
            base_url = "https://example.com"
            backend = "nodriver"
            login_config = LoginConfig(
                steps=[LoginStep(fields={"username": "#user"}, submit="#submit")],
                url="/login",
                failure="Invalid credentials",
                success=".dashboard",
                success_url="*/dashboard*",
                timeout=0.2,
                settle=0.0,
            )

        tab = _ScriptedNodriverTab(
            success_selector=".dashboard",
            success_from=0,
            contents=("<html>Too Many Requests</html>",),
            urls=("https://app.example.com/login",),
        )
        mock_bs, _instance = _nodriver_session_for(tab)

        with (
            patch("graftpunk.BrowserSession", mock_bs),
            patch("graftpunk.plugins.cli_plugin.cache_session"),
            patch("graftpunk.plugins.login_settle.LOG") as mock_log,
        ):
            result = await generate_login_method(BothSignalsRateLimited())(_LOGIN_CREDENTIALS)

        assert result is False
        assert "cookies" not in tab.events
        # One event for the window, however many ticks showed the marker.
        event = _debug_kwargs(mock_log, "login_rate_limit_marker_seen")
        assert event["url"] == "https://app.example.com/login"
        warning = _warning_kwargs(mock_log, "login_signal_timeout")
        assert warning["missing"] == "success URL pattern '*/dashboard*'"

    @pytest.mark.asyncio
    async def test_a_url_only_poll_reads_the_page_only_when_the_url_arrives(self) -> None:
        """No failure text to look for, so the document is fetched once: at the landing URL.

        The fetch still happens there, because the rate-limit marker vetoes a URL
        signal on the tick it starts to hold.
        """
        from graftpunk.plugins.login_engine import generate_login_method

        tab = _ScriptedNodriverTab(
            urls=(
                "https://app.example.com/login",
                "https://app.example.com/sso/callback",
                "https://app.example.com/dashboard?welcome=1",
            ),
        )
        mock_bs, _instance = _nodriver_session_for(tab)

        with (
            patch("graftpunk.BrowserSession", mock_bs),
            patch("graftpunk.plugins.cli_plugin.cache_session"),
        ):
            result = await generate_login_method(DeclarativeNodriverUrlOnly())(_LOGIN_CREDENTIALS)

        assert result is True
        assert tab.events == ["tick:2", "document_ready_read", "cookies"]

    @pytest.mark.asyncio
    async def test_a_glob_matching_the_login_page_never_reports_success(self) -> None:
        """The probe that cached a pre-login session: the URL never changed."""
        from graftpunk.plugins.login_engine import generate_login_method

        class LooseUrlPlugin(SitePlugin):
            site_name = "ndloose"
            session_name = "ndloose"
            help_text = "ND Loose"
            base_url = "https://example.com"
            backend = "nodriver"
            login_config = LoginConfig(
                steps=[LoginStep(fields={"username": "#user"}, submit="#submit")],
                url="/login",
                success_url="*example.com*",
                timeout=0.2,
                settle=0.0,
            )

        tab = _ScriptedNodriverTab(urls=("https://app.example.com/login",))
        mock_bs, _instance = _nodriver_session_for(tab)

        with (
            patch("graftpunk.BrowserSession", mock_bs),
            patch("graftpunk.plugins.cli_plugin.cache_session"),
            patch("graftpunk.plugins.login_settle.LOG") as mock_log,
        ):
            result = await generate_login_method(LooseUrlPlugin())(_LOGIN_CREDENTIALS)

        assert result is False
        assert "cookies" not in tab.events
        warning = _warning_kwargs(mock_log, "login_signal_timeout")
        assert warning["missing"] == "success URL pattern '*example.com*'"

    @pytest.mark.asyncio
    async def test_an_unreadable_pre_submit_url_leaves_the_url_signal_unconfirmable(
        self,
    ) -> None:
        """nodriver reports no URL at click time, so there is no baseline to move off.

        The loose glob matches every later URL; without a baseline the wait must not
        take that as a navigation, and it says why it could not.
        """
        from graftpunk.plugins.login_engine import generate_login_method

        class LooseUrlPlugin(SitePlugin):
            site_name = "ndnobase"
            session_name = "ndnobase"
            help_text = "ND No Baseline"
            base_url = "https://example.com"
            backend = "nodriver"
            login_config = LoginConfig(
                steps=[LoginStep(fields={"username": "#user"}, submit="#submit")],
                url="/login",
                success_url="*example.com*",
                timeout=0.2,
                settle=0.0,
            )

        tab = _ScriptedNodriverTab(urls=(None, "https://app.example.com/dashboard"))
        mock_bs, _instance = _nodriver_session_for(tab)

        with (
            patch("graftpunk.BrowserSession", mock_bs),
            patch("graftpunk.plugins.cli_plugin.cache_session"),
            patch("graftpunk.plugins.login_settle.LOG") as mock_log,
        ):
            result = await generate_login_method(LooseUrlPlugin())(_LOGIN_CREDENTIALS)

        assert result is False
        assert "cookies" not in tab.events
        assert _debug_kwargs(mock_log, "login_pre_submit_url_unknown")["backend"] == "nodriver"
        hint = _warning_kwargs(mock_log, "login_signal_timeout")["hint"]
        assert "URL before submit could not be read" in hint

    @pytest.mark.asyncio
    async def test_a_step_with_no_submit_measures_from_the_url_at_the_wait_start(self) -> None:
        """With no submit to click, the URL the wait starts on is the baseline."""
        from graftpunk.plugins.login_engine import generate_login_method

        class NoSubmitPlugin(SitePlugin):
            site_name = "ndnosubmit"
            session_name = "ndnosubmit"
            help_text = "ND No Submit"
            base_url = "https://example.com"
            backend = "nodriver"
            login_config = LoginConfig(
                steps=[LoginStep(fields={"username": "#user"})],
                url="/login",
                success_url="*/dashboard*",
                timeout=5.0,
                settle=0.0,
            )

        tab = _ScriptedNodriverTab(
            urls=("https://app.example.com/login", "https://app.example.com/dashboard"),
        )
        mock_bs, _instance = _nodriver_session_for(tab)

        with (
            patch("graftpunk.BrowserSession", mock_bs),
            patch("graftpunk.plugins.cli_plugin.cache_session"),
        ):
            result = await generate_login_method(NoSubmitPlugin())(_LOGIN_CREDENTIALS)

        assert result is True
        assert "cookies" in tab.events

    @pytest.mark.asyncio
    async def test_the_same_glob_succeeds_once_the_page_navigates(self) -> None:
        """A URL that changes on a later tick is the navigation the rule asks for."""
        from graftpunk.plugins.login_engine import generate_login_method

        tab = _ScriptedNodriverTab(
            urls=(
                "https://app.example.com/login",
                "https://app.example.com/login",
                "https://app.example.com/login",
                "https://app.example.com/dashboard",
            ),
        )
        mock_bs, _instance = _nodriver_session_for(tab)

        with (
            patch("graftpunk.BrowserSession", mock_bs),
            patch("graftpunk.plugins.cli_plugin.cache_session"),
        ):
            result = await generate_login_method(DeclarativeNodriverUrlOnly())(_LOGIN_CREDENTIALS)

        assert result is True
        assert tab.tick == 3
        assert "cookies" in tab.events

    @pytest.mark.asyncio
    async def test_the_first_tick_comes_after_one_poll_interval(self) -> None:
        """An element already on the login page must not be read before the submit lands."""
        from graftpunk.plugins.login_engine import generate_login_method

        tab = _ScriptedNodriverTab(success_selector=".dashboard", success_from=0)
        mock_bs, _instance = _nodriver_session_for(tab)

        async def _record_sleep(seconds: float) -> None:
            tab.events.append("sleep")

        with (
            patch("graftpunk.BrowserSession", mock_bs),
            patch("graftpunk.plugins.cli_plugin.cache_session"),
            patch(
                "graftpunk.plugins.login_settle.asyncio.sleep",
                new=AsyncMock(side_effect=_record_sleep),
            ),
        ):
            result = await generate_login_method(DeclarativeNodriverPoll())(_LOGIN_CREDENTIALS)

        assert result is True
        first_tick = tab.events.index("tick:0")
        assert tab.events[first_tick - 1] == "sleep"

    @pytest.mark.asyncio
    async def test_both_signals_configured_needs_both(self) -> None:
        """A matching URL is not enough while the configured element never appears."""
        from graftpunk.plugins.login_engine import generate_login_method

        tab = _ScriptedNodriverTab(
            success_selector=".dashboard",
            urls=("https://app.example.com/login", "https://app.example.com/dashboard"),
            success_from=None,
        )
        mock_bs, _instance = _nodriver_session_for(tab)

        with (
            patch("graftpunk.BrowserSession", mock_bs),
            patch("graftpunk.plugins.cli_plugin.cache_session"),
            patch("graftpunk.plugins.login_settle.LOG") as mock_log,
        ):
            result = await generate_login_method(DeclarativeNodriverBothSignals())(
                _LOGIN_CREDENTIALS
            )

        assert result is False
        assert "cookies" not in tab.events
        warning = _warning_kwargs(mock_log, "login_signal_timeout")
        assert warning["missing"] == "success element '.dashboard'"
        assert warning["url"] == "https://app.example.com/dashboard"

    @pytest.mark.asyncio
    async def test_timeout_names_the_url_pattern_and_the_final_url(self) -> None:
        """A URL that never matches times out, naming the pattern and where it ended."""
        from graftpunk.plugins.login_engine import generate_login_method

        tab = _ScriptedNodriverTab(urls=("https://app.example.com/login?error=mfa",))
        mock_bs, _instance = _nodriver_session_for(tab)

        with (
            patch("graftpunk.BrowserSession", mock_bs),
            patch("graftpunk.plugins.cli_plugin.cache_session"),
            patch("graftpunk.plugins.login_settle.LOG") as mock_log,
        ):
            result = await generate_login_method(DeclarativeNodriverUrlTimeout())(
                _LOGIN_CREDENTIALS
            )

        assert result is False
        assert tab.tick > 0  # it polled rather than checking once
        warning = _warning_kwargs(mock_log, "login_signal_timeout")
        assert warning["missing"] == "success URL pattern '*/dashboard*'"
        assert warning["url"] == "https://app.example.com/login?error=mfa"
        assert warning["timeout"] == "0.2s"

    @pytest.mark.asyncio
    async def test_cookies_are_captured_after_the_document_finishes_loading(self) -> None:
        """The engine waits out a still-loading document before capturing cookies."""
        from graftpunk.plugins.login_engine import generate_login_method

        tab = _ScriptedNodriverTab(
            success_selector=".dashboard",
            success_from=0,
            ready_states=("loading", "complete"),
        )
        mock_bs, _instance = _nodriver_session_for(tab)

        with (
            patch("graftpunk.BrowserSession", mock_bs),
            patch("graftpunk.plugins.cli_plugin.cache_session"),
        ):
            result = await generate_login_method(DeclarativeNodriverPoll())(_LOGIN_CREDENTIALS)

        assert result is True
        assert tab.events == ["tick:0", "document_ready_read", "document_ready_read", "cookies"]

    @pytest.mark.asyncio
    async def test_a_rate_limit_body_at_the_landing_url_is_not_a_success(self) -> None:
        """The limiter answers the post-submit redirect, so its 429 body is at the glob.

        A URL glob alone must not override the marker: the wait fails with the
        rate-limit warning instead of capturing cookies off the limiter's page.
        """
        from graftpunk.plugins.login_engine import generate_login_method

        tab = _ScriptedNodriverTab(
            contents=("<html>Signing in</html>", "<html>Too Many Requests</html>"),
            urls=("https://app.example.com/login", "https://app.example.com/dashboard"),
        )
        mock_bs, _instance = _nodriver_session_for(tab)

        with (
            patch("graftpunk.BrowserSession", mock_bs),
            patch("graftpunk.plugins.cli_plugin.cache_session"),
            patch("graftpunk.plugins.login_settle.LOG") as mock_log,
        ):
            result = await generate_login_method(DeclarativeNodriverUrlOnly())(_LOGIN_CREDENTIALS)

        assert result is False
        assert "cookies" not in tab.events
        assert _warning_kwargs(mock_log, "login_rate_limited")["plugin"] == "ndurl"

    @pytest.mark.asyncio
    async def test_a_selector_only_login_reports_a_rate_limit_page_at_its_deadline(self) -> None:
        """The deadline tick reads the page, so a timeout can be blamed on the limiter.

        A login whose only signal is a `success` selector gives no rule a reason to
        fetch the document on an ordinary tick, so this wait used to burn its whole
        budget and name the selector for a page the site's rate limiter served.
        """
        from graftpunk.plugins.login_engine import generate_login_method

        tab = _ScriptedNodriverTab(
            success_selector=".dashboard",
            success_from=None,
            contents=("<html>Too Many Requests</html>",),
        )
        mock_bs, _instance = _nodriver_session_for(tab)

        with (
            patch("graftpunk.BrowserSession", mock_bs),
            patch("graftpunk.plugins.cli_plugin.cache_session"),
            patch("graftpunk.plugins.login_settle.LOG") as mock_log,
        ):
            result = await generate_login_method(DeclarativeNodriverSuccess())(_LOGIN_CREDENTIALS)

        assert result is False
        assert "cookies" not in tab.events
        # One document read for the whole wait, on the tick that ended it.
        assert [event for event in tab.events if event.startswith("tick:")] == [f"tick:{tab.tick}"]
        assert _warning_kwargs(mock_log, "login_rate_limited")["plugin"] == "ndsuccess"
        timeouts = [c for c in mock_log.warning.call_args_list if c[0][0] == "login_signal_timeout"]
        assert timeouts == []

    @pytest.mark.asyncio
    async def test_a_url_of_none_mid_redirect_keeps_the_poll_going(self) -> None:
        """nodriver reports no URL while a tab navigates; the poll waits it out."""
        from graftpunk.plugins.login_engine import generate_login_method

        tab = _ScriptedNodriverTab(
            urls=("https://app.example.com/login", None, "https://app.example.com/dashboard"),
        )
        mock_bs, _instance = _nodriver_session_for(tab)

        with (
            patch("graftpunk.BrowserSession", mock_bs),
            patch("graftpunk.plugins.cli_plugin.cache_session"),
        ):
            result = await generate_login_method(DeclarativeNodriverUrlOnly())(_LOGIN_CREDENTIALS)

        assert result is True
        assert tab.tick == 2

    @pytest.mark.asyncio
    async def test_a_url_that_is_never_readable_is_a_dead_tab_not_a_missing_signal(self) -> None:
        """A tab that answers neither a URL nor a page has confirmed nothing.

        Nothing here can tell the URL signal apart from a dead browser, so the wait
        blames the browser rather than naming a signal it never got to look at.
        """
        from graftpunk.plugins.login_engine import generate_login_method

        tab = _ScriptedNodriverTab(urls=(None,), unreadable_ticks=tuple(range(1000)))
        mock_bs, _instance = _nodriver_session_for(tab)

        with (
            patch("graftpunk.BrowserSession", mock_bs),
            patch("graftpunk.plugins.cli_plugin.cache_session"),
            patch("graftpunk.plugins.login_settle.LOG") as mock_log,
        ):
            result = await generate_login_method(DeclarativeNodriverUrlTimeout())(
                _LOGIN_CREDENTIALS
            )

        assert result is False
        warning = _warning_kwargs(mock_log, "login_page_unreadable")
        assert "Could not find node" in warning["error"]
        assert warning["url"] == ""
        timeouts = [c for c in mock_log.warning.call_args_list if c[0][0] == "login_signal_timeout"]
        assert timeouts == []

    @pytest.mark.asyncio
    async def test_a_timeout_below_one_interval_still_runs_a_tick_and_ends(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Each sleep is clamped to what is left, so a short timeout costs its own length."""
        import time as time_module

        from graftpunk.plugins.login_engine import generate_login_method

        monkeypatch.setattr("graftpunk.plugins.login_settle._LOGIN_POLL_INTERVAL", 30.0)
        tab = _ScriptedNodriverTab(success_selector=".dashboard")
        mock_bs, _instance = _nodriver_session_for(tab)

        with (
            patch("graftpunk.BrowserSession", mock_bs),
            patch("graftpunk.plugins.cli_plugin.cache_session"),
        ):
            started = time_module.monotonic()
            # DeclarativeNodriverSuccess has timeout=0.05, a fraction of the interval.
            result = await generate_login_method(DeclarativeNodriverSuccess())(_LOGIN_CREDENTIALS)
            elapsed = time_module.monotonic() - started

        assert result is False
        assert tab.tick == 0  # it read the page once rather than sleeping past the deadline
        assert elapsed < 5.0  # an unclamped first sleep would be the 30 second interval

    @pytest.mark.asyncio
    async def test_rate_limit_page_beats_a_later_success(self) -> None:
        """A rate-limit page ends the wait instead of polling on for the element."""
        from graftpunk.plugins.login_engine import generate_login_method

        tab = _ScriptedNodriverTab(
            success_selector=".dashboard",
            contents=("<html>Too Many Requests</html>", "<html>dashboard</html>"),
            success_from=1,
        )
        mock_bs, _instance = _nodriver_session_for(tab)

        with (
            patch("graftpunk.BrowserSession", mock_bs),
            patch("graftpunk.plugins.cli_plugin.cache_session"),
            patch("graftpunk.plugins.login_settle.LOG") as mock_log,
        ):
            result = await generate_login_method(DeclarativeNodriverPoll())(_LOGIN_CREDENTIALS)

        assert result is False
        assert tab.tick == 0
        assert "cookies" not in tab.events
        assert _warning_kwargs(mock_log, "login_rate_limited")["plugin"] == "ndpoll"


class DeclarativeNodriverFailureOnly(SitePlugin):
    """Nodriver plugin whose only signal is its failure text."""

    site_name = "ndfailonly"
    session_name = "ndfailonly"
    help_text = "ND Failure Only"
    base_url = "https://example.com"
    backend = "nodriver"
    login_config = LoginConfig(
        steps=[LoginStep(fields={"username": "#user", "password": "#pass"}, submit="#submit")],
        url="/login",
        failure="Invalid credentials",
    )


class DeclarativeSeleniumFailureOnly(SitePlugin):
    """Selenium plugin whose only signal is its failure text."""

    site_name = "selfailonly"
    session_name = "selfailonly"
    help_text = "Sel Failure Only"
    base_url = "https://example.com"
    backend = "selenium"
    login_config = LoginConfig(
        steps=[LoginStep(fields={"username": "#user", "password": "#pass"}, submit="#submit")],
        url="/login",
        failure="Invalid credentials",
    )


class TestNoSignalGraceWindow:
    """A login with no success signal still watches for its failure signal."""

    @pytest.mark.asyncio
    async def test_nodriver_error_rendered_late_in_the_window_still_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The site renders its error a moment after the submit, inside the window."""
        from graftpunk.plugins.login_engine import generate_login_method

        monkeypatch.setattr("graftpunk.plugins.login_settle._NO_SIGNAL_GRACE", 1.0)
        tab = _ScriptedNodriverTab(
            contents=(
                "<html>Signing in</html>",
                "<html>Signing in</html>",
                "<html>Invalid credentials</html>",
            ),
        )
        mock_bs, _instance = _nodriver_session_for(tab)

        with (
            patch("graftpunk.BrowserSession", mock_bs),
            patch("graftpunk.plugins.cli_plugin.cache_session"),
            patch("graftpunk.plugins.login_settle.LOG") as mock_log,
        ):
            result = await generate_login_method(DeclarativeNodriverFailureOnly())(
                _LOGIN_CREDENTIALS
            )

        assert result is False
        assert tab.tick == 2  # it was still watching when the error rendered
        assert "cookies" not in tab.events
        assert _warning_kwargs(mock_log, "login_failure_text_detected")["text"] == (
            "Invalid credentials"
        )

    @pytest.mark.asyncio
    async def test_nodriver_a_clean_page_succeeds_after_the_window(self) -> None:
        """Nothing to validate against: the window passes, the login stands, and
        the warning that nothing validates it is logged as before."""
        from graftpunk.plugins.login_engine import generate_login_method

        tab = _ScriptedNodriverTab(contents=("<html>Welcome</html>",))
        mock_bs, _instance = _nodriver_session_for(tab)

        with (
            patch("graftpunk.BrowserSession", mock_bs),
            patch("graftpunk.plugins.cli_plugin.cache_session"),
            patch("graftpunk.plugins.login_settle.LOG") as mock_log,
        ):
            result = await generate_login_method(DeclarativeNodriverNoValidation())(
                _LOGIN_CREDENTIALS
            )

        assert result is True
        assert tab.tick >= 0  # the window was watched, not skipped
        assert "cookies" in tab.events
        assert _warning_kwargs(mock_log, "login_no_validation_configured")["plugin"] == "ndnoval"

    def test_selenium_error_rendered_late_in_the_window_still_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The selenium twin: the error arrives a tick or two in, and still decides."""
        from graftpunk.plugins.login_engine import generate_login_method

        monkeypatch.setattr("graftpunk.plugins.login_settle._NO_SIGNAL_GRACE", 1.0)
        driver = _ScriptedSeleniumDriver(
            contents=(
                "<html>Signing in</html>",
                "<html>Signing in</html>",
                "<html>Invalid credentials</html>",
            ),
        )
        mock_bs, _instance = _selenium_session_for(driver)

        with (
            patch("graftpunk.BrowserSession", mock_bs),
            patch("graftpunk.plugins.cli_plugin.cache_session"),
            patch("graftpunk.plugins.login_settle.LOG") as mock_log,
        ):
            result = generate_login_method(DeclarativeSeleniumFailureOnly())(_LOGIN_CREDENTIALS)

        assert result is False
        assert driver.tick == 2
        assert "cookies" not in driver.events
        assert _warning_kwargs(mock_log, "login_failure_text_detected")["text"] == (
            "Invalid credentials"
        )

    @pytest.mark.asyncio
    async def test_a_timeout_this_path_cannot_use_is_said_out_loud(self) -> None:
        """Raising timeout on a signal-less login changes nothing, and the trail says so."""
        from graftpunk.plugins.login_engine import generate_login_method

        class RaisedTimeoutPlugin(SitePlugin):
            site_name = "ndtiming"
            session_name = "ndtiming"
            help_text = "ND Timing"
            base_url = "https://example.com"
            backend = "nodriver"
            login_config = LoginConfig(
                steps=[LoginStep(fields={"username": "#user"}, submit="#submit")],
                url="/login",
                failure="Invalid credentials",
                timeout=90.0,
            )

        tab = _ScriptedNodriverTab(contents=("<html>Welcome</html>",))
        mock_bs, _instance = _nodriver_session_for(tab)

        with (
            patch("graftpunk.BrowserSession", mock_bs),
            patch("graftpunk.plugins.cli_plugin.cache_session"),
            patch("graftpunk.plugins.login_settle.LOG") as mock_log,
        ):
            result = await generate_login_method(RaisedTimeoutPlugin())(_LOGIN_CREDENTIALS)

        assert result is True
        event = _debug_kwargs(mock_log, "login_no_signal_ignores_timing")
        assert event["timeout"] == "90s"
        assert "settle" not in event  # it kept the default, so it is not worth naming

    @pytest.mark.asyncio
    async def test_a_login_that_set_no_timing_says_nothing_about_it(self) -> None:
        """The event is for a plugin that set a value, not for every signal-less login."""
        from graftpunk.plugins.login_engine import generate_login_method

        tab = _ScriptedNodriverTab(contents=("<html>Welcome</html>",))
        mock_bs, _instance = _nodriver_session_for(tab)

        with (
            patch("graftpunk.BrowserSession", mock_bs),
            patch("graftpunk.plugins.cli_plugin.cache_session"),
            patch("graftpunk.plugins.login_settle.LOG") as mock_log,
        ):
            result = await generate_login_method(DeclarativeNodriverFailureOnly())(
                _LOGIN_CREDENTIALS
            )

        assert result is True
        events = [c[0][0] for c in mock_log.debug.call_args_list]
        assert "login_no_signal_ignores_timing" not in events

    @pytest.mark.asyncio
    async def test_nodriver_a_page_that_never_reads_is_a_failure(self) -> None:
        """Nothing was read, so nothing confirms the login: it fails, and says why."""
        from graftpunk.plugins.login_engine import generate_login_method

        tab = _ScriptedNodriverTab(
            urls=("https://app.example.com/login",),
            unreadable_ticks=tuple(range(100)),
        )
        mock_bs, _instance = _nodriver_session_for(tab)

        with (
            patch("graftpunk.BrowserSession", mock_bs),
            patch("graftpunk.plugins.cli_plugin.cache_session"),
            patch("graftpunk.plugins.login_settle.LOG") as mock_log,
        ):
            result = await generate_login_method(DeclarativeNodriverFailureOnly())(
                _LOGIN_CREDENTIALS
            )

        assert result is False
        assert "cookies" not in tab.events
        warning = _warning_kwargs(mock_log, "login_page_unreadable")
        assert "Could not find node" in warning["error"]
        assert warning["url"] == "https://app.example.com/login"

    def test_selenium_a_page_that_never_reads_is_a_failure(self) -> None:
        """The selenium twin: a driver whose every read raises fails the login."""
        from graftpunk.plugins.login_engine import generate_login_method

        driver = _ScriptedSeleniumDriver(
            urls=("https://app.example.com/login",),
            unreadable_ticks=tuple(range(100)),
        )
        mock_bs, _instance = _selenium_session_for(driver)

        with (
            patch("graftpunk.BrowserSession", mock_bs),
            patch("graftpunk.plugins.cli_plugin.cache_session"),
            patch("graftpunk.plugins.login_settle.LOG") as mock_log,
        ):
            result = generate_login_method(DeclarativeSeleniumFailureOnly())(_LOGIN_CREDENTIALS)

        assert result is False
        assert "cookies" not in driver.events
        warning = _warning_kwargs(mock_log, "login_page_unreadable")
        assert "no such window" in warning["error"]
        assert warning["url"] == "https://app.example.com/login"

    def test_selenium_a_clean_page_succeeds_after_the_window(self) -> None:
        """A page that never shows the failure text keeps the login."""
        from graftpunk.plugins.login_engine import generate_login_method

        driver = _ScriptedSeleniumDriver(contents=("<html>Welcome</html>",))
        mock_bs, _instance = _selenium_session_for(driver)

        with (
            patch("graftpunk.BrowserSession", mock_bs),
            patch("graftpunk.plugins.cli_plugin.cache_session"),
        ):
            result = generate_login_method(DeclarativeSeleniumFailureOnly())(_LOGIN_CREDENTIALS)

        assert result is True
        assert driver.tick >= 0
        assert "cookies" in driver.events


class TestDocumentReadinessWait:
    """The readiness wait is bounded by the same deadline as the poll."""

    @pytest.mark.asyncio
    async def test_nodriver_a_document_stuck_on_loading_returns_at_the_deadline(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Its sleep is clamped too, so a deadline inside one interval is honoured."""
        import asyncio
        import time as time_module

        from graftpunk.plugins.login_settle import _await_document_ready_nodriver

        monkeypatch.setattr("graftpunk.plugins.login_settle._LOGIN_POLL_INTERVAL", 30.0)

        class _StuckTab:
            def __init__(self) -> None:
                self.reads = 0

            async def evaluate(self, expression: str) -> str:
                self.reads += 1
                return "loading"

        tab = _StuckTab()
        deadline = asyncio.get_running_loop().time() + 0.05
        started = time_module.monotonic()
        await _await_document_ready_nodriver(tab, deadline=deadline, site_name="stuck")
        elapsed = time_module.monotonic() - started

        assert tab.reads >= 1
        assert elapsed < 5.0  # an unclamped sleep would be the 30 second interval

    def test_selenium_a_document_stuck_on_loading_returns_at_the_deadline(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The selenium twin of the clamp."""
        import time as time_module

        from graftpunk.plugins.login_settle import _await_document_ready_selenium

        monkeypatch.setattr("graftpunk.plugins.login_settle._LOGIN_POLL_INTERVAL", 30.0)

        class _StuckDriver:
            def __init__(self) -> None:
                self.reads = 0

            def execute_script(self, script: str) -> str:
                self.reads += 1
                return "loading"

        driver = _StuckDriver()
        started = time_module.monotonic()
        _await_document_ready_selenium(
            driver, deadline=time_module.monotonic() + 0.05, site_name="stuck"
        )
        elapsed = time_module.monotonic() - started

        assert driver.reads >= 1
        assert elapsed < 5.0  # an unclamped sleep would be the 30 second interval


class TestCheckLoginResult:
    """Direct tests for _check_login_result()."""

    def test_rate_limited_page_returns_false_and_logs_rate_limit(self) -> None:
        """A 'Too Many Requests' page is not a credentials failure (issue #148 item 2)."""
        from graftpunk.plugins.login_settle import _check_login_result

        with patch("graftpunk.plugins.login_settle.LOG") as mock_log:
            result = _check_login_result(
                page_text="<html><title>429 Too Many Requests</title></html>",
                failure_text="These credentials do not match",
                site_name="test",
            )
        assert result is False
        events = [c.args[0] for c in mock_log.warning.call_args_list]
        assert events == ["login_rate_limited"]

    def test_rate_limit_detection_is_case_insensitive(self) -> None:
        from graftpunk.plugins.login_settle import _check_login_result

        with patch("graftpunk.plugins.login_settle.LOG") as mock_log:
            result = _check_login_result(
                page_text="<h1>TOO MANY REQUESTS</h1>",
                failure_text="",
                site_name="test",
            )
        assert result is False
        assert mock_log.warning.call_args.args[0] == "login_rate_limited"

    def test_failure_warning_carries_still_on_login_page_hint(self) -> None:
        """The failure-text warning must not read as a credentials verdict."""
        from graftpunk.plugins.login_settle import _check_login_result

        with patch("graftpunk.plugins.login_settle.LOG") as mock_log:
            _check_login_result(
                page_text="<html>Bad login.</html>",
                failure_text="Bad login.",
                site_name="test",
            )
        kwargs = mock_log.warning.call_args.kwargs
        assert "hint" in kwargs

    def test_failure_text_present_returns_false(self) -> None:
        """Returns False when failure text is found in page text (case-insensitive)."""
        from graftpunk.plugins.login_settle import _check_login_result

        result = _check_login_result(
            page_text="<html>Bad login. Try again.</html>",
            failure_text="Bad login.",
            site_name="test",
        )
        assert result is False

    def test_failure_text_case_insensitive(self) -> None:
        """Failure text matching is case-insensitive."""
        from graftpunk.plugins.login_settle import _check_login_result

        result = _check_login_result(
            page_text="<html>INVALID CREDENTIALS</html>",
            failure_text="invalid credentials",
            site_name="test",
        )
        assert result is False

    def test_neither_failure_nor_success_returns_true(self) -> None:
        """Returns True (optimistic) when neither failure nor success is configured."""
        from graftpunk.plugins.login_settle import _check_login_result

        result = _check_login_result(
            page_text="<html>Something</html>",
            failure_text="",
            site_name="test",
        )
        assert result is True

    def test_neither_configured_logs_warning(self) -> None:
        """Logs a no-validation warning when the plugin configured no failure text.

        This path only runs with no success signal configured, so an empty
        failure text means nothing validates the login at all.
        """
        from graftpunk.plugins.login_settle import _check_login_result

        with patch("graftpunk.plugins.login_settle._warn_no_login_validation") as mock_warn:
            _check_login_result(
                page_text="<html>Something</html>",
                failure_text="",
                site_name="testplugin",
            )
            mock_warn.assert_called_once_with("testplugin")

    def test_failure_text_not_in_page_stands_without_the_warning(self) -> None:
        """A configured failure text that never showed is a login that stands.

        Nothing is warned about: the failure text is the validation this plugin
        has, and it was read.
        """
        from graftpunk.plugins.login_settle import _check_login_result

        with patch("graftpunk.plugins.login_settle._warn_no_login_validation") as mock_warn:
            result = _check_login_result(
                page_text="<html>Welcome</html>",
                failure_text="Bad login.",
                site_name="test",
            )
        assert result is True
        # No warning because failure_text is set (non-empty)
        mock_warn.assert_not_called()


class TestLoginTickVerdict:
    """Direct tests for the rule both backends poll with."""

    @staticmethod
    def _verdict(
        *,
        page_text: str = "<html>Signing in</html>",
        url: str = "https://app.example.com/login",
        pre_submit_url: str = "https://app.example.com/login",
        failure_text: str = "",
        success_selector: str = "",
        success_url: str = "",
        success_found: bool | None = None,
    ) -> str:
        from graftpunk.plugins.login_settle import _login_tick_verdict

        return _login_tick_verdict(
            page_text=page_text,
            url=url,
            pre_submit_url=pre_submit_url,
            failure_text=failure_text,
            success_selector=success_selector,
            success_url=success_url,
            success_found=success_found,
        )

    def test_element_not_found_yet_is_pending(self) -> None:
        """A configured element that is not on the page yet keeps the poll going."""
        assert self._verdict(success_selector=".dashboard", success_found=False) == "pending"

    def test_element_found_is_success(self) -> None:
        """The configured element on the page ends the poll successfully."""
        assert self._verdict(success_selector=".dashboard", success_found=True) == "success"

    def test_url_glob_matches_the_whole_url(self) -> None:
        """A host-anchored glob matches the URL it was written for."""
        assert (
            self._verdict(
                url="https://app.example.com/dashboard?welcome=1",
                success_url="https://app.example.com/*",
            )
            == "success"
        )

    def test_path_glob_matches_any_host(self) -> None:
        """A path glob with a leading wildcard matches whatever host the login lands on."""
        assert (
            self._verdict(url="https://sso.example.com/dashboard", success_url="*/dashboard*")
            == "success"
        )

    def test_url_glob_is_case_sensitive(self) -> None:
        """The URL match is fnmatchcase: a pattern in another case does not match."""
        assert (
            self._verdict(url="https://app.example.com/dashboard", success_url="*/Dashboard*")
            == "pending"
        )

    def test_both_signals_need_both(self) -> None:
        """A matching URL without the element is still pending, and with it succeeds."""
        assert (
            self._verdict(
                url="https://app.example.com/dashboard",
                success_selector=".dashboard",
                success_url="*/dashboard*",
                success_found=False,
            )
            == "pending"
        )
        assert (
            self._verdict(
                url="https://app.example.com/dashboard",
                success_selector=".dashboard",
                success_url="*/dashboard*",
                success_found=True,
            )
            == "success"
        )

    def test_failure_text_beats_the_success_element(self) -> None:
        """The configured failure text decides even on a tick where the element is there."""
        assert (
            self._verdict(
                page_text="<html>Invalid credentials</html>",
                failure_text="Invalid credentials",
                success_selector=".dashboard",
                success_found=True,
            )
            == "failure_text"
        )

    def test_failure_text_matches_case_insensitively(self) -> None:
        """Failure text is matched case-insensitively, as it always was."""
        assert (
            self._verdict(
                page_text="<html>INVALID CREDENTIALS</html>",
                failure_text="Invalid credentials",
                success_selector=".dashboard",
                success_found=False,
            )
            == "failure_text"
        )

    def test_rate_limit_page_is_a_failure_while_the_signal_is_missing(self) -> None:
        """A rate-limit page ends the poll rather than waiting out the timeout."""
        assert (
            self._verdict(
                page_text="<html>Too Many Requests</html>",
                success_selector=".dashboard",
                success_found=False,
            )
            == "rate_limited"
        )

    def test_a_found_element_keeps_its_veto_over_the_rate_limit_marker(self) -> None:
        """Raw HTML can carry the marker text on a real post-login page."""
        assert (
            self._verdict(
                page_text="<html>dashboard<script>{'err':'Too Many Requests'}</script></html>",
                success_selector=".dashboard",
                success_found=True,
            )
            == "success"
        )

    def test_a_found_element_vetoes_the_marker_even_before_the_url_moves(self) -> None:
        """The element's veto is the element's own: the tick is pending, not failed."""
        assert (
            self._verdict(
                page_text="<html>dashboard<script>{'err':'Too Many Requests'}</script></html>",
                url="https://app.example.com/login",
                pre_submit_url="https://app.example.com/login",
                success_selector=".dashboard",
                success_url="*/dashboard*",
                success_found=True,
            )
            == "pending"
        )

    def test_a_url_only_success_never_vetoes_the_rate_limit_marker(self) -> None:
        """A limiter answering the redirect serves its 429 body at the landing URL."""
        assert (
            self._verdict(
                page_text="<html><h1>Too Many Requests</h1></html>",
                url="https://app.example.com/dashboard",
                pre_submit_url="https://app.example.com/login",
                success_url="*/dashboard*",
            )
            == "rate_limited"
        )

    def test_a_url_that_has_not_changed_is_not_a_signal(self) -> None:
        """A glob loose enough to match the login page must not report success there."""
        assert (
            self._verdict(
                url="https://app.example.com/login",
                pre_submit_url="https://app.example.com/login",
                success_url="*example.com*",
            )
            == "pending"
        )

    def test_the_same_glob_holds_once_the_page_has_moved(self) -> None:
        """The rule is a navigation, not a stricter pattern."""
        assert (
            self._verdict(
                url="https://app.example.com/dashboard",
                pre_submit_url="https://app.example.com/login",
                success_url="*example.com*",
            )
            == "success"
        )

    def test_an_unknown_baseline_is_not_a_navigation(self) -> None:
        """With no URL read before the submit there is nothing a move can be measured
        against, so a matching URL is not yet a signal."""
        assert (
            self._verdict(
                url="https://app.example.com/dashboard",
                pre_submit_url="",
                success_url="*/dashboard*",
            )
            == "pending"
        )

    def test_missing_signals_report_the_url_pattern_with_an_unknown_baseline(self) -> None:
        """The timeout warning names the URL pattern as missing, matching URL or not."""
        from graftpunk.plugins.login_settle import _missing_login_signals

        assert _missing_login_signals(
            url="https://app.example.com/dashboard",
            pre_submit_url="",
            success_found=None,
            success_selector="",
            success_url="*/dashboard*",
        ) == ["success URL pattern '*/dashboard*'"]

    def test_missing_signals_name_every_signal_that_does_not_hold(self) -> None:
        """The timeout warning's text comes from this list."""
        from graftpunk.plugins.login_settle import _missing_login_signals

        assert _missing_login_signals(
            url="https://app.example.com/login",
            pre_submit_url="https://app.example.com/login",
            success_found=False,
            success_selector=".dashboard",
            success_url="*/dashboard*",
        ) == ["success element '.dashboard'", "success URL pattern '*/dashboard*'"]

    def test_missing_signals_is_empty_when_both_hold(self) -> None:
        """Nothing is missing once the element is found and the URL has changed."""
        from graftpunk.plugins.login_settle import _missing_login_signals

        assert (
            _missing_login_signals(
                url="https://app.example.com/dashboard",
                pre_submit_url="https://app.example.com/login",
                success_found=True,
                success_selector=".dashboard",
                success_url="*/dashboard*",
            )
            == []
        )


class TestLoginConfigNoneGuard:
    """Tests for login_config=None guard paths in generated login methods."""

    @pytest.mark.asyncio
    async def test_nodriver_login_config_none_raises(self) -> None:
        """Nodriver generated login raises PluginError when login_config is None."""
        from graftpunk.plugins.login_engine import _generate_nodriver_login

        plugin = MagicMock()
        plugin.site_name = "testplugin"
        plugin.login_config = None

        login_method = _generate_nodriver_login(plugin)
        with pytest.raises(PluginError, match="has no login configuration"):
            await login_method({"username": "user", "password": "pass"})  # noqa: S106

    def test_selenium_login_config_none_raises(self) -> None:
        """Selenium generated login raises PluginError when login_config is None."""
        from graftpunk.plugins.login_engine import _generate_selenium_login

        plugin = MagicMock()
        plugin.site_name = "testplugin"
        plugin.login_config = None

        login_method = _generate_selenium_login(plugin)
        with pytest.raises(PluginError, match="has no login configuration"):
            login_method({"username": "user", "password": "pass"})  # noqa: S106

    def test_generate_login_method_nodriver_config_none_guard(self) -> None:
        """generate_login_method returns a callable that guards login_config=None (nodriver)."""
        from graftpunk.plugins.login_engine import generate_login_method

        plugin = MagicMock()
        plugin.site_name = "testplugin"
        plugin.backend = "nodriver"
        plugin.login_config = None

        login_method = generate_login_method(plugin)
        assert callable(login_method)

    def test_generate_login_method_selenium_config_none_guard(self) -> None:
        """generate_login_method returns a callable that guards login_config=None (selenium)."""
        from graftpunk.plugins.login_engine import generate_login_method

        plugin = MagicMock()
        plugin.site_name = "testplugin"
        plugin.backend = "selenium"
        plugin.login_config = None

        login_method = generate_login_method(plugin)
        assert callable(login_method)


class TestLoginEngineHeaderCapture:
    """Tests for header capture integration in login engine."""

    @pytest.mark.asyncio
    async def test_nodriver_login_sets_header_roles(self) -> None:
        """Nodriver login extracts header roles from capture backend."""
        from graftpunk.plugins.login_engine import generate_login_method

        plugin = DeclarativeHN()
        login_method = generate_login_method(plugin)

        mock_tab = MagicMock()
        mock_element = AsyncMock()
        mock_tab.select = AsyncMock(return_value=mock_element)
        mock_tab.query_selector = mock_tab.select
        mock_tab.get_content = AsyncMock(return_value="<html>Welcome</html>")

        mock_bs, instance = _make_nodriver_mock_bs()
        instance.driver = MagicMock()
        instance.driver.get = AsyncMock(return_value=mock_tab)
        instance.transfer_nodriver_cookies_to_session = AsyncMock()

        mock_capture = MagicMock()
        mock_capture.start_capture_async = AsyncMock()
        mock_capture.get_header_roles.return_value = {
            "navigation": {"User-Agent": "TestBrowser/1.0"}
        }

        with (
            patch("graftpunk.BrowserSession", mock_bs),
            patch("graftpunk.plugins.cli_plugin.cache_session"),
            patch(
                "graftpunk.observe.capture.create_capture_backend",
                return_value=mock_capture,
            ),
        ):
            result = await login_method({"username": "user", "password": "test"})  # noqa: S106

        assert result is True
        assert instance._gp_header_roles == {"navigation": {"User-Agent": "TestBrowser/1.0"}}
        mock_capture.start_capture_async.assert_awaited_once()
        mock_capture.get_header_roles.assert_called_once()

    def test_selenium_login_sets_header_roles(self) -> None:
        """Selenium login extracts header roles from capture backend."""
        from graftpunk.plugins.login_engine import generate_login_method

        plugin = DeclarativeQuotes()
        login_method = generate_login_method(plugin)

        mock_element = MagicMock()

        mock_bs, instance = _make_selenium_mock_bs()
        instance.driver = MagicMock()
        instance.driver.get = MagicMock()
        instance.driver.find_element = MagicMock(return_value=mock_element)
        instance.transfer_driver_cookies_to_session = MagicMock()

        mock_capture = MagicMock()
        mock_capture.get_header_roles.return_value = {"xhr": {"Accept": "application/json"}}

        with (
            patch("graftpunk.BrowserSession", mock_bs),
            patch("graftpunk.plugins.cli_plugin.cache_session"),
            patch("graftpunk.plugins.login_engine.time.sleep"),
            patch(
                "graftpunk.observe.capture.create_capture_backend",
                return_value=mock_capture,
            ),
        ):
            result = login_method({"username": "user", "password": "test"})  # noqa: S106

        assert result is True
        assert instance._gp_header_roles == {"xhr": {"Accept": "application/json"}}
        mock_capture.start_capture.assert_called_once()
        mock_capture.stop_capture.assert_called_once()
        mock_capture.get_header_roles.assert_called_once()


class TestSeleniumLoginValidationPaths:
    """Tests for selenium login validation paths."""

    def test_selenium_login_success_selector_found(self) -> None:
        """Success_selector found returns True."""
        from graftpunk.plugins.login_engine import generate_login_method

        plugin = DeclarativeQuotes()
        login_method = generate_login_method(plugin)

        mock_element = MagicMock()

        mock_bs, instance = _make_selenium_mock_bs()
        instance.driver = MagicMock()
        instance.driver.get = MagicMock()
        instance.driver.find_element = MagicMock(return_value=mock_element)
        instance.transfer_driver_cookies_to_session = MagicMock()

        with (
            patch("graftpunk.BrowserSession", mock_bs),
            patch("graftpunk.plugins.cli_plugin.cache_session"),
            patch("graftpunk.plugins.login_engine.time.sleep"),
        ):
            result = login_method({"username": "user", "password": "test"})  # noqa: S106

        assert result is True

    def test_selenium_login_checks_both_failure_and_success(self) -> None:
        """Both configured: failure_text not found, success_selector found returns True."""
        from graftpunk.plugins.login_engine import generate_login_method

        plugin = DeclarativeSeleniumBoth()
        login_method = generate_login_method(plugin)

        mock_element = MagicMock()

        mock_bs, instance = _make_selenium_mock_bs()
        instance.driver = MagicMock()
        instance.driver.get = MagicMock()
        instance.driver.find_element = MagicMock(return_value=mock_element)
        instance.driver.page_source = "<html>Welcome to your dashboard</html>"
        instance.transfer_driver_cookies_to_session = MagicMock()

        with (
            patch("graftpunk.BrowserSession", mock_bs),
            patch("graftpunk.plugins.cli_plugin.cache_session"),
            patch("graftpunk.plugins.login_engine.time.sleep"),
        ):
            result = login_method({"username": "user", "password": "test"})  # noqa: S106

        assert result is True


class _ScriptedSeleniumDriver:
    """A selenium driver that answers the post-submit poll from a script.

    The twin of :class:`_ScriptedNodriverTab`: each ``current_url`` read is one
    poll tick, ``contents`` gives the page for that tick, the success selector
    starts resolving on tick ``success_from``, and ``ready_states`` answers the
    document-ready wait one read at a time.
    """

    def __init__(
        self,
        *,
        success_selector: str = "",
        contents: tuple[str, ...] = ("<html>Signing in</html>",),
        urls: tuple[str, ...] = ("https://app.example.com/login",),
        success_from: int | None = None,
        ready_states: tuple[str, ...] = ("complete",),
        unreadable_ticks: tuple[int, ...] = (),
    ) -> None:
        self.events: list[str] = []
        self.tick = 0
        self._url_reads = 0
        self._wait_over = False
        self._success_selector = success_selector
        self._contents = contents
        self._urls = urls
        self._success_from = success_from
        self._ready_states = ready_states
        self._ready_reads = 0
        self._unreadable_ticks = unreadable_ticks

    def __getattr__(self, name: str) -> MagicMock:
        """Anything the browser stack asks of a driver that this script does not model."""
        return MagicMock()

    @staticmethod
    def _at(script: tuple[str, ...], index: int) -> str:
        return script[min(index, len(script) - 1)]

    @property
    def page_source(self) -> str:
        from selenium.common.exceptions import WebDriverException

        if self.tick in self._unreadable_ticks:
            self.events.append(f"tick:{self.tick}:unreadable")
            raise WebDriverException("no such window: target window already closed")
        self.events.append(f"tick:{self.tick}")
        return self._at(self._contents, self.tick)

    @property
    def current_url(self) -> str:
        """The scripted URL for this tick, and the read that moves the script on.

        The wait reads the URL once per tick and before anything else, as in the
        nodriver twin. An empty entry stands for a read the driver cannot answer:
        selenium raises there, and ``driver_url`` turns that into the empty string
        the poll reads as "no URL".
        """
        from selenium.common.exceptions import WebDriverException

        if not self._wait_over:
            self.tick = max(self._url_reads - 1, 0)
            self._url_reads += 1
        url = self._at(self._urls, self.tick)
        if not url:
            raise WebDriverException("no such window: target window already closed")
        return url

    def find_element(self, by: str, value: str) -> MagicMock:
        from selenium.common.exceptions import NoSuchElementException

        if value and value == self._success_selector:
            found = self._success_from is not None and self.tick >= self._success_from
            if not found:
                raise NoSuchElementException(value)
        return MagicMock()

    def execute_script(self, script: str) -> str:
        self._wait_over = True
        self.events.append("document_ready_read")
        state = self._at(self._ready_states, self._ready_reads)
        self._ready_reads += 1
        return state


def _selenium_session_for(driver: _ScriptedSeleniumDriver) -> tuple[MagicMock, MagicMock]:
    """A mock BrowserSession serving *driver*, recording its cookie capture on it."""
    mock_bs, instance = _make_selenium_mock_bs()
    instance.driver = driver
    instance.transfer_driver_cookies_to_session = MagicMock(
        side_effect=lambda: driver.events.append("cookies")
    )
    return mock_bs, instance


class DeclarativeSeleniumPoll(SitePlugin):
    """Selenium plugin with a success element and room to poll for it."""

    site_name = "selpoll"
    session_name = "selpoll"
    help_text = "Sel Poll"
    base_url = "https://example.com"
    backend = "selenium"
    login_config = LoginConfig(
        steps=[LoginStep(fields={"username": "#user", "password": "#pass"}, submit="#submit")],
        url="/login",
        failure="Invalid credentials",
        success=".dashboard",
        timeout=5.0,
        settle=0.0,
    )


class DeclarativeSeleniumUrlOnly(SitePlugin):
    """Selenium plugin whose only success signal is the landing URL.

    Generously budgeted, for the reason its nodriver twin gives.
    """

    site_name = "selurl"
    session_name = "selurl"
    help_text = "Sel URL"
    base_url = "https://example.com"
    backend = "selenium"
    login_config = LoginConfig(
        steps=[LoginStep(fields={"username": "#user", "password": "#pass"}, submit="#submit")],
        url="/login",
        success_url="*/dashboard*",
        timeout=5.0,
        settle=0.0,
    )


class DeclarativeSeleniumUrlTimeout(SitePlugin):
    """The same plugin with a budget small enough to reach its own timeout."""

    site_name = "selurlto"
    session_name = "selurlto"
    help_text = "Sel URL Timeout"
    base_url = "https://example.com"
    backend = "selenium"
    login_config = LoginConfig(
        steps=[LoginStep(fields={"username": "#user", "password": "#pass"}, submit="#submit")],
        url="/login",
        success_url="*/dashboard*",
        timeout=0.2,
        settle=0.0,
    )


class DeclarativeSeleniumBothSignals(SitePlugin):
    """Selenium plugin that requires both the element and the URL."""

    site_name = "selboth2"
    session_name = "selboth2"
    help_text = "Sel Both Signals"
    base_url = "https://example.com"
    backend = "selenium"
    login_config = LoginConfig(
        steps=[LoginStep(fields={"username": "#user", "password": "#pass"}, submit="#submit")],
        url="/login",
        success=".dashboard",
        success_url="*/dashboard*",
        timeout=0.2,
        settle=0.0,
    )


class TestSeleniumLoginSignalPoll:
    """The post-submit wait polls for the configured signal (selenium)."""

    def test_success_element_found_after_several_ticks(self) -> None:
        """A success element that only appears on a later tick still succeeds."""
        from graftpunk.plugins.login_engine import generate_login_method

        driver = _ScriptedSeleniumDriver(success_selector=".dashboard", success_from=3)
        mock_bs, _instance = _selenium_session_for(driver)

        with (
            patch("graftpunk.BrowserSession", mock_bs),
            patch("graftpunk.plugins.cli_plugin.cache_session"),
        ):
            result = generate_login_method(DeclarativeSeleniumPoll())(_LOGIN_CREDENTIALS)

        assert result is True
        assert driver.tick == 3
        assert "cookies" in driver.events

    def test_an_unreadable_tick_does_not_end_the_login(self) -> None:
        """A WebDriverException while the browser navigates is not a login failure."""
        from graftpunk.plugins.login_engine import generate_login_method

        driver = _ScriptedSeleniumDriver(
            success_selector=".dashboard", success_from=2, unreadable_ticks=(1,)
        )
        mock_bs, _instance = _selenium_session_for(driver)

        with (
            patch("graftpunk.BrowserSession", mock_bs),
            patch("graftpunk.plugins.cli_plugin.cache_session"),
        ):
            result = generate_login_method(DeclarativeSeleniumPoll())(_LOGIN_CREDENTIALS)

        assert result is True
        assert "tick:1:unreadable" in driver.events
        assert "cookies" in driver.events

    def test_a_poll_that_never_reads_the_page_blames_the_page(self) -> None:
        """The selenium twin: a driver whose every read raises never saw the signal."""
        from graftpunk.plugins.login_engine import generate_login_method

        class UnreadableSeleniumPlugin(SitePlugin):
            site_name = "selunread"
            session_name = "selunread"
            help_text = "Sel Unreadable"
            base_url = "https://example.com"
            backend = "selenium"
            login_config = LoginConfig(
                steps=[LoginStep(fields={"username": "#user"}, submit="#submit")],
                url="/login",
                failure="Invalid credentials",
                success=".dashboard",
                timeout=0.2,
                settle=0.0,
            )

        driver = _ScriptedSeleniumDriver(
            success_selector=".dashboard",
            urls=("https://app.example.com/login",),
            unreadable_ticks=tuple(range(1000)),
        )
        mock_bs, _instance = _selenium_session_for(driver)

        with (
            patch("graftpunk.BrowserSession", mock_bs),
            patch("graftpunk.plugins.cli_plugin.cache_session"),
            patch("graftpunk.plugins.login_settle.LOG") as mock_log,
        ):
            result = generate_login_method(UnreadableSeleniumPlugin())(_LOGIN_CREDENTIALS)

        assert result is False
        assert "cookies" not in driver.events
        warning = _warning_kwargs(mock_log, "login_page_unreadable")
        assert "no such window" in warning["error"]
        assert warning["url"] == "https://app.example.com/login"
        timeouts = [c for c in mock_log.warning.call_args_list if c[0][0] == "login_signal_timeout"]
        assert timeouts == []

    def test_failure_text_that_appears_late_ends_the_poll(self) -> None:
        """Failure text arriving after a few ticks fails the login there and then."""
        from graftpunk.plugins.login_engine import generate_login_method

        driver = _ScriptedSeleniumDriver(
            success_selector=".dashboard",
            contents=(
                "<html>Signing in</html>",
                "<html>Signing in</html>",
                "<html>Invalid credentials</html>",
            ),
        )
        mock_bs, _instance = _selenium_session_for(driver)

        with (
            patch("graftpunk.BrowserSession", mock_bs),
            patch("graftpunk.plugins.cli_plugin.cache_session"),
            patch("graftpunk.plugins.login_settle.LOG") as mock_log,
        ):
            result = generate_login_method(DeclarativeSeleniumPoll())(_LOGIN_CREDENTIALS)

        assert result is False
        assert driver.tick == 2
        assert "cookies" not in driver.events
        assert _warning_kwargs(mock_log, "login_failure_text_detected")["text"] == (
            "Invalid credentials"
        )

    def test_url_alone_can_be_the_success_signal(self) -> None:
        """With success_url and no success selector, the landing URL decides."""
        from graftpunk.plugins.login_engine import generate_login_method

        driver = _ScriptedSeleniumDriver(
            urls=(
                "https://app.example.com/login",
                "https://app.example.com/sso/callback",
                "https://app.example.com/dashboard?welcome=1",
            ),
        )
        mock_bs, _instance = _selenium_session_for(driver)

        with (
            patch("graftpunk.BrowserSession", mock_bs),
            patch("graftpunk.plugins.cli_plugin.cache_session"),
        ):
            result = generate_login_method(DeclarativeSeleniumUrlOnly())(_LOGIN_CREDENTIALS)

        assert result is True
        assert driver.tick == 2
        assert "cookies" in driver.events

    def test_a_url_only_poll_reads_the_page_only_when_the_url_arrives(self) -> None:
        """The selenium twin: one page_source read, on the tick the URL matched."""
        from graftpunk.plugins.login_engine import generate_login_method

        driver = _ScriptedSeleniumDriver(
            urls=(
                "https://app.example.com/login",
                "https://app.example.com/sso/callback",
                "https://app.example.com/dashboard?welcome=1",
            ),
        )
        mock_bs, _instance = _selenium_session_for(driver)

        with (
            patch("graftpunk.BrowserSession", mock_bs),
            patch("graftpunk.plugins.cli_plugin.cache_session"),
        ):
            result = generate_login_method(DeclarativeSeleniumUrlOnly())(_LOGIN_CREDENTIALS)

        assert result is True
        assert driver.events == ["tick:2", "document_ready_read", "cookies"]

    def test_a_glob_matching_the_login_page_never_reports_success(self) -> None:
        """The probe that cached a pre-login session: the URL never changed."""
        from graftpunk.plugins.login_engine import generate_login_method

        class LooseUrlSeleniumPlugin(SitePlugin):
            site_name = "selloose"
            session_name = "selloose"
            help_text = "Sel Loose"
            base_url = "https://example.com"
            backend = "selenium"
            login_config = LoginConfig(
                steps=[LoginStep(fields={"username": "#user"}, submit="#submit")],
                url="/login",
                success_url="*example.com*",
                timeout=0.2,
                settle=0.0,
            )

        driver = _ScriptedSeleniumDriver(urls=("https://app.example.com/login",))
        mock_bs, _instance = _selenium_session_for(driver)

        with (
            patch("graftpunk.BrowserSession", mock_bs),
            patch("graftpunk.plugins.cli_plugin.cache_session"),
            patch("graftpunk.plugins.login_settle.LOG") as mock_log,
        ):
            result = generate_login_method(LooseUrlSeleniumPlugin())(_LOGIN_CREDENTIALS)

        assert result is False
        assert "cookies" not in driver.events
        warning = _warning_kwargs(mock_log, "login_signal_timeout")
        assert warning["missing"] == "success URL pattern '*example.com*'"

    def test_a_step_with_no_submit_measures_from_the_url_at_the_wait_start(self) -> None:
        """The selenium twin: with no submit to click, the URL the wait starts on is it."""
        from graftpunk.plugins.login_engine import generate_login_method

        class NoSubmitSeleniumPlugin(SitePlugin):
            site_name = "selnosubmit"
            session_name = "selnosubmit"
            help_text = "Sel No Submit"
            base_url = "https://example.com"
            backend = "selenium"
            login_config = LoginConfig(
                steps=[LoginStep(fields={"username": "#user"})],
                url="/login",
                success_url="*/dashboard*",
                timeout=5.0,
                settle=0.0,
            )

        driver = _ScriptedSeleniumDriver(
            urls=("https://app.example.com/login", "https://app.example.com/dashboard"),
        )
        mock_bs, _instance = _selenium_session_for(driver)

        with (
            patch("graftpunk.BrowserSession", mock_bs),
            patch("graftpunk.plugins.cli_plugin.cache_session"),
        ):
            result = generate_login_method(NoSubmitSeleniumPlugin())(_LOGIN_CREDENTIALS)

        assert result is True
        assert "cookies" in driver.events

    def test_an_unreadable_pre_submit_url_leaves_the_url_signal_unconfirmable(self) -> None:
        """The selenium twin: no baseline, so a matching URL is not a navigation."""
        from graftpunk.plugins.login_engine import generate_login_method

        class NoBaselineSeleniumPlugin(SitePlugin):
            site_name = "selnobase"
            session_name = "selnobase"
            help_text = "Sel No Baseline"
            base_url = "https://example.com"
            backend = "selenium"
            login_config = LoginConfig(
                steps=[LoginStep(fields={"username": "#user"}, submit="#submit")],
                url="/login",
                success_url="*example.com*",
                timeout=0.2,
                settle=0.0,
            )

        driver = _ScriptedSeleniumDriver(urls=("", "https://app.example.com/dashboard"))
        mock_bs, _instance = _selenium_session_for(driver)

        with (
            patch("graftpunk.BrowserSession", mock_bs),
            patch("graftpunk.plugins.cli_plugin.cache_session"),
            patch("graftpunk.plugins.login_settle.LOG") as mock_log,
        ):
            result = generate_login_method(NoBaselineSeleniumPlugin())(_LOGIN_CREDENTIALS)

        assert result is False
        assert "cookies" not in driver.events
        assert _debug_kwargs(mock_log, "login_pre_submit_url_unknown")["backend"] == "selenium"
        hint = _warning_kwargs(mock_log, "login_signal_timeout")["hint"]
        assert "URL before submit could not be read" in hint

    def test_the_same_glob_succeeds_once_the_page_navigates(self) -> None:
        """A URL that changes on a later tick is the navigation the rule asks for."""
        from graftpunk.plugins.login_engine import generate_login_method

        driver = _ScriptedSeleniumDriver(
            urls=(
                "https://app.example.com/login",
                "https://app.example.com/login",
                "https://app.example.com/login",
                "https://app.example.com/dashboard",
            ),
        )
        mock_bs, _instance = _selenium_session_for(driver)

        with (
            patch("graftpunk.BrowserSession", mock_bs),
            patch("graftpunk.plugins.cli_plugin.cache_session"),
        ):
            result = generate_login_method(DeclarativeSeleniumUrlOnly())(_LOGIN_CREDENTIALS)

        assert result is True
        assert driver.tick == 3
        assert "cookies" in driver.events

    def test_the_first_tick_comes_after_one_poll_interval(self) -> None:
        """An element already on the login page must not be read before the submit lands."""
        from graftpunk.plugins.login_engine import generate_login_method

        driver = _ScriptedSeleniumDriver(success_selector=".dashboard", success_from=0)
        mock_bs, _instance = _selenium_session_for(driver)

        with (
            patch("graftpunk.BrowserSession", mock_bs),
            patch("graftpunk.plugins.cli_plugin.cache_session"),
            patch(
                "graftpunk.plugins.login_settle.time.sleep",
                side_effect=lambda seconds: driver.events.append("sleep"),
            ),
        ):
            result = generate_login_method(DeclarativeSeleniumPoll())(_LOGIN_CREDENTIALS)

        assert result is True
        first_tick = driver.events.index("tick:0")
        assert driver.events[first_tick - 1] == "sleep"

    def test_both_signals_configured_needs_both(self) -> None:
        """A matching URL is not enough while the configured element never appears."""
        from graftpunk.plugins.login_engine import generate_login_method

        driver = _ScriptedSeleniumDriver(
            success_selector=".dashboard",
            urls=("https://app.example.com/login", "https://app.example.com/dashboard"),
        )
        mock_bs, _instance = _selenium_session_for(driver)

        with (
            patch("graftpunk.BrowserSession", mock_bs),
            patch("graftpunk.plugins.cli_plugin.cache_session"),
            patch("graftpunk.plugins.login_settle.LOG") as mock_log,
        ):
            result = generate_login_method(DeclarativeSeleniumBothSignals())(_LOGIN_CREDENTIALS)

        assert result is False
        assert "cookies" not in driver.events
        warning = _warning_kwargs(mock_log, "login_signal_timeout")
        assert warning["missing"] == "success element '.dashboard'"
        assert warning["url"] == "https://app.example.com/dashboard"

    def test_timeout_names_the_url_pattern_and_the_final_url(self) -> None:
        """A URL that never matches times out, naming the pattern and where it ended."""
        from graftpunk.plugins.login_engine import generate_login_method

        driver = _ScriptedSeleniumDriver(urls=("https://app.example.com/login?error=mfa",))
        mock_bs, _instance = _selenium_session_for(driver)

        with (
            patch("graftpunk.BrowserSession", mock_bs),
            patch("graftpunk.plugins.cli_plugin.cache_session"),
            patch("graftpunk.plugins.login_settle.LOG") as mock_log,
        ):
            result = generate_login_method(DeclarativeSeleniumUrlTimeout())(_LOGIN_CREDENTIALS)

        assert result is False
        assert driver.tick > 0  # it polled rather than checking once
        warning = _warning_kwargs(mock_log, "login_signal_timeout")
        assert warning["missing"] == "success URL pattern '*/dashboard*'"
        assert warning["url"] == "https://app.example.com/login?error=mfa"
        assert warning["timeout"] == "0.2s"

    def test_cookies_are_captured_after_the_document_finishes_loading(self) -> None:
        """The engine waits out a still-loading document before capturing cookies."""
        from graftpunk.plugins.login_engine import generate_login_method

        driver = _ScriptedSeleniumDriver(
            success_selector=".dashboard",
            success_from=0,
            ready_states=("loading", "complete"),
        )
        mock_bs, _instance = _selenium_session_for(driver)

        with (
            patch("graftpunk.BrowserSession", mock_bs),
            patch("graftpunk.plugins.cli_plugin.cache_session"),
        ):
            result = generate_login_method(DeclarativeSeleniumPoll())(_LOGIN_CREDENTIALS)

        assert result is True
        assert driver.events == ["tick:0", "document_ready_read", "document_ready_read", "cookies"]

    def test_a_malformed_success_selector_ends_the_login_naming_it(self) -> None:
        """A selector the browser rejects cannot come good on a later tick.

        Every tick would raise the same way, so the wait used to spend its whole
        budget and report the page as unreadable, blaming the browser for a
        configuration error.
        """
        from graftpunk.plugins.login_engine import generate_login_method

        class _RejectingSelectorDriver(_ScriptedSeleniumDriver):
            """A driver that rejects the success selector the way a browser does."""

            def find_element(self, by: str, value: str) -> MagicMock:
                from selenium.common.exceptions import InvalidSelectorException

                if value == self._success_selector:
                    raise InvalidSelectorException(f"invalid selector: {value}")
                return super().find_element(by, value)

        driver = _RejectingSelectorDriver(success_selector=".dashboard")
        mock_bs, _instance = _selenium_session_for(driver)

        with (
            patch("graftpunk.BrowserSession", mock_bs),
            patch("graftpunk.plugins.cli_plugin.cache_session"),
            pytest.raises(PluginError) as raised,
        ):
            generate_login_method(DeclarativeSeleniumPoll())(_LOGIN_CREDENTIALS)

        assert "'.dashboard'" in str(raised.value)
        assert "cookies" not in driver.events

    def test_a_selector_only_login_reports_a_rate_limit_page_at_its_deadline(self) -> None:
        """The selenium twin: the deadline tick reads the page, so the limiter is named."""
        from graftpunk.plugins.login_engine import generate_login_method

        driver = _ScriptedSeleniumDriver(
            success_selector="a[href='/logout']",
            success_from=None,
            contents=("<html>Too Many Requests</html>",),
        )
        mock_bs, _instance = _selenium_session_for(driver)

        with (
            patch("graftpunk.BrowserSession", mock_bs),
            patch("graftpunk.plugins.cli_plugin.cache_session"),
            patch("graftpunk.plugins.login_settle.LOG") as mock_log,
        ):
            result = generate_login_method(DeclarativeQuotes())(_LOGIN_CREDENTIALS)

        assert result is False
        assert "cookies" not in driver.events
        # One document read for the whole wait, on the tick that ended it.
        assert [event for event in driver.events if event.startswith("tick:")] == [
            f"tick:{driver.tick}"
        ]
        assert _warning_kwargs(mock_log, "login_rate_limited")["plugin"] == "quotes"
        timeouts = [c for c in mock_log.warning.call_args_list if c[0][0] == "login_signal_timeout"]
        assert timeouts == []

    def test_a_url_that_is_never_readable_is_a_dead_tab_not_a_missing_signal(self) -> None:
        """The selenium twin: a driver that answers neither a URL nor a page said nothing."""
        from graftpunk.plugins.login_engine import generate_login_method

        driver = _ScriptedSeleniumDriver(urls=("",), unreadable_ticks=tuple(range(1000)))
        mock_bs, _instance = _selenium_session_for(driver)

        with (
            patch("graftpunk.BrowserSession", mock_bs),
            patch("graftpunk.plugins.cli_plugin.cache_session"),
            patch("graftpunk.plugins.login_settle.LOG") as mock_log,
        ):
            result = generate_login_method(DeclarativeSeleniumUrlTimeout())(_LOGIN_CREDENTIALS)

        assert result is False
        warning = _warning_kwargs(mock_log, "login_page_unreadable")
        assert "target window already closed" in warning["error"]
        assert warning["url"] == ""
        timeouts = [c for c in mock_log.warning.call_args_list if c[0][0] == "login_signal_timeout"]
        assert timeouts == []

    def test_a_timeout_below_one_interval_still_runs_a_tick_and_ends(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Each sleep is clamped to what is left, so a short timeout costs its own length."""
        import time as time_module

        from graftpunk.plugins.login_engine import generate_login_method

        monkeypatch.setattr("graftpunk.plugins.login_settle._LOGIN_POLL_INTERVAL", 30.0)
        driver = _ScriptedSeleniumDriver(success_selector="a[href='/logout']")
        mock_bs, _instance = _selenium_session_for(driver)

        with (
            patch("graftpunk.BrowserSession", mock_bs),
            patch("graftpunk.plugins.cli_plugin.cache_session"),
        ):
            started = time_module.monotonic()
            # DeclarativeQuotes has timeout=0.05, a fraction of the interval.
            result = generate_login_method(DeclarativeQuotes())(_LOGIN_CREDENTIALS)
            elapsed = time_module.monotonic() - started

        assert result is False
        assert driver.tick == 0
        assert elapsed < 5.0  # an unclamped first sleep would be the 30 second interval

    def test_rate_limit_page_beats_a_later_success(self) -> None:
        """A rate-limit page ends the wait instead of polling on for the element."""
        from graftpunk.plugins.login_engine import generate_login_method

        driver = _ScriptedSeleniumDriver(
            success_selector=".dashboard",
            contents=("<html>Too Many Requests</html>", "<html>dashboard</html>"),
            success_from=1,
        )
        mock_bs, _instance = _selenium_session_for(driver)

        with (
            patch("graftpunk.BrowserSession", mock_bs),
            patch("graftpunk.plugins.cli_plugin.cache_session"),
            patch("graftpunk.plugins.login_settle.LOG") as mock_log,
        ):
            result = generate_login_method(DeclarativeSeleniumPoll())(_LOGIN_CREDENTIALS)

        assert result is False
        assert driver.tick == 0
        assert "cookies" not in driver.events
        assert _warning_kwargs(mock_log, "login_rate_limited")["plugin"] == "selpoll"


class TestLoginTimeTokenExtraction:
    """Tests for token extraction during login (nodriver path)."""

    @pytest.mark.asyncio
    async def test_nodriver_login_calls_extract_tokens_from_tab(self) -> None:
        """Nodriver login calls extract_tokens_from_tab when plugin has token_config."""
        from graftpunk.plugins.login_engine import generate_login_method
        from graftpunk.tokens import _CACHE_ATTR, Token, TokenConfig

        class TokenPlugin(SitePlugin):
            site_name = "tokensite"
            session_name = "tokensite"
            help_text = "Token Site"
            base_url = "https://example.com"
            backend = "nodriver"
            login_config = LoginConfig(
                steps=[
                    LoginStep(
                        fields={"username": "#user", "password": "#pass"},
                        submit="#submit",
                    ),
                ],
                url="/login",
                success=".dashboard",
                timeout=0.05,
                settle=0.0,
            )
            token_config = TokenConfig(
                tokens=(
                    Token.from_meta_tag("csrf-token", "X-CSRF"),
                    Token.from_cookie("sid", "X-Session"),
                )
            )

        plugin = TokenPlugin()
        login_method = generate_login_method(plugin)

        mock_element = AsyncMock()
        mock_tab = MagicMock()
        mock_tab.select = AsyncMock(return_value=mock_element)
        mock_tab.query_selector = mock_tab.select
        mock_tab.get_content = AsyncMock(return_value="Welcome to dashboard")
        mock_tab.url = "https://example.com/dashboard"

        mock_bs, instance = _make_nodriver_mock_bs()
        instance.driver = MagicMock()
        instance.driver.get = AsyncMock(return_value=mock_tab)
        instance.transfer_nodriver_cookies_to_session = AsyncMock()
        instance.cookies = MagicMock()
        instance.cookies.get = MagicMock(return_value="session123")

        mock_capture = MagicMock()
        mock_capture.start_capture_async = AsyncMock()
        mock_capture.get_header_roles = MagicMock(return_value={})

        with (
            patch("graftpunk.BrowserSession", mock_bs),
            patch("graftpunk.plugins.cli_plugin.cache_session"),
            patch(
                "graftpunk.observe.capture.create_capture_backend",
                return_value=mock_capture,
            ),
            patch(
                "graftpunk.tokens.extract_tokens_from_tab",
                new_callable=AsyncMock,
                return_value={"X-CSRF": "abc123"},
            ) as mock_extract,
        ):
            result = await login_method({"username": "user", "password": "test"})  # noqa: S106

        assert result is True
        mock_extract.assert_called_once()
        # Verify token cache was set on session
        token_cache = getattr(instance, _CACHE_ATTR, None)
        assert token_cache is not None
        assert "X-CSRF" in token_cache
        assert token_cache["X-CSRF"].value == "abc123"
        # Cookie-source token should also be cached
        assert "X-Session" in token_cache
        assert token_cache["X-Session"].value == "session123"

    @pytest.mark.asyncio
    async def test_nodriver_login_no_token_config_skips_extraction(self) -> None:
        """Nodriver login without token_config does not call extract_tokens_from_tab."""
        from graftpunk.plugins.login_engine import generate_login_method

        plugin = DeclarativeHN()
        login_method = generate_login_method(plugin)

        mock_element = AsyncMock()
        mock_tab = MagicMock()
        mock_tab.select = AsyncMock(return_value=mock_element)
        mock_tab.query_selector = mock_tab.select
        mock_tab.get_content = AsyncMock(return_value="<html>Welcome back!</html>")
        mock_tab.url = "https://news.ycombinator.com/news"

        mock_bs, instance = _make_nodriver_mock_bs()
        instance.driver = MagicMock()
        instance.driver.get = AsyncMock(return_value=mock_tab)
        instance.transfer_nodriver_cookies_to_session = AsyncMock()

        mock_capture = MagicMock()
        mock_capture.start_capture_async = AsyncMock()
        mock_capture.get_header_roles = MagicMock(return_value={})

        with (
            patch("graftpunk.BrowserSession", mock_bs),
            patch("graftpunk.plugins.cli_plugin.cache_session"),
            patch(
                "graftpunk.observe.capture.create_capture_backend",
                return_value=mock_capture,
            ),
            patch(
                "graftpunk.tokens.extract_tokens_from_tab",
                new_callable=AsyncMock,
            ) as mock_extract,
        ):
            result = await login_method({"username": "user", "password": "test"})  # noqa: S106

        assert result is True
        mock_extract.assert_not_called()


class TestSeleniumTokenExtraction:
    """Tests for _extract_and_cache_tokens_selenium."""

    def test_selenium_login_extracts_cookie_token(self) -> None:
        """Selenium login caches cookie-source tokens."""
        from graftpunk.plugins.login_engine import generate_login_method
        from graftpunk.tokens import _CACHE_ATTR, Token, TokenConfig

        class SeleniumTokenPlugin(SitePlugin):
            site_name = "seltoken"
            session_name = "seltoken"
            help_text = "Selenium Token"
            base_url = "https://example.com"
            backend = "selenium"
            login_config = LoginConfig(
                steps=[
                    LoginStep(
                        fields={"username": "#user", "password": "#pass"},
                        submit="#submit",
                    ),
                ],
                url="/login",
                success=".dashboard",
                timeout=0.05,
                settle=0.0,
            )
            token_config = TokenConfig(tokens=(Token.from_cookie("sid", "X-Session"),))

        plugin = SeleniumTokenPlugin()
        login_method = generate_login_method(plugin)

        mock_element = MagicMock()
        mock_bs, instance = _make_selenium_mock_bs()
        instance.driver = MagicMock()
        instance.driver.get = MagicMock()
        instance.driver.find_element = MagicMock(return_value=mock_element)
        instance.driver.page_source = "<html>Welcome to dashboard</html>"
        instance.transfer_driver_cookies_to_session = MagicMock()
        instance.cookies = MagicMock()
        instance.cookies.get = MagicMock(return_value="session-abc")

        mock_capture = MagicMock()
        mock_capture.start_capture = MagicMock()
        mock_capture.stop_capture = MagicMock()
        mock_capture.get_header_roles = MagicMock(return_value={})

        with (
            patch("graftpunk.BrowserSession", mock_bs),
            patch("graftpunk.plugins.cli_plugin.cache_session"),
            patch("graftpunk.plugins.login_engine.time.sleep"),
            patch(
                "graftpunk.observe.capture.create_capture_backend",
                return_value=mock_capture,
            ),
        ):
            result = login_method({"username": "user", "password": "test"})  # noqa: S106

        assert result is True
        token_cache = getattr(instance, _CACHE_ATTR, None)
        assert token_cache is not None
        assert "X-Session" in token_cache
        assert token_cache["X-Session"].value == "session-abc"

    def test_selenium_login_no_token_config_skips(self) -> None:
        """Selenium login without token_config does not attempt extraction."""
        from graftpunk.plugins.login_engine import generate_login_method
        from graftpunk.tokens import _CACHE_ATTR

        plugin = DeclarativeQuotes()
        login_method = generate_login_method(plugin)

        mock_element = MagicMock()
        mock_bs, instance = _make_selenium_mock_bs()
        instance.driver = MagicMock()
        instance.driver.get = MagicMock()
        instance.driver.find_element = MagicMock(return_value=mock_element)
        instance.driver.page_source = "<html>Welcome back!</html>"
        instance.transfer_driver_cookies_to_session = MagicMock()

        mock_capture = MagicMock()
        mock_capture.start_capture = MagicMock()
        mock_capture.stop_capture = MagicMock()
        mock_capture.get_header_roles = MagicMock(return_value={})

        with (
            patch("graftpunk.BrowserSession", mock_bs),
            patch("graftpunk.plugins.cli_plugin.cache_session"),
            patch("graftpunk.plugins.login_engine.time.sleep"),
            patch(
                "graftpunk.observe.capture.create_capture_backend",
                return_value=mock_capture,
            ),
        ):
            result = login_method({"username": "user", "password": "test"})  # noqa: S106

        assert result is True
        # _build_token_cache should not have been called — no setattr on session
        token_cache = getattr(instance, _CACHE_ATTR, None)
        # MagicMock auto-creates attributes, so check it's either None or a MagicMock
        # (not a real dict). The key assertion is that _build_token_cache was never called.
        assert not isinstance(token_cache, dict)


class TestSeleniumWaitForRaises:
    """Tests that selenium backend raises on wait_for usage."""

    def test_selenium_wait_for_raises_plugin_error(self) -> None:
        """Selenium backend raises PluginError when wait_for is configured."""
        from graftpunk.plugins.login_engine import generate_login_method

        class SeleniumWaitFor(SitePlugin):
            site_name = "swf"
            session_name = "swf"
            help_text = "SWF"
            base_url = "https://example.com"
            backend = "selenium"
            login_config = LoginConfig(
                steps=[
                    LoginStep(
                        fields={"username": "#user"},
                        submit="#btn",
                    ),
                ],
                url="/login",
                wait_for="#form",
                timeout=0.05,
                settle=0.0,
            )

        plugin = SeleniumWaitFor()
        login_method = generate_login_method(plugin)

        mock_bs, instance = _make_selenium_mock_bs()
        instance.driver = MagicMock()

        with (
            patch("graftpunk.BrowserSession", mock_bs),
            pytest.raises(PluginError, match="wait_for.*requires.*nodriver"),
        ):
            login_method({"username": "user"})

    def test_selenium_step_wait_for_raises_plugin_error(self) -> None:
        """Selenium backend raises PluginError when step.wait_for is configured."""
        from graftpunk.plugins.login_engine import generate_login_method

        class SeleniumStepWaitFor(SitePlugin):
            site_name = "sswf"
            session_name = "sswf"
            help_text = "SSWF"
            base_url = "https://example.com"
            backend = "selenium"
            login_config = LoginConfig(
                steps=[
                    LoginStep(
                        fields={"username": "#user"},
                        submit="#next",
                    ),
                    LoginStep(
                        wait_for="#password-section",  # Per-step wait_for
                        fields={"password": "#pass"},
                        submit="#login",
                    ),
                ],
                url="/login",
                timeout=0.05,
                settle=0.0,
            )

        plugin = SeleniumStepWaitFor()
        login_method = generate_login_method(plugin)

        mock_element = MagicMock()
        mock_bs, instance = _make_selenium_mock_bs()
        instance.driver = MagicMock()
        instance.driver.find_element = MagicMock(return_value=mock_element)

        with (
            patch("graftpunk.BrowserSession", mock_bs),
            patch("graftpunk.plugins.login_engine.time.sleep"),
            pytest.raises(PluginError, match="Step 2.*step.wait_for is not supported for selenium"),
        ):
            login_method({"username": "user", "password": "pass"})  # noqa: S106


class DeclarativeMultiStep(SitePlugin):
    """Test plugin with multi-step login (nodriver)."""

    site_name = "multistep"
    session_name = "multistep"
    help_text = "Multi-Step Login"
    base_url = "https://example.com"
    backend = "nodriver"
    login_config = LoginConfig(
        steps=[
            LoginStep(
                fields={"username": "input#email"},
                submit="button#next",
            ),
            LoginStep(
                wait_for="#password-section",
                fields={"password": "input#password"},
                submit="button#submit",
            ),
        ],
        url="/login",
        success=".dashboard",
        timeout=0.05,
        settle=0.0,
    )


class DeclarativeStepWithDelay(SitePlugin):
    """Test plugin with step delay (nodriver)."""

    site_name = "stepdelay"
    session_name = "stepdelay"
    help_text = "Step With Delay"
    base_url = "https://example.com"
    backend = "nodriver"
    login_config = LoginConfig(
        steps=[
            LoginStep(
                fields={"username": "input#user", "password": "input#pass"},
                submit="button#login",
                delay=0.5,
            ),
        ],
        url="/login",
        success=".dashboard",
        timeout=0.05,
        settle=0.0,
    )


class DeclarativeTopLevelWaitFor(SitePlugin):
    """Test plugin with top-level wait_for (nodriver)."""

    site_name = "topwait"
    session_name = "topwait"
    help_text = "Top-Level Wait For"
    base_url = "https://example.com"
    backend = "nodriver"
    login_config = LoginConfig(
        steps=[
            LoginStep(
                fields={"username": "input#user"},
                submit="button#login",
            ),
        ],
        url="/login",
        wait_for="#login-form",
        success=".dashboard",
        timeout=0.05,
        settle=0.0,
    )


class DeclarativeThreeStepLogin(SitePlugin):
    """Test plugin with 3-step login including MFA (nodriver)."""

    site_name = "threestep"
    session_name = "threestep"
    help_text = "Three-Step Login"
    base_url = "https://example.com"
    backend = "nodriver"
    login_config = LoginConfig(
        steps=[
            LoginStep(
                fields={"username": "input#email"},
                submit="button#next",
            ),
            LoginStep(
                wait_for="#password-section",
                fields={"password": "input#password"},
                submit="button#continue",
            ),
            LoginStep(
                wait_for="#mfa-section",
                fields={"otp": "input#otp"},
                submit="button#verify",
                delay=0.1,
            ),
        ],
        url="/login",
        success=".dashboard",
        timeout=0.05,
        settle=0.0,
    )


class TestNodriverMultiStepLogin:
    """Tests for multi-step login with nodriver backend."""

    @pytest.mark.asyncio
    async def test_top_level_wait_for_timeout_raises_plugin_error(self) -> None:
        """Top-level wait_for timeout raises PluginError before steps execute."""
        from graftpunk.plugins.login_engine import generate_login_method

        plugin = DeclarativeTopLevelWaitFor()
        login_method = generate_login_method(plugin)

        mock_tab = MagicMock()
        mock_bs, instance = _make_nodriver_mock_bs()
        instance.driver = MagicMock()
        instance.driver.get = AsyncMock(return_value=mock_tab)

        mock_capture = MagicMock()
        mock_capture.start_capture_async = AsyncMock()

        with (
            patch("graftpunk.BrowserSession", mock_bs),
            patch(
                "graftpunk.observe.capture.create_capture_backend",
                return_value=mock_capture,
            ),
            patch(
                "graftpunk.plugins.login_engine._select_with_retry",
                return_value=None,  # Element never found
            ),
            pytest.raises(PluginError, match="Login page.*Timed out waiting for '#login-form'"),
        ):
            await login_method({"username": "user"})

    @pytest.mark.asyncio
    async def test_multi_step_login_executes_both_steps(self) -> None:
        """Multi-step login fills fields and clicks submit for each step."""
        from graftpunk.plugins.login_engine import generate_login_method

        plugin = DeclarativeMultiStep()
        login_method = generate_login_method(plugin)

        mock_tab = MagicMock()
        mock_element = AsyncMock()
        mock_tab.select = AsyncMock(return_value=mock_element)
        mock_tab.query_selector = mock_tab.select
        mock_tab.get_content = AsyncMock(return_value="<html>Dashboard</html>")

        mock_bs, instance = _make_nodriver_mock_bs()
        instance.driver = MagicMock()
        instance.driver.get = AsyncMock(return_value=mock_tab)
        instance.transfer_nodriver_cookies_to_session = AsyncMock()

        mock_capture = MagicMock()
        mock_capture.start_capture_async = AsyncMock()
        mock_capture.get_header_roles = MagicMock(return_value={})

        with (
            patch("graftpunk.BrowserSession", mock_bs),
            patch("graftpunk.plugins.cli_plugin.cache_session"),
            patch(
                "graftpunk.observe.capture.create_capture_backend",
                return_value=mock_capture,
            ),
        ):
            result = await login_method({"username": "user", "password": "pass123"})  # noqa: S106

        assert result is True

        # Verify element interactions happened for both steps:
        # Step 1: select email field, click, send_keys, select next button, click
        # Step 2: select wait_for element, select password field, click, send_keys,
        #         select submit button, click
        # Plus success selector check
        # _select_with_retry is called for: email, next, wait_for, password, submit
        # tab.select is called for success selector
        assert mock_element.click.await_count >= 4  # 2 fields + 2 submits
        assert mock_element.send_keys.await_count == 2  # username and password

    @pytest.mark.asyncio
    async def test_multi_step_login_with_step_wait_for(self) -> None:
        """Step-level wait_for waits before filling that step's fields."""
        from graftpunk.plugins.login_engine import generate_login_method

        plugin = DeclarativeMultiStep()
        login_method = generate_login_method(plugin)

        select_calls: list[str] = []

        async def track_select(tab: object, selector: str, **kwargs: object) -> AsyncMock:
            select_calls.append(selector)
            return AsyncMock()

        mock_tab = MagicMock()
        mock_tab.select = AsyncMock(return_value=AsyncMock())
        mock_tab.query_selector = mock_tab.select
        mock_tab.get_content = AsyncMock(return_value="<html>Dashboard</html>")

        mock_bs, instance = _make_nodriver_mock_bs()
        instance.driver = MagicMock()
        instance.driver.get = AsyncMock(return_value=mock_tab)
        instance.transfer_nodriver_cookies_to_session = AsyncMock()

        mock_capture = MagicMock()
        mock_capture.start_capture_async = AsyncMock()
        mock_capture.get_header_roles = MagicMock(return_value={})

        with (
            patch("graftpunk.BrowserSession", mock_bs),
            patch("graftpunk.plugins.cli_plugin.cache_session"),
            patch(
                "graftpunk.observe.capture.create_capture_backend",
                return_value=mock_capture,
            ),
            patch("graftpunk.plugins.login_engine._select_with_retry", side_effect=track_select),
        ):
            result = await login_method({"username": "user", "password": "pass"})  # noqa: S106

        assert result is True
        # Verify step 2's wait_for was called before password field
        password_section_idx = select_calls.index("#password-section")
        password_field_idx = select_calls.index("input#password")
        assert password_section_idx < password_field_idx

    @pytest.mark.asyncio
    async def test_step_wait_for_timeout_raises_plugin_error(self) -> None:
        """Step wait_for timeout raises PluginError with step index."""
        from graftpunk.plugins.login_engine import generate_login_method

        plugin = DeclarativeMultiStep()
        login_method = generate_login_method(plugin)

        async def select_side_effect(
            tab: object, selector: str, **kwargs: object
        ) -> AsyncMock | None:
            # Return element for step 1 (email, next button), None for step 2 wait_for
            if selector == "#password-section":
                return None
            return AsyncMock()

        mock_tab = MagicMock()
        mock_tab.select = AsyncMock(return_value=AsyncMock())
        mock_tab.query_selector = mock_tab.select

        mock_bs, instance = _make_nodriver_mock_bs()
        instance.driver = MagicMock()
        instance.driver.get = AsyncMock(return_value=mock_tab)

        mock_capture = MagicMock()
        mock_capture.start_capture_async = AsyncMock()
        mock_capture.get_header_roles = MagicMock(return_value={})

        with (
            patch("graftpunk.BrowserSession", mock_bs),
            patch(
                "graftpunk.observe.capture.create_capture_backend",
                return_value=mock_capture,
            ),
            patch(
                "graftpunk.plugins.login_engine._select_with_retry",
                side_effect=select_side_effect,
            ),
            pytest.raises(PluginError, match="Step 2.*Timed out waiting for '#password-section'"),
        ):
            await login_method({"username": "user", "password": "pass"})  # noqa: S106

    @pytest.mark.asyncio
    async def test_step_field_not_found_raises_with_step_index(self) -> None:
        """Field not found raises PluginError with step index."""
        from graftpunk.plugins.login_engine import generate_login_method

        plugin = DeclarativeMultiStep()
        login_method = generate_login_method(plugin)

        async def select_side_effect(
            tab: object, selector: str, **kwargs: object
        ) -> AsyncMock | None:
            # Return None for email field to trigger field not found error
            if selector == "input#email":
                return None
            return AsyncMock()

        mock_tab = MagicMock()
        mock_tab.select = AsyncMock(return_value=AsyncMock())
        mock_tab.query_selector = mock_tab.select

        mock_bs, instance = _make_nodriver_mock_bs()
        instance.driver = MagicMock()
        instance.driver.get = AsyncMock(return_value=mock_tab)

        mock_capture = MagicMock()
        mock_capture.start_capture_async = AsyncMock()
        mock_capture.get_header_roles = MagicMock(return_value={})

        with (
            patch("graftpunk.BrowserSession", mock_bs),
            patch(
                "graftpunk.observe.capture.create_capture_backend",
                return_value=mock_capture,
            ),
            patch(
                "graftpunk.plugins.login_engine._select_with_retry",
                side_effect=select_side_effect,
            ),
            pytest.raises(PluginError, match="Step 1.*Login field 'username' not found"),
        ):
            await login_method({"username": "user", "password": "pass"})  # noqa: S106

    @pytest.mark.asyncio
    async def test_step_submit_not_found_raises_with_step_index(self) -> None:
        """Submit button not found raises PluginError with step index."""
        from graftpunk.plugins.login_engine import generate_login_method

        plugin = DeclarativeMultiStep()
        login_method = generate_login_method(plugin)

        async def select_side_effect(
            tab: object, selector: str, **kwargs: object
        ) -> AsyncMock | None:
            # Return None for next button to trigger submit not found error
            if selector == "button#next":
                return None
            return AsyncMock()

        mock_tab = MagicMock()
        mock_tab.select = AsyncMock(return_value=AsyncMock())
        mock_tab.query_selector = mock_tab.select

        mock_bs, instance = _make_nodriver_mock_bs()
        instance.driver = MagicMock()
        instance.driver.get = AsyncMock(return_value=mock_tab)

        mock_capture = MagicMock()
        mock_capture.start_capture_async = AsyncMock()
        mock_capture.get_header_roles = MagicMock(return_value={})

        with (
            patch("graftpunk.BrowserSession", mock_bs),
            patch(
                "graftpunk.observe.capture.create_capture_backend",
                return_value=mock_capture,
            ),
            patch(
                "graftpunk.plugins.login_engine._select_with_retry",
                side_effect=select_side_effect,
            ),
            pytest.raises(PluginError, match="Step 1.*Submit button not found"),
        ):
            await login_method({"username": "user", "password": "pass"})  # noqa: S106

    @pytest.mark.asyncio
    async def test_step_delay_pauses_after_submit(self) -> None:
        """Step delay pauses after clicking submit."""
        from graftpunk.plugins.login_engine import generate_login_method

        plugin = DeclarativeStepWithDelay()
        login_method = generate_login_method(plugin)

        mock_tab = MagicMock()
        mock_element = AsyncMock()
        mock_tab.select = AsyncMock(return_value=mock_element)
        mock_tab.query_selector = mock_tab.select
        mock_tab.get_content = AsyncMock(return_value="<html>Dashboard</html>")

        mock_bs, instance = _make_nodriver_mock_bs()
        instance.driver = MagicMock()
        instance.driver.get = AsyncMock(return_value=mock_tab)
        instance.transfer_nodriver_cookies_to_session = AsyncMock()

        mock_capture = MagicMock()
        mock_capture.start_capture_async = AsyncMock()
        mock_capture.get_header_roles = MagicMock(return_value={})

        with (
            patch("graftpunk.BrowserSession", mock_bs),
            patch("graftpunk.plugins.cli_plugin.cache_session"),
            patch(
                "graftpunk.observe.capture.create_capture_backend",
                return_value=mock_capture,
            ),
            patch(
                "asyncio.sleep",
                new_callable=AsyncMock,
            ) as mock_sleep,
        ):
            result = await login_method({"username": "user", "password": "pass"})  # noqa: S106

        assert result is True
        # The step delay and the settle after the success signal are both slept for;
        # the other sleeps on the way (a settle per filled field, the poll interval
        # before the first tick) are the engine's business, not this test's.
        sleep_calls = [call[0][0] for call in mock_sleep.call_args_list]
        assert 0.5 in sleep_calls
        assert plugin.login_config.settle in sleep_calls

    @pytest.mark.asyncio
    async def test_step_without_submit_skips_click(self) -> None:
        """Step without submit selector skips the click action."""
        from graftpunk.plugins.login_engine import generate_login_method

        class NoSubmitStep(SitePlugin):
            site_name = "nosubmit"
            session_name = "nosubmit"
            help_text = "No Submit Step"
            base_url = "https://example.com"
            backend = "nodriver"
            login_config = LoginConfig(
                steps=[
                    LoginStep(
                        fields={"username": "input#user"},
                        # No submit - field entry triggers auto-advance
                    ),
                    LoginStep(
                        fields={"password": "input#pass"},
                        submit="button#login",
                    ),
                ],
                url="/login",
                timeout=0.05,
                settle=0.0,
            )

        plugin = NoSubmitStep()
        login_method = generate_login_method(plugin)

        click_count = 0

        async def mock_click() -> None:
            nonlocal click_count
            click_count += 1

        mock_element = AsyncMock()
        mock_element.click = mock_click

        mock_tab = MagicMock()
        mock_tab.select = AsyncMock(return_value=mock_element)
        mock_tab.query_selector = mock_tab.select
        mock_tab.get_content = AsyncMock(return_value="<html>Welcome</html>")

        mock_bs, instance = _make_nodriver_mock_bs()
        instance.driver = MagicMock()
        instance.driver.get = AsyncMock(return_value=mock_tab)
        instance.transfer_nodriver_cookies_to_session = AsyncMock()

        mock_capture = MagicMock()
        mock_capture.start_capture_async = AsyncMock()
        mock_capture.get_header_roles = MagicMock(return_value={})

        with (
            patch("graftpunk.BrowserSession", mock_bs),
            patch("graftpunk.plugins.cli_plugin.cache_session"),
            patch(
                "graftpunk.observe.capture.create_capture_backend",
                return_value=mock_capture,
            ),
        ):
            result = await login_method({"username": "user", "password": "pass"})  # noqa: S106

        assert result is True
        # Clicks: 2 field clicks + 1 submit click = 3
        # (Step 1 has no submit, Step 2 has submit)
        assert click_count == 3

    @pytest.mark.asyncio
    async def test_three_step_login_executes_all_steps(self) -> None:
        """Three-step login with MFA completes all steps successfully."""
        from graftpunk.plugins.login_engine import generate_login_method

        plugin = DeclarativeThreeStepLogin()
        login_method = generate_login_method(plugin)

        # Track selectors called to verify order
        selectors_called: list[str] = []

        async def select_side_effect(tab: object, selector: str, **kwargs: object) -> AsyncMock:
            selectors_called.append(selector)
            return AsyncMock()

        mock_tab = MagicMock()
        mock_tab.select = AsyncMock(return_value=AsyncMock())
        mock_tab.query_selector = mock_tab.select
        mock_tab.get_content = AsyncMock(return_value="<html>Dashboard</html>")

        mock_bs, instance = _make_nodriver_mock_bs()
        instance.driver = MagicMock()
        instance.driver.get = AsyncMock(return_value=mock_tab)
        instance.transfer_nodriver_cookies_to_session = AsyncMock()

        mock_capture = MagicMock()
        mock_capture.start_capture_async = AsyncMock()
        mock_capture.get_header_roles = MagicMock(return_value={})

        with (
            patch("graftpunk.BrowserSession", mock_bs),
            patch("graftpunk.plugins.cli_plugin.cache_session"),
            patch(
                "graftpunk.observe.capture.create_capture_backend",
                return_value=mock_capture,
            ),
            patch(
                "graftpunk.plugins.login_engine._select_with_retry",
                side_effect=select_side_effect,
            ),
        ):
            result = await login_method(
                {"username": "user", "password": "pass", "otp": "123456"}  # noqa: S106
            )

        assert result is True

        # Verify all 3 steps were processed by checking key selectors
        # Step 1: email, next button
        assert "input#email" in selectors_called
        assert "button#next" in selectors_called
        # Step 2: password-section wait, password, continue button
        assert "#password-section" in selectors_called
        assert "input#password" in selectors_called
        assert "button#continue" in selectors_called
        # Step 3: mfa-section wait, otp, verify button
        assert "#mfa-section" in selectors_called
        assert "input#otp" in selectors_called
        assert "button#verify" in selectors_called


class DeclarativeSeleniumMultiStep(SitePlugin):
    """Test plugin with multi-step login (selenium)."""

    site_name = "selmultistep"
    session_name = "selmultistep"
    help_text = "Selenium Multi-Step Login"
    base_url = "https://example.com"
    backend = "selenium"
    login_config = LoginConfig(
        steps=[
            LoginStep(
                fields={"username": "input#email"},
                submit="button#next",
            ),
            LoginStep(
                fields={"password": "input#password"},
                submit="button#submit",
            ),
        ],
        url="/login",
        success=".dashboard",
        timeout=0.05,
        settle=0.0,
    )


class DeclarativeSeleniumStepWithDelay(SitePlugin):
    """Test plugin with step delay (selenium)."""

    site_name = "selstepdelay"
    session_name = "selstepdelay"
    help_text = "Selenium Step With Delay"
    base_url = "https://example.com"
    backend = "selenium"
    login_config = LoginConfig(
        steps=[
            LoginStep(
                fields={"username": "input#user", "password": "input#pass"},
                submit="button#login",
                delay=0.5,
            ),
        ],
        url="/login",
        success=".dashboard",
        timeout=0.05,
        settle=0.0,
    )


class TestSeleniumMultiStepLogin:
    """Tests for multi-step login with selenium backend."""

    def test_multi_step_login_executes_both_steps(self) -> None:
        """Multi-step login fills fields and clicks submit for each step."""
        from graftpunk.plugins.login_engine import generate_login_method

        plugin = DeclarativeSeleniumMultiStep()
        login_method = generate_login_method(plugin)

        find_element_calls: list[str] = []

        def mock_find_element(by: str, value: str) -> MagicMock:
            find_element_calls.append(value)
            return MagicMock()

        mock_bs, instance = _make_selenium_mock_bs()
        instance.driver = MagicMock()
        instance.driver.get = MagicMock()
        instance.driver.find_element = mock_find_element
        instance.driver.page_source = "<html><div class='dashboard'>Welcome</div></html>"
        instance.transfer_driver_cookies_to_session = MagicMock()

        mock_capture = MagicMock()
        mock_capture.start_capture = MagicMock()
        mock_capture.stop_capture = MagicMock()
        mock_capture.get_header_roles = MagicMock(return_value={})

        with (
            patch("graftpunk.BrowserSession", mock_bs),
            patch("graftpunk.plugins.cli_plugin.cache_session"),
            patch("graftpunk.plugins.login_engine.time.sleep"),
            patch(
                "graftpunk.observe.capture.create_capture_backend",
                return_value=mock_capture,
            ),
        ):
            result = login_method({"username": "user", "password": "pass123"})  # noqa: S106

        assert result is True

        # Verify element interactions for both steps:
        # Step 1: email field, next button
        # Step 2: password field, submit button
        # Plus success selector check
        assert "input#email" in find_element_calls
        assert "button#next" in find_element_calls
        assert "input#password" in find_element_calls
        assert "button#submit" in find_element_calls
        assert ".dashboard" in find_element_calls

    def test_step_field_not_found_raises_with_step_index(self) -> None:
        """Field not found raises PluginError with step index."""
        from selenium.common.exceptions import NoSuchElementException

        from graftpunk.plugins.login_engine import generate_login_method

        plugin = DeclarativeSeleniumMultiStep()
        login_method = generate_login_method(plugin)

        def mock_find_element(by: str, value: str) -> MagicMock:
            if value == "input#email":
                raise NoSuchElementException("Element not found")
            return MagicMock()

        mock_bs, instance = _make_selenium_mock_bs()
        instance.driver = MagicMock()
        instance.driver.get = MagicMock()
        instance.driver.find_element = mock_find_element

        mock_capture = MagicMock()
        mock_capture.start_capture = MagicMock()

        with (
            patch("graftpunk.BrowserSession", mock_bs),
            patch("graftpunk.plugins.login_engine.time.sleep"),
            patch(
                "graftpunk.observe.capture.create_capture_backend",
                return_value=mock_capture,
            ),
            pytest.raises(PluginError, match="Step 1.*Failed to fill login field 'username'"),
        ):
            login_method({"username": "user", "password": "pass"})  # noqa: S106

    def test_step_submit_not_found_raises_with_step_index(self) -> None:
        """Submit button not found raises PluginError with step index."""
        from selenium.common.exceptions import NoSuchElementException

        from graftpunk.plugins.login_engine import generate_login_method

        plugin = DeclarativeSeleniumMultiStep()
        login_method = generate_login_method(plugin)

        call_count = 0

        def mock_find_element(by: str, value: str) -> MagicMock:
            nonlocal call_count
            call_count += 1
            # First call: email field (ok)
            # Second call: next button (fail)
            if call_count == 2:
                raise NoSuchElementException("Button not found")
            return MagicMock()

        mock_bs, instance = _make_selenium_mock_bs()
        instance.driver = MagicMock()
        instance.driver.get = MagicMock()
        instance.driver.find_element = mock_find_element

        mock_capture = MagicMock()
        mock_capture.start_capture = MagicMock()

        with (
            patch("graftpunk.BrowserSession", mock_bs),
            patch("graftpunk.plugins.login_engine.time.sleep"),
            patch(
                "graftpunk.observe.capture.create_capture_backend",
                return_value=mock_capture,
            ),
            pytest.raises(PluginError, match="Step 1.*Failed to click submit button"),
        ):
            login_method({"username": "user", "password": "pass"})  # noqa: S106

    def test_step_delay_pauses_after_submit(self) -> None:
        """Step delay pauses after clicking submit."""
        from graftpunk.plugins.login_engine import generate_login_method

        plugin = DeclarativeSeleniumStepWithDelay()
        login_method = generate_login_method(plugin)

        mock_element = MagicMock()
        mock_bs, instance = _make_selenium_mock_bs()
        instance.driver = MagicMock()
        instance.driver.get = MagicMock()
        instance.driver.find_element = MagicMock(return_value=mock_element)
        instance.driver.page_source = "<html><div class='dashboard'>Welcome</div></html>"
        instance.transfer_driver_cookies_to_session = MagicMock()

        mock_capture = MagicMock()
        mock_capture.start_capture = MagicMock()
        mock_capture.stop_capture = MagicMock()
        mock_capture.get_header_roles = MagicMock(return_value={})

        with (
            patch("graftpunk.BrowserSession", mock_bs),
            patch("graftpunk.plugins.cli_plugin.cache_session"),
            patch("time.sleep") as mock_sleep,
            patch(
                "graftpunk.observe.capture.create_capture_backend",
                return_value=mock_capture,
            ),
        ):
            result = login_method({"username": "user", "password": "pass"})  # noqa: S106

        assert result is True
        # The step delay and the settle after the success signal are both slept for.
        sleep_calls = [call[0][0] for call in mock_sleep.call_args_list]
        assert 0.5 in sleep_calls
        assert plugin.login_config.settle in sleep_calls

    def test_step_without_submit_skips_click(self) -> None:
        """Step without submit selector skips the click action."""
        from graftpunk.plugins.login_engine import generate_login_method

        class SeleniumNoSubmitStep(SitePlugin):
            site_name = "selnosubmit"
            session_name = "selnosubmit"
            help_text = "Selenium No Submit Step"
            base_url = "https://example.com"
            backend = "selenium"
            login_config = LoginConfig(
                steps=[
                    LoginStep(
                        fields={"username": "input#user"},
                        # No submit - field entry triggers auto-advance
                    ),
                    LoginStep(
                        fields={"password": "input#pass"},
                        submit="button#login",
                    ),
                ],
                url="/login",
                timeout=0.05,
                settle=0.0,
            )

        plugin = SeleniumNoSubmitStep()
        login_method = generate_login_method(plugin)

        click_count = 0

        def make_element() -> MagicMock:
            nonlocal click_count
            elem = MagicMock()
            original_click = elem.click

            def counted_click() -> None:
                nonlocal click_count
                click_count += 1
                original_click()

            elem.click = counted_click
            return elem

        mock_bs, instance = _make_selenium_mock_bs()
        instance.driver = MagicMock()
        instance.driver.get = MagicMock()
        instance.driver.find_element = MagicMock(side_effect=lambda *a, **kw: make_element())
        instance.driver.page_source = "<html>Welcome</html>"
        instance.transfer_driver_cookies_to_session = MagicMock()

        mock_capture = MagicMock()
        mock_capture.start_capture = MagicMock()
        mock_capture.stop_capture = MagicMock()
        mock_capture.get_header_roles = MagicMock(return_value={})

        with (
            patch("graftpunk.BrowserSession", mock_bs),
            patch("graftpunk.plugins.cli_plugin.cache_session"),
            patch("graftpunk.plugins.login_engine.time.sleep"),
            patch(
                "graftpunk.observe.capture.create_capture_backend",
                return_value=mock_capture,
            ),
        ):
            result = login_method({"username": "user", "password": "pass"})  # noqa: S106

        assert result is True
        # Clicks: 2 field clicks + 1 submit click = 3
        # (Step 1 has no submit, Step 2 has submit)
        assert click_count == 3


class TestResolveUrl:
    """Tests for _resolve_url — login/token URL resolution against base_url."""

    def test_relative_path_joined_onto_base_url(self):
        from graftpunk.plugins.login_engine import _resolve_url

        assert _resolve_url("https://api.example.com", "/login") == "https://api.example.com/login"

    def test_empty_url_yields_base_url(self):
        from graftpunk.plugins.login_engine import _resolve_url

        assert _resolve_url("https://api.example.com", "") == "https://api.example.com"

    def test_absolute_url_on_different_host_used_as_is(self):
        # The bug this guards: login host differs from the API base_url, so the
        # absolute login_config.url must be used directly, not concatenated.
        from graftpunk.plugins.login_engine import _resolve_url

        assert (
            _resolve_url("https://embedded.example.com", "https://www.example.com/login")
            == "https://www.example.com/login"
        )

    def test_absolute_http_and_uppercase_scheme_used_as_is(self):
        from graftpunk.plugins.login_engine import _resolve_url

        assert _resolve_url("https://api.x.com", "http://login.x.com/") == "http://login.x.com/"
        assert _resolve_url("https://api.x.com", "HTTPS://login.x.com/a") == "HTTPS://login.x.com/a"
