"""Tests for session module.

Note: Many BrowserSession tests require a real browser and are better suited
for integration tests. These unit tests focus on testable components that
don't require actual browser instantiation.
"""

from unittest.mock import MagicMock, patch


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
