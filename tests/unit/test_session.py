"""Tests for session module.

Note: Many BrowserSession tests require a real browser and are better suited
for integration tests. These unit tests focus on testable components that
don't require actual browser instantiation.
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestSessionName:
    """Tests for session_name property."""

    def test_session_name_setter(self):
        """Test that session_name can be set directly on an instance."""
        from graftpunk.session import BrowserSession

        with patch.object(BrowserSession, "__init__", return_value=None):
            session = BrowserSession.__new__(BrowserSession)
            session.session_name = "custom-name"
            assert session._session_name == "custom-name"

    def test_session_name_getter_uses_cached_name(self):
        """Test that session_name getter returns cached name."""
        from graftpunk.session import BrowserSession

        with patch.object(BrowserSession, "__init__", return_value=None):
            session = BrowserSession.__new__(BrowserSession)
            session._session_name = "cached-name"
            assert session.session_name == "cached-name"


class TestBrowserError:
    """Tests for browser error handling."""

    def test_browser_error_inheritance(self):
        """Test that BrowserError inherits from GraftpunkError."""
        from graftpunk.exceptions import BrowserError, GraftpunkError

        assert issubclass(BrowserError, GraftpunkError)

    def test_chrome_driver_error_inheritance(self):
        """Test that ChromeDriverError inherits from BrowserError."""
        from graftpunk.exceptions import BrowserError, ChromeDriverError

        assert issubclass(ChromeDriverError, BrowserError)

    def test_mfa_required_error(self):
        """Test MFARequiredError exception."""
        from graftpunk.exceptions import GraftpunkError, MFARequiredError

        assert issubclass(MFARequiredError, GraftpunkError)

        # Test with default message
        error = MFARequiredError()
        assert "MFA is required" in str(error)
        assert error.mfa_type is None

        # Test with custom message and mfa_type
        error = MFARequiredError("Need TOTP code", mfa_type="totp")
        assert "Need TOTP code" in str(error)
        assert error.mfa_type == "totp"


class TestBrowserSessionBackendValidation:
    """Tests for BrowserSession backend parameter validation."""

    def test_invalid_backend_raises_value_error(self):
        """BrowserSession raises ValueError for unknown backend."""
        import pytest

        from graftpunk.session import BrowserSession

        # Patch the stealth driver creation to avoid actual browser startup
        with (
            patch("graftpunk.stealth.create_stealth_driver"),
            pytest.raises(ValueError, match="Unknown backend 'invalid'"),
        ):
            BrowserSession(backend="invalid")

    def test_valid_backend_is_accepted(self):
        """BrowserSession accepts valid backend names."""
        from graftpunk.backends import list_backends

        # Verify selenium is a valid backend
        assert "selenium" in list_backends()
        # Verify nodriver is a valid backend
        assert "nodriver" in list_backends()


class TestBrowserSessionStateSerialization:
    """Tests for BrowserSession __getstate__/__setstate__ with backend_type."""

    def test_getstate_includes_backend_type(self):
        """__getstate__ includes _backend_type in serialized state."""
        from graftpunk.session import BrowserSession

        with patch.object(BrowserSession, "__init__", return_value=None):
            session = BrowserSession.__new__(BrowserSession)
            session._backend_type = "nodriver"
            session._use_stealth = False

            # Mock the required attributes for __getstate__
            session._driver_initializer = None
            session.webdriver_path = None
            session.default_timeout = 15
            session.webdriver_options = {}
            session._last_requests_url = None
            session.cookies = {}
            session.headers = {}
            session.auth = None
            session.proxies = {}
            session.hooks = {}
            session.params = {}
            session.verify = True
            session._session_name = "test"

            # Create a mock driver - requestium uses _driver internally
            mock_driver = MagicMock()
            mock_driver.current_url = "https://example.com"
            session._driver = mock_driver
            session._webdriver = mock_driver

            # Now patch the parent's __getstate__
            with patch("requestium.Session.__getstate__", return_value={}):
                state = session.__getstate__()

            assert state["_backend_type"] == "nodriver"
            assert state["_use_stealth"] is False

    def test_setstate_restores_backend_type(self):
        """__setstate__ restores _backend_type from serialized state."""
        from graftpunk.session import BrowserSession

        with patch.object(BrowserSession, "__init__", return_value=None):
            session = BrowserSession.__new__(BrowserSession)

            # Create state with backend_type
            state = {
                "_backend_type": "nodriver",
                "_use_stealth": False,
                "_driver": None,
            }

            # Patch parent's __setstate__
            with patch("requestium.Session.__setstate__"):
                session.__setstate__(state)

            assert session._backend_type == "nodriver"
            assert session._use_stealth is False

    def test_setstate_defaults_to_selenium_for_legacy_sessions(self):
        """__setstate__ defaults to selenium for sessions without backend_type."""
        from graftpunk.session import BrowserSession

        with patch.object(BrowserSession, "__init__", return_value=None):
            session = BrowserSession.__new__(BrowserSession)

            # Legacy state without backend_type
            state = {"_driver": None}

            with patch("requestium.Session.__setstate__"):
                session.__setstate__(state)

            # Should default to selenium for backward compatibility
            assert session._backend_type == "selenium"
            assert session._use_stealth is True  # Default


class TestDriverProperty:
    """Tests for the driver property with different backends."""

    def test_driver_returns_nodriver_browser(self):
        """driver property returns _browser when backend is nodriver."""
        from graftpunk.session import BrowserSession

        with patch.object(BrowserSession, "__init__", return_value=None):
            session = BrowserSession.__new__(BrowserSession)
            session._backend_type = "nodriver"
            mock_browser = MagicMock(name="nodriver_browser")
            session._backend_instance = MagicMock()
            session._backend_instance._browser = mock_browser

            assert session.driver is mock_browser

    def test_driver_returns_selenium_webdriver(self):
        """driver property returns _webdriver when backend is selenium."""
        from graftpunk.session import BrowserSession

        with patch.object(BrowserSession, "__init__", return_value=None):
            session = BrowserSession.__new__(BrowserSession)
            session._backend_type = "selenium"
            session._backend_instance = None
            mock_webdriver = MagicMock(name="selenium_webdriver")
            session._webdriver = mock_webdriver

            assert session.driver is mock_webdriver

    def test_driver_raises_when_no_selenium_webdriver(self):
        """driver property raises BrowserError when selenium webdriver not available."""
        from graftpunk.exceptions import BrowserError
        from graftpunk.session import BrowserSession

        with patch.object(BrowserSession, "__init__", return_value=None):
            session = BrowserSession.__new__(BrowserSession)
            session._backend_type = "selenium"
            session._backend_instance = None
            # No _webdriver attribute at all

            with pytest.raises(BrowserError, match="Selenium WebDriver not available"):
                _ = session.driver

    def test_driver_raises_for_nodriver_without_instance(self):
        """driver property raises BrowserError for nodriver when backend_instance is None."""
        from graftpunk.exceptions import BrowserError
        from graftpunk.session import BrowserSession

        with patch.object(BrowserSession, "__init__", return_value=None):
            session = BrowserSession.__new__(BrowserSession)
            session._backend_type = "nodriver"
            session._backend_instance = None

            with pytest.raises(BrowserError, match="Nodriver backend not initialized"):
                _ = session.driver

    def test_driver_raises_for_nodriver_browser_not_started(self):
        """driver property raises BrowserError when nodriver browser is None."""
        from graftpunk.exceptions import BrowserError
        from graftpunk.session import BrowserSession

        with patch.object(BrowserSession, "__init__", return_value=None):
            session = BrowserSession.__new__(BrowserSession)
            session._backend_type = "nodriver"
            session._backend_instance = MagicMock()
            session._backend_instance._browser = None

            with pytest.raises(BrowserError, match="Nodriver browser not started"):
                _ = session.driver


class TestQuit:
    """Tests for the quit() method."""

    def test_quit_nodriver_stops_backend(self):
        """quit() calls stop() on nodriver backend and clears instance."""
        from graftpunk.session import BrowserSession

        with patch.object(BrowserSession, "__init__", return_value=None):
            session = BrowserSession.__new__(BrowserSession)
            session._backend_type = "nodriver"
            mock_backend = MagicMock()
            session._backend_instance = mock_backend

            session.quit()

            mock_backend.stop.assert_called_once()
            assert session._backend_instance is None

    def test_quit_selenium_quits_webdriver(self):
        """quit() calls quit() on selenium webdriver and clears it."""
        from graftpunk.session import BrowserSession

        with patch.object(BrowserSession, "__init__", return_value=None):
            session = BrowserSession.__new__(BrowserSession)
            session._backend_type = "selenium"
            session._backend_instance = None
            mock_webdriver = MagicMock()
            session._webdriver = mock_webdriver

            session.quit()

            mock_webdriver.quit.assert_called_once()
            assert session._webdriver is None

    def test_quit_selenium_handles_exception(self):
        """quit() logs warning and clears webdriver when quit() raises."""
        from graftpunk.session import BrowserSession

        with patch.object(BrowserSession, "__init__", return_value=None):
            session = BrowserSession.__new__(BrowserSession)
            session._backend_type = "selenium"
            session._backend_instance = None
            mock_webdriver = MagicMock()
            mock_webdriver.quit.side_effect = RuntimeError("browser already closed")
            session._webdriver = mock_webdriver

            # Should not raise
            session.quit()

            mock_webdriver.quit.assert_called_once()
            assert session._webdriver is None

    def test_quit_no_driver_is_noop(self):
        """quit() does nothing when no backend or webdriver is present."""
        from graftpunk.session import BrowserSession

        with patch.object(BrowserSession, "__init__", return_value=None):
            session = BrowserSession.__new__(BrowserSession)
            session._backend_type = "selenium"
            session._backend_instance = None
            # No _webdriver attribute

            # Should not raise
            session.quit()

    def test_quit_nodriver_handles_stop_exception(self):
        """quit() logs warning and clears backend when nodriver stop() raises."""
        from graftpunk.session import BrowserSession

        with patch.object(BrowserSession, "__init__", return_value=None):
            session = BrowserSession.__new__(BrowserSession)
            session._backend_type = "nodriver"
            mock_backend = MagicMock()
            mock_backend.stop.side_effect = RuntimeError("browser already closed")
            session._backend_instance = mock_backend

            # Should not raise
            session.quit()

            mock_backend.stop.assert_called_once()
            assert session._backend_instance is None


class TestGetStateNodriverPath:
    """Tests for __getstate__ following the nodriver-specific path (lines 285-299)."""

    def test_getstate_nodriver_returns_minimal_state(self):
        """__getstate__ for nodriver returns cookies, headers, session_name."""
        from graftpunk.session import BrowserSession

        with patch.object(BrowserSession, "__init__", return_value=None):
            session = BrowserSession.__new__(BrowserSession)
            session._backend_type = "nodriver"
            session._use_stealth = False
            session._session_name = "my-session"

            # Set up real requests.Session-like cookies and headers
            import requests

            requests.Session.__init__(session)
            session.cookies.set("token", "abc123")
            session.headers["Authorization"] = "Bearer xyz"

            state = session.__getstate__()

            assert state["_backend_type"] == "nodriver"
            assert state["_use_stealth"] is False
            assert state["session_name"] == "my-session"
            # cookies is now a RequestsCookieJar (not a dict)
            assert hasattr(state["cookies"], "set")  # it's a CookieJar
            assert state["cookies"]["token"] == "abc123"  # noqa: S105
            assert state["headers"]["Authorization"] == "Bearer xyz"
            # Should NOT have selenium-specific keys
            assert "_driver" not in state
            assert "webdriver_path" not in state

    def test_getstate_nodriver_defaults_for_missing_attrs(self):
        """__getstate__ for nodriver handles missing cookies/headers gracefully."""
        from graftpunk.session import BrowserSession

        with patch.object(BrowserSession, "__init__", return_value=None):
            session = BrowserSession.__new__(BrowserSession)
            session._backend_type = "nodriver"
            session._use_stealth = False
            # No cookies, headers, or session_name attributes

            import requests

            requests.Session.__init__(session)

            state = session.__getstate__()

            assert state["_backend_type"] == "nodriver"
            assert state["session_name"] == "default"

    def test_getstate_nodriver_duplicate_cookie_names(self):
        """__getstate__ handles cookies with duplicate names across domains."""
        from graftpunk.session import BrowserSession

        with patch.object(BrowserSession, "__init__", return_value=None):
            session = BrowserSession.__new__(BrowserSession)
            session._backend_type = "nodriver"
            session._use_stealth = False
            session._session_name = "dup-cookies"
            session.current_url = ""

            import requests

            requests.Session.__init__(session)
            session.cookies.set("JSESSIONID", "val1", domain=".app.example.com", path="/")
            session.cookies.set("JSESSIONID", "val2", domain=".auth.example.com", path="/")
            session.cookies.set("_ga", "GA1.1.123", domain=".example.com", path="/")

            state = session.__getstate__()

            # Should not crash, and should preserve all 3 cookies
            assert len(state["cookies"]) == 3
            # Verify it round-trips through pickle
            import pickle

            pickled = pickle.dumps(state["cookies"])
            restored = pickle.loads(pickled)  # noqa: S301
            assert len(restored) == 3

    def test_getstate_nodriver_cookies_preserve_domain(self):
        """__getstate__ preserves cookie domain and path attributes."""
        from graftpunk.session import BrowserSession

        with patch.object(BrowserSession, "__init__", return_value=None):
            session = BrowserSession.__new__(BrowserSession)
            session._backend_type = "nodriver"
            session._use_stealth = False
            session._session_name = "domain-test"
            session.current_url = ""

            import requests

            requests.Session.__init__(session)
            session.cookies.set("token", "abc", domain=".example.com", path="/api")

            state = session.__getstate__()

            cookie = list(state["cookies"])[0]
            assert cookie.name == "token"
            assert cookie.value == "abc"
            assert cookie.domain == ".example.com"
            assert cookie.path == "/api"


class TestSetStateNodriverPath:
    """Tests for __setstate__ following the nodriver-specific path (lines 336-356)."""

    def test_setstate_nodriver_handles_cookiejar_directly(self):
        """__setstate__ for nodriver passes through non-dict cookies."""
        from graftpunk.session import BrowserSession

        with patch.object(BrowserSession, "__init__", return_value=None):
            session = BrowserSession.__new__(BrowserSession)

            import requests.utils

            jar = requests.utils.cookiejar_from_dict({"key": "val"})
            state = {
                "_backend_type": "nodriver",
                "_use_stealth": False,
                "cookies": jar,
                "headers": {},
                "session_name": "test",
            }

            session.__setstate__(state)

            assert session.cookies is jar

    def test_setstate_nodriver_without_optional_fields(self):
        """__setstate__ for nodriver works with minimal state."""
        from graftpunk.session import BrowserSession

        with patch.object(BrowserSession, "__init__", return_value=None):
            session = BrowserSession.__new__(BrowserSession)

            state = {
                "_backend_type": "nodriver",
                "_use_stealth": False,
            }

            session.__setstate__(state)

            assert session._backend_type == "nodriver"
            assert session._backend_instance is None
            # session_name not set since not in state
            assert not hasattr(session, "_session_name")


class TestTransferNodriverCookies:
    """Tests for async transfer_nodriver_cookies_to_session."""

    async def test_transfers_cookies_from_browser(self):
        """Transfers all cookies from nodriver browser to requests session."""
        from graftpunk.session import BrowserSession

        with patch.object(BrowserSession, "__init__", return_value=None):
            session = BrowserSession.__new__(BrowserSession)
            session._backend_type = "nodriver"

            # Initialize a real requests cookie jar
            import requests

            requests.Session.__init__(session)

            # Set up mock browser with cookies
            mock_cookie1 = MagicMock()
            mock_cookie1.name = "session_id"
            mock_cookie1.value = "abc123"
            mock_cookie1.domain = ".example.com"
            mock_cookie1.path = "/"

            mock_cookie2 = MagicMock()
            mock_cookie2.name = "csrf"
            mock_cookie2.value = "xyz789"
            mock_cookie2.domain = ".example.com"
            mock_cookie2.path = "/app"

            mock_browser = MagicMock()
            mock_browser.cookies.get_all = AsyncMock(return_value=[mock_cookie1, mock_cookie2])
            session._backend_instance = MagicMock()
            session._backend_instance._browser = mock_browser

            await session.transfer_nodriver_cookies_to_session()

            assert session.cookies.get("session_id") == "abc123"
            assert session.cookies.get("csrf") == "xyz789"

    async def test_raises_when_no_backend(self):
        """Raises BrowserError when _backend_instance is None."""
        from graftpunk.exceptions import BrowserError
        from graftpunk.session import BrowserSession

        with patch.object(BrowserSession, "__init__", return_value=None):
            session = BrowserSession.__new__(BrowserSession)
            session._backend_instance = None

            with pytest.raises(BrowserError, match="nodriver backend not initialized"):
                await session.transfer_nodriver_cookies_to_session()

    async def test_raises_when_browser_is_none(self):
        """Raises BrowserError when browser is None."""
        from graftpunk.exceptions import BrowserError
        from graftpunk.session import BrowserSession

        with patch.object(BrowserSession, "__init__", return_value=None):
            session = BrowserSession.__new__(BrowserSession)
            session._backend_instance = MagicMock()
            session._backend_instance._browser = None

            with pytest.raises(BrowserError, match="browser is None"):
                await session.transfer_nodriver_cookies_to_session()


class TestStartAsync:
    """Tests for async start_async method."""

    async def test_start_async_calls_backend(self):
        """start_async calls _start_async on backend and sets _started."""
        from graftpunk.session import BrowserSession

        with patch.object(BrowserSession, "__init__", return_value=None):
            session = BrowserSession.__new__(BrowserSession)
            session._backend_type = "nodriver"
            mock_backend = MagicMock()
            mock_backend._start_async = AsyncMock()
            mock_backend._started = False
            session._backend_instance = mock_backend

            await session.start_async()

            mock_backend._start_async.assert_awaited_once()
            assert mock_backend._started is True

    async def test_start_async_raises_on_selenium_backend(self):
        """start_async raises BrowserError when backend is not nodriver."""
        from graftpunk.exceptions import BrowserError
        from graftpunk.session import BrowserSession

        with patch.object(BrowserSession, "__init__", return_value=None):
            session = BrowserSession.__new__(BrowserSession)
            session._backend_type = "selenium"
            session._backend_instance = None

            with pytest.raises(BrowserError, match="only supported for the nodriver backend"):
                await session.start_async()

    async def test_start_async_raises_when_backend_instance_is_none(self):
        """start_async raises BrowserError when nodriver backend instance is None."""
        from graftpunk.exceptions import BrowserError
        from graftpunk.session import BrowserSession

        with patch.object(BrowserSession, "__init__", return_value=None):
            session = BrowserSession.__new__(BrowserSession)
            session._backend_type = "nodriver"
            session._backend_instance = None

            with pytest.raises(BrowserError, match="backend instance is None"):
                await session.start_async()


class TestSaveHttpieSession:
    """Tests for save_httpie_session method."""

    def test_save_httpie_session_creates_and_saves(self):
        """save_httpie_session transfers cookies and saves httpie session."""
        from graftpunk.session import BrowserSession

        with patch.object(BrowserSession, "__init__", return_value=None):
            session = BrowserSession.__new__(BrowserSession)
            session._backend_type = "selenium"
            session._session_name = "test-session"

            # Initialize real requests session for cookies
            import requests

            requests.Session.__init__(session)
            session.cookies.set("token", "abc")

            # Mock the driver for current_url
            mock_driver = MagicMock()
            mock_driver.current_url = "https://example.com/page"
            session._webdriver = mock_driver
            session._backend_instance = None

            # Mock httpie environment and session
            mock_env = MagicMock()
            mock_env.config.directory = "/tmp/httpie-test"  # noqa: S108
            mock_httpie_session = MagicMock()

            with (
                patch("httpie.context.Environment", return_value=mock_env),
                patch(
                    "httpie.sessions.get_httpie_session",
                    return_value=mock_httpie_session,
                ),
            ):
                result = session.save_httpie_session()

            from pathlib import Path

            expected = Path("/tmp/httpie-test") / "sessions" / "test-session.json"  # noqa: S108
            assert result == expected
            mock_httpie_session.load.assert_called_once()
            mock_httpie_session.save.assert_called_once()

    def test_save_httpie_session_uses_provided_name(self):
        """save_httpie_session uses the provided session_name argument."""
        from graftpunk.session import BrowserSession

        with patch.object(BrowserSession, "__init__", return_value=None):
            session = BrowserSession.__new__(BrowserSession)
            session._backend_type = "selenium"
            session._backend_instance = None

            import requests

            requests.Session.__init__(session)

            session.current_url = "https://example.com"
            session._webdriver = MagicMock()
            session._webdriver.current_url = "https://example.com"

            mock_env = MagicMock()
            mock_env.config.directory = "/tmp/httpie-test"  # noqa: S108
            mock_httpie_session = MagicMock()

            with (
                patch("httpie.context.Environment", return_value=mock_env),
                patch(
                    "httpie.sessions.get_httpie_session",
                    return_value=mock_httpie_session,
                ),
            ):
                session.save_httpie_session(session_name="custom-name")

            # Verify it used the custom name (the path contains it)
            mock_httpie_session.load.assert_called_once()
            mock_httpie_session.save.assert_called_once()


class TestInitNodriverBackend:
    """Tests for __init__ when backend='nodriver' (lines 104-123)."""

    def test_init_nodriver_backend_success(self):
        """Nodriver backend init calls get_backend and requests.Session.__init__."""
        from graftpunk.session import BrowserSession

        mock_backend = MagicMock()
        with (
            patch("graftpunk.backends.get_backend", return_value=mock_backend) as mock_get,
            patch("graftpunk.backends.list_backends", return_value=["selenium", "nodriver"]),
            patch("requests.Session.__init__"),
        ):
            session = BrowserSession(backend="nodriver", headless=False)

        mock_get.assert_called_once_with("nodriver", headless=False)
        assert session._backend_type == "nodriver"
        assert session._backend_instance is mock_backend

    def test_init_nodriver_backend_headless_default(self):
        """Nodriver backend defaults to headless=True."""
        from graftpunk.session import BrowserSession

        mock_backend = MagicMock()
        with (
            patch("graftpunk.backends.get_backend", return_value=mock_backend) as mock_get,
            patch("graftpunk.backends.list_backends", return_value=["selenium", "nodriver"]),
            patch("requests.Session.__init__"),
        ):
            session = BrowserSession(backend="nodriver")

        mock_get.assert_called_once_with("nodriver", headless=True)
        assert session._backend_type == "nodriver"

    def test_init_nodriver_backend_failure_raises_browser_error(self):
        """Nodriver backend init wraps exceptions in BrowserError."""
        from graftpunk.exceptions import BrowserError
        from graftpunk.session import BrowserSession

        with (
            patch(
                "graftpunk.backends.get_backend",
                side_effect=RuntimeError("chrome not found"),
            ),
            patch("graftpunk.backends.list_backends", return_value=["selenium", "nodriver"]),
            pytest.raises(BrowserError, match="Failed to create nodriver browser session"),
        ):
            BrowserSession(backend="nodriver")

    def test_init_nodriver_sets_use_stealth(self):
        """Nodriver backend preserves use_stealth setting."""
        from graftpunk.session import BrowserSession

        mock_backend = MagicMock()
        with (
            patch("graftpunk.backends.get_backend", return_value=mock_backend),
            patch("graftpunk.backends.list_backends", return_value=["selenium", "nodriver"]),
            patch("requests.Session.__init__"),
        ):
            session = BrowserSession(backend="nodriver", use_stealth=False)

        assert session._use_stealth is False


class TestInitStealthBackend:
    """Tests for __init__ when backend='selenium' with use_stealth=True (lines 124-145)."""

    def test_init_stealth_backend_success(self):
        """Stealth backend creates driver and initializes requestium session."""
        from graftpunk.session import BrowserSession

        mock_stealth_driver = MagicMock()
        with (
            patch("graftpunk.backends.list_backends", return_value=["selenium", "nodriver"]),
            patch(
                "graftpunk.stealth.create_stealth_driver",
                return_value=mock_stealth_driver,
            ) as mock_create,
            patch("requestium.Session.__init__"),
        ):
            session = BrowserSession(backend="selenium", headless=True, use_stealth=True)

        mock_create.assert_called_once_with(headless=True)
        assert session._backend_type == "selenium"
        assert session._use_stealth is True
        assert session._webdriver is mock_stealth_driver

    def test_init_stealth_backend_webdriver_error_raises_browser_error(self):
        """Stealth backend wraps WebDriverException in BrowserError."""
        import selenium.common.exceptions

        from graftpunk.exceptions import BrowserError
        from graftpunk.session import BrowserSession

        with (
            patch("graftpunk.backends.list_backends", return_value=["selenium", "nodriver"]),
            patch(
                "graftpunk.stealth.create_stealth_driver",
                side_effect=selenium.common.exceptions.WebDriverException("driver failed"),
            ),
            pytest.raises(BrowserError, match="Failed to create stealth browser session"),
        ):
            BrowserSession(backend="selenium", use_stealth=True)

    def test_init_stealth_backend_os_error_raises_browser_error(self):
        """Stealth backend wraps OSError in BrowserError."""
        from graftpunk.exceptions import BrowserError
        from graftpunk.session import BrowserSession

        with (
            patch("graftpunk.backends.list_backends", return_value=["selenium", "nodriver"]),
            patch(
                "graftpunk.stealth.create_stealth_driver",
                side_effect=OSError("binary not found"),
            ),
            pytest.raises(BrowserError, match="Failed to create stealth browser session"),
        ):
            BrowserSession(backend="selenium", use_stealth=True)


class TestInitStandardBackend:
    """Tests for __init__ when backend='selenium' with use_stealth=False (lines 146-178)."""

    def test_init_standard_backend_success(self):
        """Standard backend detects chrome version and calls super().__init__."""
        from graftpunk.session import BrowserSession

        with (
            patch("graftpunk.backends.list_backends", return_value=["selenium", "nodriver"]),
            patch("graftpunk.session.get_chrome_version", return_value="120"),
            patch(
                "webdriver_manager.chrome.ChromeDriverManager.install",
                return_value="/usr/bin/chromedriver",
            ),
            patch("requestium.Session.__init__") as mock_super_init,
        ):
            session = BrowserSession(backend="selenium", headless=True, use_stealth=False)

        assert session._backend_type == "selenium"
        assert session._use_stealth is False
        mock_super_init.assert_called_once()
        # Verify headless argument was passed through
        call_kwargs = mock_super_init.call_args[1]
        assert "headless" in call_kwargs["webdriver_options"]["arguments"]
        assert "window-size=800,600" in call_kwargs["webdriver_options"]["arguments"]

    def test_init_standard_backend_chrome_version_failure(self):
        """Standard backend raises BrowserError when chrome version detection fails."""
        from graftpunk.exceptions import BrowserError, ChromeDriverError
        from graftpunk.session import BrowserSession

        with (
            patch("graftpunk.backends.list_backends", return_value=["selenium", "nodriver"]),
            patch(
                "graftpunk.session.get_chrome_version",
                side_effect=ChromeDriverError("no chrome"),
            ),
            pytest.raises(BrowserError, match="Failed to detect Chrome version"),
        ):
            BrowserSession(backend="selenium", use_stealth=False)

    def test_init_standard_backend_session_not_created_error(self):
        """Standard backend raises BrowserError on SessionNotCreatedException."""
        import selenium.common.exceptions

        from graftpunk.exceptions import BrowserError
        from graftpunk.session import BrowserSession

        with (
            patch("graftpunk.backends.list_backends", return_value=["selenium", "nodriver"]),
            patch("graftpunk.session.get_chrome_version", return_value="120"),
            patch(
                "webdriver_manager.chrome.ChromeDriverManager.install",
                return_value="/usr/bin/chromedriver",
            ),
            patch(
                "requestium.Session.__init__",
                side_effect=selenium.common.exceptions.SessionNotCreatedException("fail"),
            ),
            pytest.raises(BrowserError, match="Failed to create browser session"),
        ):
            BrowserSession(backend="selenium", headless=False, use_stealth=False)

    def test_init_standard_backend_no_headless(self):
        """Standard backend omits headless argument when headless=False."""
        from graftpunk.session import BrowserSession

        with (
            patch("graftpunk.backends.list_backends", return_value=["selenium", "nodriver"]),
            patch("graftpunk.session.get_chrome_version", return_value="120"),
            patch(
                "webdriver_manager.chrome.ChromeDriverManager.install",
                return_value="/usr/bin/chromedriver",
            ),
            patch("requestium.Session.__init__") as mock_super_init,
        ):
            BrowserSession(backend="selenium", headless=False, use_stealth=False)

        call_kwargs = mock_super_init.call_args[1]
        assert "headless" not in call_kwargs["webdriver_options"]["arguments"]
        assert "window-size=800,600" in call_kwargs["webdriver_options"]["arguments"]


class TestSessionNameUncached:
    """Tests for session_name getter when _session_name is not set (lines 188-189)."""

    def test_session_name_getter_from_driver_title(self):
        """session_name computes slugified title from driver when not cached."""
        from graftpunk.session import BrowserSession

        with patch.object(BrowserSession, "__init__", return_value=None):
            session = BrowserSession.__new__(BrowserSession)
            mock_driver = MagicMock()
            mock_driver.current_url = "https://example.com/page"
            mock_driver.title = "My Cool Page"
            session._webdriver = mock_driver
            session._backend_type = "selenium"
            session._backend_instance = None
            # Don't set _session_name — force the getter to compute it

            name = session.session_name
            assert name == "my-cool-page"

    def test_session_name_getter_falls_back_to_hostname(self):
        """session_name uses hostname when title is empty."""
        from graftpunk.session import BrowserSession

        with patch.object(BrowserSession, "__init__", return_value=None):
            session = BrowserSession.__new__(BrowserSession)
            mock_driver = MagicMock()
            mock_driver.current_url = "https://example.com/page"
            mock_driver.title = ""
            session._webdriver = mock_driver
            session._backend_type = "selenium"
            session._backend_instance = None

            name = session.session_name
            assert name == "example.com"

    def test_session_name_getter_falls_back_to_default(self):
        """session_name returns 'default' when title and hostname are empty."""
        from graftpunk.session import BrowserSession

        with patch.object(BrowserSession, "__init__", return_value=None):
            session = BrowserSession.__new__(BrowserSession)
            mock_driver = MagicMock()
            mock_driver.current_url = ""
            mock_driver.title = ""
            session._webdriver = mock_driver
            session._backend_type = "selenium"
            session._backend_instance = None

            name = session.session_name
            assert name == "default"


class TestGetStateSeleniumPath:
    """Tests for __getstate__ selenium path (lines 302-321)."""

    def test_getstate_selenium_includes_all_fields(self):
        """__getstate__ for selenium includes driver state and session fields."""
        from graftpunk.session import BrowserSession

        with patch.object(BrowserSession, "__init__", return_value=None):
            session = BrowserSession.__new__(BrowserSession)
            session._backend_type = "selenium"
            session._use_stealth = True
            session._driver_initializer = None
            session.webdriver_path = "/usr/bin/chromedriver"
            session.default_timeout = 15
            session.webdriver_options = {"arguments": []}
            session._last_requests_url = "https://example.com"
            session.cookies = MagicMock()
            session.headers = {"User-Agent": "test"}
            session.auth = None
            session.proxies = {}
            session.hooks = {}
            session.params = {}
            session.verify = True
            session._session_name = "test-session"
            session._backend_instance = None

            mock_driver = MagicMock()
            mock_driver.current_url = "https://example.com/dashboard"
            session._driver = mock_driver
            session._webdriver = mock_driver

            with patch("requestium.Session.__getstate__", return_value={}):
                state = session.__getstate__()

            assert state["_backend_type"] == "selenium"
            assert state["_use_stealth"] is True
            assert state["session_name"] == "test-session"
            assert state["current_url"] == "https://example.com/dashboard"
            assert state["_driver"] is None  # Driver is not serialized
            assert state["webdriver_path"] == "/usr/bin/chromedriver"
            assert state["default_timeout"] == 15
            assert state["headers"] == {"User-Agent": "test"}
            assert state["verify"] is True

    def test_getstate_selenium_computes_session_name_if_not_set(self):
        """__getstate__ for selenium triggers session_name computation."""
        from graftpunk.session import BrowserSession

        with patch.object(BrowserSession, "__init__", return_value=None):
            session = BrowserSession.__new__(BrowserSession)
            session._backend_type = "selenium"
            session._use_stealth = True
            session._driver_initializer = None
            session.webdriver_path = None
            session.default_timeout = 15
            session.webdriver_options = {}
            session._last_requests_url = None
            session.cookies = MagicMock()
            session.headers = {}
            session.auth = None
            session.proxies = {}
            session.hooks = {}
            session.params = {}
            session.verify = True
            session._backend_instance = None

            mock_driver = MagicMock()
            mock_driver.current_url = "https://example.com/page"
            mock_driver.title = "Example Page"
            session._driver = mock_driver
            session._webdriver = mock_driver
            # Note: _session_name not set, so session_name getter will compute it

            with patch("requestium.Session.__getstate__", return_value={}):
                state = session.__getstate__()

            assert state["session_name"] == "example-page"


class TestSetStateSeleniumPath:
    """Tests for __setstate__ selenium path (lines 358-373)."""

    def test_setstate_selenium_no_driver_skips_cookie_transfer(self):
        """__setstate__ for selenium skips cookie transfer when _driver is None."""
        from graftpunk.session import BrowserSession

        with patch.object(BrowserSession, "__init__", return_value=None):
            session = BrowserSession.__new__(BrowserSession)

            state = {
                "_backend_type": "selenium",
                "_use_stealth": True,
                "_driver": None,
                "cookies": {},
                "headers": {},
            }

            with patch("requestium.Session.__setstate__"):
                session.__setstate__(state)

            assert session._backend_type == "selenium"
            assert session._use_stealth is True
            # _driver should be None (no cookie transfer attempted)
            assert session._driver is None

    def test_setstate_selenium_with_driver_transfers_cookies(self):
        """__setstate__ for selenium transfers cookies when _driver is present."""
        from graftpunk.session import BrowserSession

        with patch.object(BrowserSession, "__init__", return_value=None):
            session = BrowserSession.__new__(BrowserSession)

            mock_driver = MagicMock()
            state = {
                "_backend_type": "selenium",
                "_use_stealth": True,
                "_driver": mock_driver,
            }

            with (
                patch("requestium.Session.__setstate__"),
                patch.object(BrowserSession, "transfer_session_cookies_to_driver") as mock_transfer,
            ):
                session.__setstate__(state)

            mock_transfer.assert_called_once()

    def test_setstate_selenium_cookie_transfer_failure_raises_browser_error(self):
        """__setstate__ for selenium raises BrowserError on cookie transfer failure."""
        import selenium.common.exceptions

        from graftpunk.exceptions import BrowserError
        from graftpunk.session import BrowserSession

        with patch.object(BrowserSession, "__init__", return_value=None):
            session = BrowserSession.__new__(BrowserSession)

            mock_driver = MagicMock()
            state = {
                "_backend_type": "selenium",
                "_use_stealth": True,
                "_driver": mock_driver,
            }

            with (
                patch("requestium.Session.__setstate__"),
                patch.object(
                    BrowserSession,
                    "transfer_session_cookies_to_driver",
                    side_effect=selenium.common.exceptions.SessionNotCreatedException(
                        "session expired"
                    ),
                ),
                pytest.raises(BrowserError, match="Failed to restore session"),
            ):
                session.__setstate__(state)


class TestBrowserSessionContextManager:
    """Tests for context manager protocol (__enter__/__exit__, __aenter__/__aexit__)."""

    def test_sync_context_manager_returns_self(self):
        """'with BrowserSession(...)' returns the session."""
        from graftpunk.session import BrowserSession

        with patch.object(BrowserSession, "__init__", return_value=None):
            session = BrowserSession.__new__(BrowserSession)
            session._backend_type = "selenium"
            session._observe_mode = "off"
            session._capture = None
            session._observe_storage = None
            with patch.object(session, "quit") as mock_quit:
                with session as s:
                    assert s is session
                mock_quit.assert_called_once()

    def test_sync_context_manager_quits_on_exception(self):
        """'with BrowserSession(...)' calls quit() even when exception raised."""
        from graftpunk.session import BrowserSession

        with patch.object(BrowserSession, "__init__", return_value=None):
            session = BrowserSession.__new__(BrowserSession)
            session._backend_type = "selenium"
            session._observe_mode = "off"
            session._capture = None
            session._observe_storage = None
            with patch.object(session, "quit") as mock_quit:
                with pytest.raises(ValueError, match="test"), session:
                    raise ValueError("test")
                mock_quit.assert_called_once()

    @pytest.mark.asyncio
    async def test_async_context_manager_starts_and_quits(self):
        """'async with BrowserSession(...)' calls start_async and _quit_async."""
        from graftpunk.session import BrowserSession

        with patch.object(BrowserSession, "__init__", return_value=None):
            session = BrowserSession.__new__(BrowserSession)
            session._backend_type = "nodriver"
            session._backend_instance = None
            session._observe_mode = "off"
            session._capture = None
            session._observe_storage = None
            with (
                patch.object(session, "start_async", new_callable=AsyncMock) as mock_start,
                patch.object(session, "_quit_async", new_callable=AsyncMock) as mock_quit,
            ):
                async with session as s:
                    assert s is session
                mock_start.assert_called_once()
                mock_quit.assert_called_once()

    @pytest.mark.asyncio
    async def test_async_context_manager_quits_on_exception(self):
        """'async with BrowserSession(...)' calls _quit_async() even when exception raised."""
        from graftpunk.session import BrowserSession

        with patch.object(BrowserSession, "__init__", return_value=None):
            session = BrowserSession.__new__(BrowserSession)
            session._backend_type = "nodriver"
            session._backend_instance = None
            session._observe_mode = "off"
            session._capture = None
            session._observe_storage = None
            with (
                patch.object(session, "start_async", new_callable=AsyncMock),
                patch.object(session, "_quit_async", new_callable=AsyncMock) as mock_quit,
            ):
                with pytest.raises(RuntimeError, match="test"):
                    async with session:
                        raise RuntimeError("test")
                mock_quit.assert_called_once()


class TestObservabilityWiring:
    """Tests for observability wiring in BrowserSession context managers."""

    def _make_session(self, observe_mode: str = "off") -> Any:
        """Create a BrowserSession with patched __init__ for testing."""
        from graftpunk.session import BrowserSession

        with patch.object(BrowserSession, "__init__", return_value=None):
            session = BrowserSession.__new__(BrowserSession)
            session._backend_type = "selenium"
            session._backend_instance = None
            session._observe_mode = observe_mode
            session._capture = None
            session._observe_storage = None
            session._session_name = "test-session"
            mock_driver = MagicMock()
            session._webdriver = mock_driver
            return session

    def test_observe_mode_defaults_to_off(self):
        """BrowserSession stores observe_mode attribute."""
        session = self._make_session("off")
        assert session._observe_mode == "off"

    def test_enter_starts_capture_when_observe_full(self):
        """__enter__ creates capture backend and starts capture in full mode."""
        session = self._make_session("full")
        mock_capture = MagicMock()
        mock_storage = MagicMock()

        with (
            patch("graftpunk.session.create_capture_backend", return_value=mock_capture),
            patch("graftpunk.session.ObserveStorage", return_value=mock_storage),
            patch.object(session, "quit"),
        ):
            session.__enter__()
            session.__exit__(None, None, None)

        mock_capture.start_capture.assert_called_once()

    def test_enter_starts_capture_when_observe_errors(self):
        """__enter__ creates capture backend in errors mode."""
        session = self._make_session("errors")
        mock_capture = MagicMock()
        mock_storage = MagicMock()

        with (
            patch("graftpunk.session.create_capture_backend", return_value=mock_capture),
            patch("graftpunk.session.ObserveStorage", return_value=mock_storage),
            patch.object(session, "quit"),
        ):
            session.__enter__()
            session.__exit__(None, None, None)

        mock_capture.start_capture.assert_called_once()

    def test_enter_skips_capture_when_observe_off(self):
        """__enter__ does not create capture when observe_mode is off."""
        session = self._make_session("off")

        with patch.object(session, "quit"):
            s = session.__enter__()
            assert s._capture is None
            assert s._observe_storage is None
            session.__exit__(None, None, None)

    def test_exit_flushes_data_on_normal_exit(self):
        """__exit__ writes HAR and console logs on normal exit."""
        session = self._make_session("full")
        mock_capture = MagicMock()
        mock_capture.get_har_entries.return_value = [{"entry": 1}]
        mock_capture.get_console_logs.return_value = [{"log": "hello"}]
        mock_storage = MagicMock()

        with (
            patch("graftpunk.session.create_capture_backend", return_value=mock_capture),
            patch("graftpunk.session.ObserveStorage", return_value=mock_storage),
            patch.object(session, "quit"),
        ):
            session.__enter__()
            session.__exit__(None, None, None)

        mock_capture.stop_capture.assert_called_once()
        mock_storage.write_har.assert_called_once_with([{"entry": 1}])
        mock_storage.write_console_logs.assert_called_once_with([{"log": "hello"}])

    def test_exit_takes_error_screenshot_on_exception(self):
        """__exit__ takes error screenshot when exception occurs."""
        session = self._make_session("full")
        mock_capture = MagicMock()
        mock_capture.take_screenshot_sync.return_value = b"png-data"
        mock_capture.get_har_entries.return_value = []
        mock_capture.get_console_logs.return_value = []
        mock_storage = MagicMock()

        with (
            patch("graftpunk.session.create_capture_backend", return_value=mock_capture),
            patch("graftpunk.session.ObserveStorage", return_value=mock_storage),
            patch.object(session, "quit"),
        ):
            session.__enter__()
            session.__exit__(ValueError, ValueError("boom"), None)

        mock_capture.take_screenshot_sync.assert_called_once()
        mock_storage.save_screenshot.assert_called_once()

    def test_enter_skips_capture_when_driver_is_none(self):
        """__enter__ skips capture setup when driver is None."""
        session = self._make_session("full")
        session._webdriver = None  # No driver

        with patch.object(session, "quit"):
            s = session.__enter__()
            assert s._capture is None
            session.__exit__(None, None, None)

    @pytest.mark.asyncio
    async def test_async_enter_starts_capture_when_observe_full(self):
        """__aenter__ creates capture backend and starts capture in full mode."""
        session = self._make_session("full")
        mock_capture = MagicMock()
        mock_storage = MagicMock()

        with (
            patch("graftpunk.session.create_capture_backend", return_value=mock_capture),
            patch("graftpunk.session.ObserveStorage", return_value=mock_storage),
            patch.object(session, "start_async", new_callable=AsyncMock),
            patch.object(session, "quit"),
        ):
            await session.__aenter__()
            await session.__aexit__(None, None, None)

        mock_capture.start_capture.assert_called_once()

    @pytest.mark.asyncio
    async def test_async_exit_flushes_data(self):
        """__aexit__ writes HAR and console logs via async stop."""
        session = self._make_session("full")
        mock_capture = MagicMock()
        mock_capture.stop_capture_async = AsyncMock()
        mock_capture.get_har_entries.return_value = [{"e": 1}]
        mock_capture.get_console_logs.return_value = [{"l": 1}]
        mock_storage = MagicMock()

        with (
            patch("graftpunk.session.create_capture_backend", return_value=mock_capture),
            patch("graftpunk.session.ObserveStorage", return_value=mock_storage),
            patch.object(session, "start_async", new_callable=AsyncMock),
            patch.object(session, "quit"),
        ):
            await session.__aenter__()
            await session.__aexit__(None, None, None)

        mock_capture.stop_capture_async.assert_called_once()
        mock_storage.write_har.assert_called_once()
        mock_storage.write_console_logs.assert_called_once()

    @pytest.mark.asyncio
    async def test_async_exit_takes_error_screenshot_on_exception(self):
        """__aexit__ takes error screenshot when exception occurs."""
        session = self._make_session("full")
        mock_capture = MagicMock()
        mock_capture.take_screenshot_sync.return_value = b"png"
        mock_capture.get_har_entries.return_value = []
        mock_capture.get_console_logs.return_value = []
        mock_storage = MagicMock()

        with (
            patch("graftpunk.session.create_capture_backend", return_value=mock_capture),
            patch("graftpunk.session.ObserveStorage", return_value=mock_storage),
            patch.object(session, "start_async", new_callable=AsyncMock),
            patch.object(session, "quit"),
        ):
            await session.__aenter__()
            await session.__aexit__(RuntimeError, RuntimeError("async-boom"), None)

        mock_capture.take_screenshot_sync.assert_called_once()
        mock_storage.save_screenshot.assert_called_once()


class TestStopObserveErrorHandling:
    """Tests for _stop_observe error handling."""

    def _make_session(self, observe_mode: str = "full") -> Any:
        """Create a BrowserSession with patched __init__ for testing."""
        from graftpunk.session import BrowserSession

        with patch.object(BrowserSession, "__init__", return_value=None):
            session = BrowserSession.__new__(BrowserSession)
            session._backend_type = "selenium"
            session._backend_instance = None
            session._observe_mode = observe_mode
            session._capture = None
            session._observe_storage = None
            session._session_name = "test-session"
            mock_driver = MagicMock()
            session._webdriver = mock_driver
            return session

    def test_stop_observe_does_not_propagate_exceptions(self):
        """_stop_observe catches exceptions so they don't mask the original error."""
        session = self._make_session("full")
        mock_capture = MagicMock()
        mock_capture.stop_capture.side_effect = RuntimeError("capture cleanup failed")
        session._capture = mock_capture
        session._observe_storage = MagicMock()

        # Should not raise
        session._stop_observe(exc_type=None)

    def test_stop_observe_does_not_propagate_screenshot_exceptions(self):
        """_stop_observe catches exceptions during error screenshot."""
        session = self._make_session("full")
        mock_capture = MagicMock()
        mock_capture.take_screenshot_sync.side_effect = RuntimeError("screenshot failed")
        session._capture = mock_capture
        session._observe_storage = MagicMock()

        # Should not raise even when screenshot fails during error path
        session._stop_observe(exc_type=ValueError)

    def test_stop_observe_logs_when_screenshot_unavailable(self):
        """_stop_observe logs warning when error screenshot returns None."""
        session = self._make_session("full")
        mock_capture = MagicMock()
        mock_capture.take_screenshot_sync.return_value = None
        mock_capture.get_har_entries.return_value = []
        mock_capture.get_console_logs.return_value = []
        session._capture = mock_capture
        session._observe_storage = MagicMock()

        # Should not raise; screenshot is None so save_screenshot should not be called
        session._stop_observe(exc_type=ValueError)

        mock_capture.take_screenshot_sync.assert_called_once()
        session._observe_storage.save_screenshot.assert_not_called()

    def test_stop_observe_noop_when_no_capture(self):
        """_stop_observe returns immediately when _capture is None."""
        session = self._make_session("full")
        session._capture = None

        # Should not raise
        session._stop_observe(exc_type=ValueError)


