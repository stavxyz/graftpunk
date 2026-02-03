"""Tests for keepalive handler module."""

from unittest.mock import MagicMock

import pytest
import requests

from graftpunk.keepalive.handler import (
    GenericHTTPHandler,
    KeepaliveHandler,
    SessionStatus,
)


class TestSessionStatus:
    """Tests for SessionStatus TypedDict."""

    def test_session_status_accepts_all_fields(self) -> None:
        """Test that SessionStatus accepts all documented fields."""
        status: SessionStatus = {
            "is_active": True,
            "max_age_ms": 3600000,
            "time_to_expiry_ms": 1800000,
            "message": "Session is active",
        }
        assert status["is_active"] is True
        assert status["max_age_ms"] == 3600000

    def test_session_status_partial_fields(self) -> None:
        """Test that SessionStatus works with partial fields (total=False)."""
        status: SessionStatus = {"is_active": True}
        assert status["is_active"] is True


class TestKeepaliveHandlerProtocol:
    """Tests for KeepaliveHandler protocol."""

    def test_generic_handler_is_keepalive_handler(self) -> None:
        """Test that GenericHTTPHandler satisfies the KeepaliveHandler protocol."""
        handler = GenericHTTPHandler(
            site_name="Test",
            touch_url="https://example.com/touch",
        )
        assert isinstance(handler, KeepaliveHandler)


class TestGenericHTTPHandlerInit:
    """Tests for GenericHTTPHandler initialization."""

    def test_all_parameters_stored(self) -> None:
        """Test that all constructor parameters are stored correctly."""
        handler = GenericHTTPHandler(
            site_name="My Site",
            touch_url="https://example.com/touch",
            touch_method="PUT",
            validate_url="https://example.com/validate",
            status_url="https://example.com/status",
            timeout=60,
        )
        assert handler.site_name == "My Site"
        assert handler.touch_url == "https://example.com/touch"
        assert handler.touch_method == "PUT"
        assert handler.validate_url == "https://example.com/validate"
        assert handler.status_url == "https://example.com/status"
        assert handler.timeout == 60

    def test_default_values(self) -> None:
        """Test that default values are applied correctly."""
        handler = GenericHTTPHandler(
            site_name="Test",
            touch_url="https://example.com/touch",
        )
        assert handler.touch_method == "POST"
        assert handler.validate_url is None
        assert handler.status_url is None
        assert handler.timeout == 30

    def test_touch_method_uppercased(self) -> None:
        """Test that touch_method is uppercased."""
        handler = GenericHTTPHandler(
            site_name="Test",
            touch_url="https://example.com/touch",
            touch_method="get",
        )
        assert handler.touch_method == "GET"

    def test_site_name_property(self) -> None:
        """Test site_name property returns stored value."""
        handler = GenericHTTPHandler(
            site_name="My API",
            touch_url="https://example.com/touch",
        )
        assert handler.site_name == "My API"


