"""Tests for the declarative login engine."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from graftpunk.exceptions import PluginError
from graftpunk.plugins.cli_plugin import LoginConfig, SitePlugin


class DeclarativeHN(SitePlugin):
    """Test plugin with declarative login (nodriver)."""

    site_name = "hn"
    session_name = "hackernews"
    help_text = "HN"
    base_url = "https://news.ycombinator.com"
    backend = "nodriver"
    login_config = LoginConfig(
        url="/login",
        fields={"username": "input[name='acct']", "password": "input[name='pw']"},
        submit="input[value='login']",
        failure="Bad login.",
    )


class DeclarativeQuotes(SitePlugin):
    """Test plugin with declarative login (selenium)."""

    site_name = "quotes"
    session_name = "quotes"
    help_text = "Quotes"
    base_url = "https://quotes.toscrape.com"
    backend = "selenium"
    login_config = LoginConfig(
        url="/login",
        fields={"username": "#username", "password": "#password"},
        submit="input[type='submit']",
        success="a[href='/logout']",
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

        mock_tab = AsyncMock()
        mock_element = AsyncMock()
        mock_tab.select = AsyncMock(return_value=mock_element)
        mock_tab.get_content = AsyncMock(return_value="<html>Welcome</html>")

        mock_bs, instance = _make_nodriver_mock_bs()
        instance.driver = MagicMock()
        instance.driver.get = AsyncMock(return_value=mock_tab)
        instance.transfer_nodriver_cookies_to_session = AsyncMock()

        with (
            patch("graftpunk.plugins.login_engine.BrowserSession", mock_bs),
            patch("graftpunk.plugins.login_engine.cache_session"),
        ):
            result = await login_method({"username": "user", "password": "test"})  # noqa: S106

        assert result is True

    @pytest.mark.asyncio
    async def test_nodriver_login_failure(self) -> None:
        """Test nodriver declarative login failure path."""
        from graftpunk.plugins.login_engine import generate_login_method

        plugin = DeclarativeHN()
        login_method = generate_login_method(plugin)

        mock_tab = AsyncMock()
        mock_element = AsyncMock()
        mock_tab.select = AsyncMock(return_value=mock_element)
        mock_tab.get_content = AsyncMock(return_value="<html>Bad login.</html>")

        mock_bs, instance = _make_nodriver_mock_bs()
        instance.driver = MagicMock()
        instance.driver.get = AsyncMock(return_value=mock_tab)

        with patch("graftpunk.plugins.login_engine.BrowserSession", mock_bs):
            result = await login_method({"username": "user", "password": "wrong"})  # noqa: S106

        assert result is False

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
            patch("graftpunk.plugins.login_engine.BrowserSession", mock_bs),
            patch("graftpunk.plugins.login_engine.cache_session"),
            patch("graftpunk.plugins.login_engine.time"),
        ):
            result = login_method({"username": "user", "password": "test"})  # noqa: S106

        assert result is True

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
            patch("graftpunk.plugins.login_engine.BrowserSession", mock_bs),
            patch("graftpunk.plugins.login_engine.cache_session"),
            patch("graftpunk.plugins.login_engine.time"),
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
        url="/login",
        fields={"username": "#user", "password": "#pass"},
        submit="#submit",
        failure="Invalid credentials",
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
            patch("graftpunk.plugins.login_engine.BrowserSession", mock_bs),
            patch("graftpunk.plugins.login_engine.time"),
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
            patch("graftpunk.plugins.login_engine.BrowserSession", mock_bs),
            patch("graftpunk.plugins.login_engine.cache_session"),
            patch("graftpunk.plugins.login_engine.time"),
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

        mock_tab = AsyncMock()
        mock_tab.select = AsyncMock(side_effect=RuntimeError("Element vanished"))

        mock_bs, instance = _make_nodriver_mock_bs()
        instance.driver = MagicMock()
        instance.driver.get = AsyncMock(return_value=mock_tab)

        with patch("graftpunk.plugins.login_engine.BrowserSession", mock_bs):
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

        mock_tab = AsyncMock()
        mock_tab.select = AsyncMock(side_effect=RuntimeError("Boom"))

        mock_bs, instance = _make_nodriver_mock_bs()
        instance.driver = MagicMock()
        instance.driver.get = AsyncMock(return_value=mock_tab)

        with patch("graftpunk.plugins.login_engine.BrowserSession", mock_bs):
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
            patch("graftpunk.plugins.login_engine.BrowserSession", mock_bs),
            patch("graftpunk.plugins.login_engine.time"),
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
            patch("graftpunk.plugins.login_engine.BrowserSession", mock_bs),
            patch("graftpunk.plugins.login_engine.time"),
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
            patch("graftpunk.plugins.login_engine.BrowserSession", mock_bs),
            patch("graftpunk.plugins.login_engine.time"),
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
                url="/login",
                fields={"email": "#email", "password": "#pass"},
                submit="#submit",
                failure="Invalid",
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
            patch("graftpunk.plugins.login_engine.BrowserSession", mock_bs),
            patch("graftpunk.plugins.login_engine.time"),
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
                url="/login",
                fields={"email": "#email", "password": "#pass"},
                submit="#submit",
                failure="Invalid",
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
            patch("graftpunk.plugins.login_engine.BrowserSession", mock_bs),
            patch("graftpunk.plugins.login_engine.time"),
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
        url="/login",
        fields={"username": "#user", "password": "#pass"},
        submit="#submit",
        success=".dashboard",
    )


class DeclarativeNodriverNoValidation(SitePlugin):
    """Nodriver plugin with no validation configured."""

    site_name = "ndnoval"
    session_name = "ndnoval"
    help_text = "ND No Validation"
    base_url = "https://example.com"
    backend = "nodriver"
    login_config = LoginConfig(
        url="/login",
        fields={"username": "#user", "password": "#pass"},
        submit="#submit",
    )


class DeclarativeSeleniumBoth(SitePlugin):
    """Selenium plugin with both failure_text and success_selector."""

    site_name = "selboth"
    session_name = "selboth"
    help_text = "Sel Both"
    base_url = "https://example.com"
    backend = "selenium"
    login_config = LoginConfig(
        url="/login",
        fields={"username": "#user", "password": "#pass"},
        submit="#submit",
        failure="Invalid credentials",
        success=".dashboard",
    )


class TestNodriverLoginValidationPaths:
    """Tests for nodriver login validation paths."""

    @pytest.mark.asyncio
    async def test_nodriver_login_success_selector_not_found(self) -> None:
        """Success_selector configured but element not found returns False."""
        from graftpunk.plugins.login_engine import generate_login_method

        plugin = DeclarativeNodriverSuccess()
        login_method = generate_login_method(plugin)

        mock_tab = AsyncMock()
        mock_element = AsyncMock()
        mock_tab.get_content = AsyncMock(return_value="<html>Welcome</html>")

        # select succeeds for form fields, returns None for success selector (timeout)
        async def select_side_effect(selector: str, **kwargs: object) -> AsyncMock | None:
            if selector == ".dashboard":
                return None
            return mock_element

        mock_tab.select = AsyncMock(side_effect=select_side_effect)

        mock_bs, instance = _make_nodriver_mock_bs()
        instance.driver = MagicMock()
        instance.driver.get = AsyncMock(return_value=mock_tab)

        with patch("graftpunk.plugins.login_engine.BrowserSession", mock_bs):
            result = await login_method({"username": "user", "password": "test"})  # noqa: S106

        assert result is False

    @pytest.mark.asyncio
    async def test_nodriver_login_success_selector_found(self) -> None:
        """Success_selector configured and element found returns True."""
        from graftpunk.plugins.login_engine import generate_login_method

        plugin = DeclarativeNodriverSuccess()
        login_method = generate_login_method(plugin)

        mock_tab = AsyncMock()
        mock_element = AsyncMock()
        mock_tab.select = AsyncMock(return_value=mock_element)
        mock_tab.get_content = AsyncMock(return_value="<html>Welcome</html>")

        mock_bs, instance = _make_nodriver_mock_bs()
        instance.driver = MagicMock()
        instance.driver.get = AsyncMock(return_value=mock_tab)
        instance.transfer_nodriver_cookies_to_session = AsyncMock()

        with (
            patch("graftpunk.plugins.login_engine.BrowserSession", mock_bs),
            patch("graftpunk.plugins.login_engine.cache_session"),
        ):
            result = await login_method({"username": "user", "password": "test"})  # noqa: S106

        assert result is True

    @pytest.mark.asyncio
    async def test_nodriver_login_success_selector_unknown_error_propagates(self) -> None:
        """Unknown error during success selector check propagates instead of returning False."""
        from graftpunk.plugins.login_engine import generate_login_method

        plugin = DeclarativeNodriverSuccess()
        login_method = generate_login_method(plugin)

        mock_tab = AsyncMock()
        mock_element = AsyncMock()
        mock_tab.get_content = AsyncMock(return_value="<html>Welcome</html>")

        # select succeeds for form fields, raises unknown error for success selector
        async def select_side_effect(selector: str, **kwargs: object) -> AsyncMock:
            if selector == ".dashboard":
                raise ConnectionError("WebDriver session crashed")
            return mock_element

        mock_tab.select = AsyncMock(side_effect=select_side_effect)

        mock_bs, instance = _make_nodriver_mock_bs()
        instance.driver = MagicMock()
        instance.driver.get = AsyncMock(return_value=mock_tab)

        with (
            patch("graftpunk.plugins.login_engine.BrowserSession", mock_bs),
            pytest.raises(ConnectionError, match="WebDriver session crashed"),
        ):
            await login_method({"username": "user", "password": "test"})  # noqa: S106

    @pytest.mark.asyncio
    async def test_nodriver_login_no_validation_configured(self) -> None:
        """Neither failure_text nor success_selector set returns True with warning."""
        from graftpunk.plugins.login_engine import generate_login_method

        plugin = DeclarativeNodriverNoValidation()
        login_method = generate_login_method(plugin)

        mock_tab = AsyncMock()
        mock_element = AsyncMock()
        mock_tab.select = AsyncMock(return_value=mock_element)
        mock_tab.get_content = AsyncMock(return_value="<html>Welcome</html>")

        mock_bs, instance = _make_nodriver_mock_bs()
        instance.driver = MagicMock()
        instance.driver.get = AsyncMock(return_value=mock_tab)
        instance.transfer_nodriver_cookies_to_session = AsyncMock()

        with (
            patch("graftpunk.plugins.login_engine.BrowserSession", mock_bs),
            patch("graftpunk.plugins.login_engine.cache_session"),
            patch("graftpunk.plugins.login_engine.LOG") as mock_log,
        ):
            result = await login_method({"username": "user", "password": "test"})  # noqa: S106

        assert result is True
        mock_log.warning.assert_called_once_with(
            "login_no_validation_configured",
            plugin="ndnoval",
            hint="Consider adding login_failure or login_success to validate login result",
        )


class TestCheckLoginResult:
    """Direct tests for _check_login_result()."""

    def test_failure_text_present_returns_false(self) -> None:
        """Returns False when failure text is found in page text (case-insensitive)."""
        from graftpunk.plugins.login_engine import _check_login_result

        result = _check_login_result(
            page_text="<html>Bad login. Try again.</html>",
            failure_text="Bad login.",
            success_found=None,
            success_selector="",
            site_name="test",
        )
        assert result is False

    def test_failure_text_case_insensitive(self) -> None:
        """Failure text matching is case-insensitive."""
        from graftpunk.plugins.login_engine import _check_login_result

        result = _check_login_result(
            page_text="<html>INVALID CREDENTIALS</html>",
            failure_text="invalid credentials",
            success_found=None,
            success_selector="",
            site_name="test",
        )
        assert result is False

    def test_success_selector_found_returns_true(self) -> None:
        """Returns True when success element was found."""
        from graftpunk.plugins.login_engine import _check_login_result

        result = _check_login_result(
            page_text="<html>Dashboard</html>",
            failure_text="",
            success_found=True,
            success_selector=".dashboard",
            site_name="test",
        )
        assert result is True

    def test_success_selector_not_found_returns_false(self) -> None:
        """Returns False when success element was not found."""
        from graftpunk.plugins.login_engine import _check_login_result

        result = _check_login_result(
            page_text="<html>Login page</html>",
            failure_text="",
            success_found=False,
            success_selector=".dashboard",
            site_name="test",
        )
        assert result is False

    def test_neither_failure_nor_success_returns_true(self) -> None:
        """Returns True (optimistic) when neither failure nor success is configured."""
        from graftpunk.plugins.login_engine import _check_login_result

        result = _check_login_result(
            page_text="<html>Something</html>",
            failure_text="",
            success_found=None,
            success_selector="",
            site_name="test",
        )
        assert result is True

    def test_neither_configured_logs_warning(self) -> None:
        """Logs a no-validation warning when neither failure_text nor success_selector set."""
        from graftpunk.plugins.login_engine import _check_login_result

        with patch("graftpunk.plugins.login_engine._warn_no_login_validation") as mock_warn:
            _check_login_result(
                page_text="<html>Something</html>",
                failure_text="",
                success_found=None,
                success_selector="",
                site_name="testplugin",
            )
            mock_warn.assert_called_once_with("testplugin")

    def test_empty_failure_text_with_success_found_returns_true(self) -> None:
        """Empty failure_text + success found returns True without warning."""
        from graftpunk.plugins.login_engine import _check_login_result

        result = _check_login_result(
            page_text="<html>Welcome</html>",
            failure_text="",
            success_found=True,
            success_selector=".dashboard",
            site_name="test",
        )
        assert result is True

    def test_failure_text_takes_priority_over_success(self) -> None:
        """Failure text check runs before success check; returns False even if success_found."""
        from graftpunk.plugins.login_engine import _check_login_result

        result = _check_login_result(
            page_text="<html>Bad login. Dashboard link here.</html>",
            failure_text="Bad login.",
            success_found=True,
            success_selector=".dashboard",
            site_name="test",
        )
        assert result is False

    def test_failure_text_not_in_page_with_success_none(self) -> None:
        """Failure text configured but not in page, no success selector -> True with warning."""
        from graftpunk.plugins.login_engine import _check_login_result

        # failure_text is set but not found in page, success_found is None
        # -> no failure detected, no success configured -> True + warning
        with patch("graftpunk.plugins.login_engine._warn_no_login_validation") as mock_warn:
            result = _check_login_result(
                page_text="<html>Welcome</html>",
                failure_text="Bad login.",
                success_found=None,
                success_selector="",
                site_name="test",
            )
        assert result is True
        # No warning because failure_text is set (non-empty)
        mock_warn.assert_not_called()


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
    async def test_nodriver_login_sets_header_profiles(self) -> None:
        """Nodriver login extracts header profiles from capture backend."""
        from graftpunk.plugins.login_engine import generate_login_method

        plugin = DeclarativeHN()
        login_method = generate_login_method(plugin)

        mock_tab = AsyncMock()
        mock_element = AsyncMock()
        mock_tab.select = AsyncMock(return_value=mock_element)
        mock_tab.get_content = AsyncMock(return_value="<html>Welcome</html>")

        mock_bs, instance = _make_nodriver_mock_bs()
        instance.driver = MagicMock()
        instance.driver.get = AsyncMock(return_value=mock_tab)
        instance.transfer_nodriver_cookies_to_session = AsyncMock()

        mock_capture = MagicMock()
        mock_capture.start_capture_async = AsyncMock()
        mock_capture.get_header_profiles.return_value = {
            "navigation": {"User-Agent": "TestBrowser/1.0"}
        }

        with (
            patch("graftpunk.plugins.login_engine.BrowserSession", mock_bs),
            patch("graftpunk.plugins.login_engine.cache_session"),
            patch(
                "graftpunk.observe.capture.create_capture_backend",
                return_value=mock_capture,
            ),
        ):
            result = await login_method({"username": "user", "password": "test"})  # noqa: S106

        assert result is True
        assert instance._gp_header_profiles == {"navigation": {"User-Agent": "TestBrowser/1.0"}}
        mock_capture.start_capture_async.assert_awaited_once()
        mock_capture.get_header_profiles.assert_called_once()

    def test_selenium_login_sets_header_profiles(self) -> None:
        """Selenium login extracts header profiles from capture backend."""
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
        mock_capture.get_header_profiles.return_value = {"xhr": {"Accept": "application/json"}}

        with (
            patch("graftpunk.plugins.login_engine.BrowserSession", mock_bs),
            patch("graftpunk.plugins.login_engine.cache_session"),
            patch("graftpunk.plugins.login_engine.time"),
            patch(
                "graftpunk.observe.capture.create_capture_backend",
                return_value=mock_capture,
            ),
        ):
            result = login_method({"username": "user", "password": "test"})  # noqa: S106

        assert result is True
        assert instance._gp_header_profiles == {"xhr": {"Accept": "application/json"}}
        mock_capture.start_capture.assert_called_once()
        mock_capture.stop_capture.assert_called_once()
        mock_capture.get_header_profiles.assert_called_once()


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
            patch("graftpunk.plugins.login_engine.BrowserSession", mock_bs),
            patch("graftpunk.plugins.login_engine.cache_session"),
            patch("graftpunk.plugins.login_engine.time"),
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
            patch("graftpunk.plugins.login_engine.BrowserSession", mock_bs),
            patch("graftpunk.plugins.login_engine.cache_session"),
            patch("graftpunk.plugins.login_engine.time"),
        ):
            result = login_method({"username": "user", "password": "test"})  # noqa: S106

        assert result is True


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
                url="/login",
                fields={"username": "#user", "password": "#pass"},
                submit="#submit",
                success=".dashboard",
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
        mock_capture.get_header_profiles = MagicMock(return_value={})

        with (
            patch("graftpunk.plugins.login_engine.BrowserSession", mock_bs),
            patch("graftpunk.plugins.login_engine.cache_session"),
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
        mock_tab.get_content = AsyncMock(return_value="<html>Welcome back!</html>")
        mock_tab.url = "https://news.ycombinator.com/news"

        mock_bs, instance = _make_nodriver_mock_bs()
        instance.driver = MagicMock()
        instance.driver.get = AsyncMock(return_value=mock_tab)
        instance.transfer_nodriver_cookies_to_session = AsyncMock()

        mock_capture = MagicMock()
        mock_capture.start_capture_async = AsyncMock()
        mock_capture.get_header_profiles = MagicMock(return_value={})

        with (
            patch("graftpunk.plugins.login_engine.BrowserSession", mock_bs),
            patch("graftpunk.plugins.login_engine.cache_session"),
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
                url="/login",
                fields={"username": "#user", "password": "#pass"},
                submit="#submit",
                success=".dashboard",
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
        mock_capture.get_header_profiles = MagicMock(return_value={})

        with (
            patch("graftpunk.plugins.login_engine.BrowserSession", mock_bs),
            patch("graftpunk.plugins.login_engine.cache_session"),
            patch("graftpunk.plugins.login_engine.time"),
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
        mock_capture.get_header_profiles = MagicMock(return_value={})

        with (
            patch("graftpunk.plugins.login_engine.BrowserSession", mock_bs),
            patch("graftpunk.plugins.login_engine.cache_session"),
            patch("graftpunk.plugins.login_engine.time"),
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
                url="/login",
                fields={"username": "#user"},
                submit="#btn",
                wait_for="#form",
            )

        plugin = SeleniumWaitFor()
        login_method = generate_login_method(plugin)

        mock_bs, instance = _make_selenium_mock_bs()
        instance.driver = MagicMock()

        with (
            patch("graftpunk.plugins.login_engine.BrowserSession", mock_bs),
            pytest.raises(PluginError, match="wait_for.*requires.*nodriver"),
        ):
            login_method({"username": "user"})