class TestStopObserveAsync:
    """Tests for _stop_observe_async method."""

    def _make_session(self, observe_mode: str = "full") -> Any:
        """Create a BrowserSession with patched __init__ for testing."""
        from graftpunk.session import BrowserSession

        with patch.object(BrowserSession, "__init__", return_value=None):
            session = BrowserSession.__new__(BrowserSession)
            session._backend_type = "nodriver"
            session._backend_instance = None
            session._observe_mode = observe_mode
            session._capture = None
            session._observe_storage = None
            session._session_name = "test-session"
            return session

    @pytest.mark.asyncio
    async def test_stop_observe_async_calls_stop_capture_async(self):
        """_stop_observe_async calls stop_capture_async instead of stop_capture."""
        session = self._make_session("full")
        mock_capture = MagicMock()
        mock_capture.stop_capture_async = AsyncMock()
        mock_capture.get_har_entries.return_value = [{"entry": 1}]
        mock_capture.get_console_logs.return_value = [{"log": "hello"}]
        mock_storage = MagicMock()
        session._capture = mock_capture
        session._observe_storage = mock_storage

        await session._stop_observe_async(exc_type=None)

        mock_capture.stop_capture_async.assert_awaited_once()
        mock_storage.write_har.assert_called_once_with([{"entry": 1}])
        mock_storage.write_console_logs.assert_called_once_with([{"log": "hello"}])

    @pytest.mark.asyncio
    async def test_stop_observe_async_noop_when_no_capture(self):
        """_stop_observe_async returns immediately when _capture is None."""
        session = self._make_session("full")
        session._capture = None

        # Should not raise
        await session._stop_observe_async(exc_type=None)

    @pytest.mark.asyncio
    async def test_stop_observe_async_takes_error_screenshot(self):
        """_stop_observe_async takes screenshot on error."""
        session = self._make_session("full")
        mock_capture = MagicMock()
        mock_capture.stop_capture_async = AsyncMock()
        mock_capture.take_screenshot_sync.return_value = b"png-data"
        mock_capture.get_har_entries.return_value = []
        mock_capture.get_console_logs.return_value = []
        mock_storage = MagicMock()
        session._capture = mock_capture
        session._observe_storage = mock_storage

        await session._stop_observe_async(exc_type=ValueError)

        mock_capture.take_screenshot_sync.assert_called_once()
        mock_storage.save_screenshot.assert_called_once()

    @pytest.mark.asyncio
    async def test_stop_observe_async_handles_stop_capture_error(self):
        """_stop_observe_async catches errors from stop_capture_async."""
        session = self._make_session("full")
        mock_capture = MagicMock()
        mock_capture.stop_capture_async = AsyncMock(side_effect=RuntimeError("cdp error"))
        mock_capture.get_har_entries.return_value = []
        mock_capture.get_console_logs.return_value = []
        mock_storage = MagicMock()
        session._capture = mock_capture
        session._observe_storage = mock_storage

        # Should not raise
        await session._stop_observe_async(exc_type=None)

        # HAR and console logs should still be written
        mock_storage.write_har.assert_called_once()
        mock_storage.write_console_logs.assert_called_once()

    @pytest.mark.asyncio
    async def test_aexit_calls_stop_observe_async(self):
        """__aexit__ calls _stop_observe_async instead of _stop_observe."""
        session = self._make_session("full")
        mock_capture = MagicMock()
        mock_capture.stop_capture_async = AsyncMock()
        mock_capture.get_har_entries.return_value = []
        mock_capture.get_console_logs.return_value = []
        mock_storage = MagicMock()
        session._capture = mock_capture
        session._observe_storage = mock_storage

        with patch.object(session, "quit"):
            await session.__aexit__(None, None, None)

        mock_capture.stop_capture_async.assert_awaited_once()