class TestGenericHTTPHandlerTouchSession:
    """Tests for GenericHTTPHandler.touch_session."""

    @pytest.fixture
    def handler(self) -> GenericHTTPHandler:
        """Create a handler for testing."""
        return GenericHTTPHandler(
            site_name="example",
            touch_url="https://api.example.com/keepalive",
            touch_method="POST",
            timeout=15,
        )

    @pytest.fixture
    def mock_session(self) -> MagicMock:
        """Create a mock requests.Session."""
        return MagicMock(spec=requests.Session)

    def test_success_with_full_json(
        self, handler: GenericHTTPHandler, mock_session: MagicMock
    ) -> None:
        """Test successful touch with full JSON response body."""
        mock_response = MagicMock()
        mock_response.ok = True
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "maxAge": 3600000,
            "timeToSessionExpiration": 1800000,
            "isActive": True,
            "message": "Session extended",
        }
        mock_session.request.return_value = mock_response

        success, status = handler.touch_session(mock_session)

        assert success is True
        assert status is not None
        assert status["is_active"] is True
        assert status["max_age_ms"] == 3600000
        assert status["time_to_expiry_ms"] == 1800000
        assert status["message"] == "Session extended"

        mock_session.request.assert_called_once_with(
            method="POST",
            url="https://api.example.com/keepalive",
            timeout=15,
        )

    def test_success_without_json(
        self, handler: GenericHTTPHandler, mock_session: MagicMock
    ) -> None:
        """Test successful touch when response has no JSON body."""
        mock_response = MagicMock()
        mock_response.ok = True
        mock_response.status_code = 200
        mock_response.json.side_effect = ValueError("No JSON")
        mock_session.request.return_value = mock_response

        success, status = handler.touch_session(mock_session)

        assert success is True
        assert status is not None
        assert status["is_active"] is True
        # No extra fields parsed
        assert "max_age_ms" not in status

    def test_success_with_partial_json(
        self, handler: GenericHTTPHandler, mock_session: MagicMock
    ) -> None:
        """Test successful touch with partial JSON keys."""
        mock_response = MagicMock()
        mock_response.ok = True
        mock_response.status_code = 200
        mock_response.json.return_value = {"maxAge": 7200000}
        mock_session.request.return_value = mock_response

        success, status = handler.touch_session(mock_session)

        assert success is True
        assert status is not None
        assert status["is_active"] is True
        assert status["max_age_ms"] == 7200000
        assert "time_to_expiry_ms" not in status
        assert "message" not in status

    def test_success_json_not_dict(
        self, handler: GenericHTTPHandler, mock_session: MagicMock
    ) -> None:
        """Test successful touch when JSON is not a dict (e.g., a list)."""
        mock_response = MagicMock()
        mock_response.ok = True
        mock_response.status_code = 200
        mock_response.json.return_value = ["not", "a", "dict"]
        mock_session.request.return_value = mock_response

        success, status = handler.touch_session(mock_session)

        assert success is True
        assert status is not None
        assert status["is_active"] is True
        # No extra fields since JSON wasn't a dict
        assert "max_age_ms" not in status

    def test_http_error_status(self, handler: GenericHTTPHandler, mock_session: MagicMock) -> None:
        """Test touch with non-2xx HTTP status (no exception raised)."""
        mock_response = MagicMock()
        mock_response.ok = False
        mock_response.status_code = 401
        mock_session.request.return_value = mock_response

        success, status = handler.touch_session(mock_session)

        assert success is False
        assert status is not None
        assert status["is_active"] is False

    def test_request_exception(self, handler: GenericHTTPHandler, mock_session: MagicMock) -> None:
        """Test touch when request raises RequestException."""
        mock_session.request.side_effect = requests.RequestException("Connection error")

        success, status = handler.touch_session(mock_session)

        assert success is False
        assert status is None

    def test_uses_configured_method(self, mock_session: MagicMock) -> None:
        """Test that configured HTTP method is used."""
        handler = GenericHTTPHandler(
            site_name="test",
            touch_url="https://example.com/touch",
            touch_method="PATCH",
        )
        mock_response = MagicMock()
        mock_response.ok = True
        mock_response.status_code = 200
        mock_response.json.side_effect = ValueError
        mock_session.request.return_value = mock_response

        handler.touch_session(mock_session)

        mock_session.request.assert_called_once_with(
            method="PATCH",
            url="https://example.com/touch",
            timeout=30,
        )

    def test_isactive_false_overrides_default(
        self, handler: GenericHTTPHandler, mock_session: MagicMock
    ) -> None:
        """Test that isActive=False in JSON overrides the default True."""
        mock_response = MagicMock()
        mock_response.ok = True
        mock_response.status_code = 200
        mock_response.json.return_value = {"isActive": False}
        mock_session.request.return_value = mock_response

        success, status = handler.touch_session(mock_session)

        assert success is True
        assert status is not None
        assert status["is_active"] is False


class TestGenericHTTPHandlerValidateSession:
    """Tests for GenericHTTPHandler.validate_session."""

    @pytest.fixture
    def mock_session(self) -> MagicMock:
        """Create a mock requests.Session."""
        return MagicMock(spec=requests.Session)

    def test_no_validate_url_returns_true(self, mock_session: MagicMock) -> None:
        """Test that no validate_url configured returns True immediately."""
        handler = GenericHTTPHandler(
            site_name="test",
            touch_url="https://example.com/touch",
            validate_url=None,
        )

        result = handler.validate_session(mock_session)

        assert result is True
        mock_session.get.assert_not_called()

    def test_valid_session(self, mock_session: MagicMock) -> None:
        """Test validate_session returns True for OK response."""
        handler = GenericHTTPHandler(
            site_name="test",
            touch_url="https://example.com/touch",
            validate_url="https://example.com/validate",
            timeout=10,
        )
        mock_response = MagicMock()
        mock_response.ok = True
        mock_response.status_code = 200
        mock_session.get.return_value = mock_response

        result = handler.validate_session(mock_session)

        assert result is True
        mock_session.get.assert_called_once_with(
            "https://example.com/validate",
            timeout=10,
        )

    def test_invalid_session(self, mock_session: MagicMock) -> None:
        """Test validate_session returns False for non-OK response."""
        handler = GenericHTTPHandler(
            site_name="test",
            touch_url="https://example.com/touch",
            validate_url="https://example.com/validate",
        )
        mock_response = MagicMock()
        mock_response.ok = False
        mock_response.status_code = 403
        mock_session.get.return_value = mock_response

        result = handler.validate_session(mock_session)

        assert result is False

    def test_request_exception(self, mock_session: MagicMock) -> None:
        """Test validate_session returns False on RequestException."""
        handler = GenericHTTPHandler(
            site_name="test",
            touch_url="https://example.com/touch",
            validate_url="https://example.com/validate",
        )
        mock_session.get.side_effect = requests.RequestException("Timeout")

        result = handler.validate_session(mock_session)

        assert result is False


