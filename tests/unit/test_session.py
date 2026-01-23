"""Tests for session module.

Note: Many BrowserSession tests require a real browser and are better suited
for integration tests. These unit tests focus on testable components that
don't require actual browser instantiation.
"""

from unittest.mock import patch


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