class TestHeaderProfilesSerialization:
    """Tests for _gp_header_profiles roundtrip through __getstate__/__setstate__."""

    def test_getstate_includes_gp_header_profiles_nodriver(self):
        """__getstate__ should include _gp_header_profiles for nodriver sessions."""
        from graftpunk.session import BrowserSession

        with patch.object(BrowserSession, "__init__", return_value=None):
            session = BrowserSession.__new__(BrowserSession)
            session._backend_type = "nodriver"
            session._use_stealth = False
            session._session_name = "test"
            session.current_url = ""

            import requests

            requests.Session.__init__(session)

            session._gp_header_profiles = {"navigation": {"User-Agent": "Test"}}
            state = session.__getstate__()
            assert state.get("_gp_header_profiles") == {"navigation": {"User-Agent": "Test"}}

    def test_getstate_default_empty_profiles_nodriver(self):
        """__getstate__ returns empty dict when no profiles set."""
        from graftpunk.session import BrowserSession

        with patch.object(BrowserSession, "__init__", return_value=None):
            session = BrowserSession.__new__(BrowserSession)
            session._backend_type = "nodriver"
            session._use_stealth = False
            session._session_name = "test"
            session.current_url = ""

            import requests

            requests.Session.__init__(session)

            state = session.__getstate__()
            assert state.get("_gp_header_profiles") == {}

    def test_setstate_restores_gp_header_profiles_nodriver(self):
        """__setstate__ should restore _gp_header_profiles for nodriver."""
        from graftpunk.session import BrowserSession

        with patch.object(BrowserSession, "__init__", return_value=None):
            session = BrowserSession.__new__(BrowserSession)
            session._backend_type = "nodriver"
            session._use_stealth = False
            session._session_name = "test"
            session.current_url = ""

            import requests

            requests.Session.__init__(session)

            profiles = {"xhr": {"Accept": "application/json"}}
            session._gp_header_profiles = profiles
            state = session.__getstate__()

            # Create a new session and restore state
            new_session = requests.Session.__new__(type(session))
            new_session.__setstate__(state)
            assert getattr(new_session, "_gp_header_profiles", None) == profiles

    def test_getstate_includes_gp_header_profiles_selenium(self):
        """__getstate__ should include _gp_header_profiles for selenium sessions."""
        from graftpunk.session import BrowserSession

        with patch.object(BrowserSession, "__init__", return_value=None):
            session = BrowserSession.__new__(BrowserSession)
            session._backend_type = "selenium"
            session._use_stealth = True
            session._driver_initializer = None
            session.webdriver_path = None
            session.default_timeout = 15
            session.webdriver_options = {}
            session._last_requests_url = None
            session.cookies = MagicMock()
            session.headers = {}
            session.auth = None
            session.proxies = {}
            session.hooks = {}
            session.params = {}
            session.verify = True
            session._session_name = "test"
            session._backend_instance = None

            mock_driver = MagicMock()
            mock_driver.current_url = "https://example.com"
            session._driver = mock_driver
            session._webdriver = mock_driver

            session._gp_header_profiles = {"navigation": {"Accept": "text/html"}}

            with patch("requestium.Session.__getstate__", return_value={}):
                state = session.__getstate__()

            assert state.get("_gp_header_profiles") == {"navigation": {"Accept": "text/html"}}

    def test_setstate_restores_gp_header_profiles_selenium(self):
        """__setstate__ should restore _gp_header_profiles for selenium."""
        from graftpunk.session import BrowserSession

        with patch.object(BrowserSession, "__init__", return_value=None):
            session = BrowserSession.__new__(BrowserSession)

            profiles = {"xhr": {"Accept": "application/json"}}
            state = {
                "_backend_type": "selenium",
                "_use_stealth": True,
                "_driver": None,
                "_gp_header_profiles": profiles,
            }

            with patch("requestium.Session.__setstate__"):
                session.__setstate__(state)

            assert session._gp_header_profiles == profiles

    def test_setstate_defaults_empty_profiles_when_missing(self):
        """__setstate__ defaults to empty dict when _gp_header_profiles not in state."""
        from graftpunk.session import BrowserSession

        with patch.object(BrowserSession, "__init__", return_value=None):
            session = BrowserSession.__new__(BrowserSession)

            state = {
                "_backend_type": "nodriver",
                "_use_stealth": False,
            }

            session.__setstate__(state)

            assert session._gp_header_profiles == {}