class TestGenericHTTPHandlerGetSessionStatus:
    """Tests for GenericHTTPHandler.get_session_status."""

    @pytest.fixture
    def mock_session(self) -> MagicMock:
        """Create a mock requests.Session."""
        return MagicMock(spec=requests.Session)

    def test_no_status_url_delegates_to_touch_success(self, mock_session: MagicMock) -> None:
        """Test get_session_status with no status_url delegates to touch_session."""
        handler = GenericHTTPHandler(
            site_name="test",
            touch_url="https://example.com/touch",
            status_url=None,
        )
        mock_response = MagicMock()
        mock_response.ok = True
        mock_response.status_code = 200
        mock_response.json.return_value = {"maxAge": 5000, "isActive": True}
        mock_session.request.return_value = mock_response

        status = handler.get_session_status(mock_session)

        assert status["is_active"] is True
        assert status["max_age_ms"] == 5000

    def test_no_status_url_touch_fails(self, mock_session: MagicMock) -> None:
        """Test get_session_status when touch fails returns default status."""
        handler = GenericHTTPHandler(
            site_name="test",
            touch_url="https://example.com/touch",
            status_url=None,
        )
        mock_session.request.side_effect = requests.RequestException("fail")

        status = handler.get_session_status(mock_session)

        assert status["is_active"] is False

    def test_no_status_url_touch_success_no_json(self, mock_session: MagicMock) -> None:
        """Test get_session_status when touch succeeds but has no JSON."""
        handler = GenericHTTPHandler(
            site_name="test",
            touch_url="https://example.com/touch",
            status_url=None,
        )
        mock_response = MagicMock()
        mock_response.ok = True
        mock_response.status_code = 200
        mock_response.json.side_effect = ValueError
        mock_session.request.return_value = mock_response

        status = handler.get_session_status(mock_session)

        # touch returns (True, {"is_active": True}) since ok=True
        assert status["is_active"] is True

    def test_with_status_url_success_full_json(self, mock_session: MagicMock) -> None:
        """Test get_session_status with status_url and full JSON response."""
        handler = GenericHTTPHandler(
            site_name="test",
            touch_url="https://example.com/touch",
            status_url="https://example.com/status",
            timeout=20,
        )
        mock_response = MagicMock()
        mock_response.ok = True
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "maxAge": 3600000,
            "timeToSessionExpiration": 900000,
            "isActive": True,
        }
        mock_session.get.return_value = mock_response

        status = handler.get_session_status(mock_session)

        assert status["is_active"] is True
        assert status["max_age_ms"] == 3600000
        assert status["time_to_expiry_ms"] == 900000
        mock_session.get.assert_called_once_with(
            "https://example.com/status",
            timeout=20,
        )

    def test_with_status_url_non_ok_response(self, mock_session: MagicMock) -> None:
        """Test get_session_status with non-OK response returns default."""
        handler = GenericHTTPHandler(
            site_name="test",
            touch_url="https://example.com/touch",
            status_url="https://example.com/status",
        )
        mock_response = MagicMock()
        mock_response.ok = False
        mock_response.status_code = 500
        mock_session.get.return_value = mock_response

        status = handler.get_session_status(mock_session)

        assert status["is_active"] is False

    def test_with_status_url_request_exception(self, mock_session: MagicMock) -> None:
        """Test get_session_status with RequestException returns default."""
        handler = GenericHTTPHandler(
            site_name="test",
            touch_url="https://example.com/touch",
            status_url="https://example.com/status",
        )
        mock_session.get.side_effect = requests.RequestException("Network error")

        status = handler.get_session_status(mock_session)

        assert status["is_active"] is False

    def test_with_status_url_json_parse_error(self, mock_session: MagicMock) -> None:
        """Test get_session_status with JSON parse error still returns is_active."""
        handler = GenericHTTPHandler(
            site_name="test",
            touch_url="https://example.com/touch",
            status_url="https://example.com/status",
        )
        mock_response = MagicMock()
        mock_response.ok = True
        mock_response.status_code = 200
        mock_response.json.side_effect = ValueError("Bad JSON")
        mock_session.get.return_value = mock_response

        status = handler.get_session_status(mock_session)

        assert status["is_active"] is True
        assert "max_age_ms" not in status

    def test_with_status_url_json_not_dict(self, mock_session: MagicMock) -> None:
        """Test get_session_status when JSON response is not a dict."""
        handler = GenericHTTPHandler(
            site_name="test",
            touch_url="https://example.com/touch",
            status_url="https://example.com/status",
        )
        mock_response = MagicMock()
        mock_response.ok = True
        mock_response.status_code = 200
        mock_response.json.return_value = "just a string"
        mock_session.get.return_value = mock_response

        status = handler.get_session_status(mock_session)

        assert status["is_active"] is True
        assert "max_age_ms" not in status