class TestTokenCacheSerialization:
    """Tests for _gp_cached_tokens roundtrip through __getstate__/__setstate__."""

    def test_getstate_includes_token_cache_nodriver(self):
        """__getstate__ should include _gp_cached_tokens for nodriver sessions."""
        from graftpunk.session import BrowserSession

        with patch.object(BrowserSession, "__init__", return_value=None):
            session = BrowserSession.__new__(BrowserSession)
            session._backend_type = "nodriver"
            session._use_stealth = False
            session._session_name = "test"
            session.current_url = ""

            import requests

            requests.Session.__init__(session)

            cache = {"X-CSRF": {"name": "X-CSRF", "value": "abc"}}
            session._gp_cached_tokens = cache
            state = session.__getstate__()
            assert state.get("_gp_cached_tokens") == cache

    def test_setstate_restores_token_cache_nodriver(self):
        """__setstate__ should restore _gp_cached_tokens for nodriver."""
        from graftpunk.session import BrowserSession

        with patch.object(BrowserSession, "__init__", return_value=None):
            session = BrowserSession.__new__(BrowserSession)
            session._backend_type = "nodriver"
            session._use_stealth = False
            session._session_name = "test"
            session.current_url = ""

            import requests

            requests.Session.__init__(session)

            cache = {"X-CSRF": {"name": "X-CSRF", "value": "abc"}}
            session._gp_cached_tokens = cache
            state = session.__getstate__()

            new_session = requests.Session.__new__(type(session))
            new_session.__setstate__(state)
            assert getattr(new_session, "_gp_cached_tokens", None) == cache

    def test_setstate_restores_token_cache_selenium(self):
        """__setstate__ should restore _gp_cached_tokens for selenium."""
        from graftpunk.session import BrowserSession

        with patch.object(BrowserSession, "__init__", return_value=None):
            session = BrowserSession.__new__(BrowserSession)

            cache = {"X-Token": {"name": "X-Token", "value": "xyz"}}
            state = {
                "_backend_type": "selenium",
                "_use_stealth": True,
                "_driver": None,
                "_gp_cached_tokens": cache,
            }

            with patch("requestium.Session.__setstate__"):
                session.__setstate__(state)

            assert session._gp_cached_tokens == cache

    def test_setstate_defaults_empty_cache_when_missing(self):
        """__setstate__ defaults to empty dict when _gp_cached_tokens not in state."""
        from graftpunk.session import BrowserSession

        with patch.object(BrowserSession, "__init__", return_value=None):
            session = BrowserSession.__new__(BrowserSession)

            state = {
                "_backend_type": "nodriver",
                "_use_stealth": False,
            }

            session.__setstate__(state)

            assert session._gp_cached_tokens == {}


class TestInjectCookiesToNodriver:
    """Tests for inject_cookies_to_nodriver function."""

    @pytest.mark.asyncio
    async def test_injects_cookies_via_cdp(self) -> None:
        """Inject cookies from RequestsCookieJar into nodriver browser via CDP."""
        from unittest.mock import AsyncMock

        from requests.cookies import RequestsCookieJar

        from graftpunk.session import inject_cookies_to_nodriver

        jar = RequestsCookieJar()
        jar.set("session_id", "abc123", domain=".example.com", path="/")
        jar.set("csrf", "xyz", domain=".example.com", path="/")

        mock_tab = AsyncMock()
        result = await inject_cookies_to_nodriver(mock_tab, jar)
        assert result == 2
        mock_tab.send.assert_called_once()

    @pytest.mark.asyncio
    async def test_returns_zero_for_empty_jar(self) -> None:
        """Inject cookies returns 0 and doesn't call send when jar is empty."""
        from unittest.mock import AsyncMock

        from requests.cookies import RequestsCookieJar

        from graftpunk.session import inject_cookies_to_nodriver

        jar = RequestsCookieJar()
        mock_tab = AsyncMock()
        result = await inject_cookies_to_nodriver(mock_tab, jar)
        assert result == 0
        mock_tab.send.assert_not_called()
