"""Tests for cache module."""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from bsc.cache import (
    SessionLike,
    _extract_session_metadata,
    _reset_session_storage_backend,
    cache_session,
    clear_session_cache,
    get_session_metadata,
    list_sessions,
    load_session,
    load_session_for_api,
    update_session_status,
)
from bsc.exceptions import SessionExpiredError, SessionNotFoundError


class TestSessionLikeProtocol:
    """Tests for SessionLike Protocol."""

    def test_session_like_protocol_with_valid_object(self):
        """Test that objects with cookies and headers are SessionLike."""
        mock_session = MagicMock()
        mock_session.cookies = {}
        mock_session.headers = {}

        assert isinstance(mock_session, SessionLike)

    def test_session_like_protocol_with_invalid_object(self):
        """Test that objects without required attrs are not SessionLike."""

        class NoAttrs:
            pass

        obj = NoAttrs()
        assert not isinstance(obj, SessionLike)


class TestExtractSessionMetadata:
    """Tests for _extract_session_metadata."""

    def test_extract_metadata_with_url(self):
        """Test extracting metadata from session with URL."""
        mock_session = MagicMock()
        mock_session.current_url = "https://example.com/dashboard"
        mock_session.cookies = []

        metadata = _extract_session_metadata(mock_session, "test-session")

        assert metadata["name"] == "test-session"
        assert metadata["current_url"] == "https://example.com/dashboard"
        assert metadata["domain"] == "example.com"
        assert metadata["cookie_count"] == 0
        assert metadata["cookie_domains"] == []

    def test_extract_metadata_with_cookies(self):
        """Test extracting metadata from session with cookies."""
        mock_cookie = MagicMock()
        mock_cookie.domain = ".example.com"

        mock_session = MagicMock()
        mock_session.current_url = "https://example.com"
        mock_session.cookies = [mock_cookie, mock_cookie]

        metadata = _extract_session_metadata(mock_session, "test")

        assert metadata["cookie_count"] == 2
        assert ".example.com" in metadata["cookie_domains"]

    def test_extract_metadata_without_url(self):
        """Test extracting metadata from session without URL."""
        mock_session = MagicMock(spec=[])

        metadata = _extract_session_metadata(mock_session, "no-url")

        assert metadata["name"] == "no-url"
        assert "current_url" not in metadata
        assert "domain" not in metadata


class SimpleSession:
    """Simple picklable session for testing."""

    def __init__(self, url: str = "https://example.com", name: str | None = None):
        self.current_url = url
        self.cookies = []
        self.headers = {}
        if name:
            self.session_name = name


class TestCacheSession:
    """Tests for cache_session function.

    Note: MagicMock objects don't serialize well with dill/pickle.
    We use a simple picklable class instead.
    """

    def setup_method(self) -> None:
        """Reset storage backend before each test."""
        _reset_session_storage_backend()

    def test_cache_session_with_name(self, tmp_path, monkeypatch):
        """Test caching a session with explicit name."""
        monkeypatch.setenv("BSC_CONFIG_DIR", str(tmp_path))
        monkeypatch.setenv("BSC_STORAGE_BACKEND", "local")
        monkeypatch.setenv("BSC_SESSION_TTL_HOURS", "24")
        _reset_session_storage_backend()

        from bsc.config import reset_settings
        from bsc.encryption import reset_encryption_key_cache

        reset_settings()
        reset_encryption_key_cache()

        session = SimpleSession()
        location = cache_session(session, "test-cache")

        assert "test-cache" in location
        assert (tmp_path / "sessions" / "test-cache").exists()

    def test_cache_session_without_name(self, tmp_path, monkeypatch):
        """Test caching a session that has a session_name attribute."""
        monkeypatch.setenv("BSC_CONFIG_DIR", str(tmp_path))
        monkeypatch.setenv("BSC_STORAGE_BACKEND", "local")
        _reset_session_storage_backend()

        from bsc.config import reset_settings
        from bsc.encryption import reset_encryption_key_cache

        reset_settings()
        reset_encryption_key_cache()

        session = SimpleSession(name="auto-named")
        location = cache_session(session)

        assert "auto-named" in location


class TestLoadSession:
    """Tests for load_session function."""

    def setup_method(self) -> None:
        """Reset storage backend before each test."""
        _reset_session_storage_backend()

    def test_load_nonexistent_session_raises_error(self, tmp_path, monkeypatch):
        """Test that loading non-existent session raises SessionNotFoundError."""
        monkeypatch.setenv("BSC_CONFIG_DIR", str(tmp_path))
        monkeypatch.setenv("BSC_STORAGE_BACKEND", "local")
        _reset_session_storage_backend()

        from bsc.config import reset_settings

        reset_settings()

        with pytest.raises(SessionNotFoundError):
            load_session("non-existent")

    def test_load_session_with_checksum_mismatch(self, tmp_path, monkeypatch):
        """Test that checksum mismatch raises SessionExpiredError."""
        monkeypatch.setenv("BSC_CONFIG_DIR", str(tmp_path))
        monkeypatch.setenv("BSC_STORAGE_BACKEND", "local")
        _reset_session_storage_backend()

        from bsc.config import reset_settings
        from bsc.encryption import reset_encryption_key_cache

        reset_settings()
        reset_encryption_key_cache()

        # Create a session directory with mismatched checksum
        session_dir = tmp_path / "sessions" / "bad-checksum"
        session_dir.mkdir(parents=True)

        # Create metadata with wrong checksum
        import json

        metadata_file = session_dir / "metadata.json"
        now = datetime.now(UTC)
        metadata_file.write_text(
            json.dumps(
                {
                    "name": "bad-checksum",
                    "checksum": "wrong-checksum",
                    "created_at": now.isoformat(),
                    "modified_at": now.isoformat(),
                    "expires_at": (now + timedelta(hours=24)).isoformat(),
                    "domain": "example.com",
                    "current_url": None,
                    "cookie_count": 0,
                    "cookie_domains": [],
                    "status": "active",
                }
            )
        )

        # Create encrypted session file with different data
        import dill as pickle

        from bsc.encryption import encrypt_data

        # Use a simple picklable session instead of MagicMock
        session = SimpleSession()
        pickled = pickle.dumps(session)
        encrypted = encrypt_data(pickled)
        (session_dir / "session.pickle").write_bytes(encrypted)

        with pytest.raises(SessionExpiredError, match="integrity check"):
            load_session("bad-checksum")


class TestLoadSessionForApi:
    """Tests for load_session_for_api function."""

    def setup_method(self) -> None:
        """Reset storage backend before each test."""
        _reset_session_storage_backend()

    def test_load_session_for_api_not_found(self, tmp_path, monkeypatch):
        """Test that loading non-existent session for API raises error."""
        monkeypatch.setenv("BSC_CONFIG_DIR", str(tmp_path))
        monkeypatch.setenv("BSC_STORAGE_BACKEND", "local")
        _reset_session_storage_backend()

        from bsc.config import reset_settings

        reset_settings()

        with pytest.raises(SessionNotFoundError):
            load_session_for_api("non-existent")


class TestListSessions:
    """Tests for list_sessions functions."""

    def setup_method(self) -> None:
        """Reset storage backend before each test."""
        _reset_session_storage_backend()

    def test_list_sessions_empty(self, tmp_path, monkeypatch):
        """Test listing sessions when none exist."""
        monkeypatch.setenv("BSC_CONFIG_DIR", str(tmp_path))
        monkeypatch.setenv("BSC_STORAGE_BACKEND", "local")
        _reset_session_storage_backend()

        from bsc.config import reset_settings

        reset_settings()

        sessions = list_sessions()
        assert sessions == []


class TestClearSessionCache:
    """Tests for clear_session_cache function."""

    def setup_method(self) -> None:
        """Reset storage backend before each test."""
        _reset_session_storage_backend()

    def test_clear_nonexistent_session(self, tmp_path, monkeypatch):
        """Test clearing a session that doesn't exist."""
        monkeypatch.setenv("BSC_CONFIG_DIR", str(tmp_path))
        monkeypatch.setenv("BSC_STORAGE_BACKEND", "local")
        _reset_session_storage_backend()

        from bsc.config import reset_settings

        reset_settings()

        removed = clear_session_cache("non-existent")
        assert removed == []


class TestUpdateSessionStatus:
    """Tests for update_session_status function."""

    def setup_method(self) -> None:
        """Reset storage backend before each test."""
        _reset_session_storage_backend()

    def test_update_invalid_status_raises_error(self, tmp_path, monkeypatch):
        """Test that invalid status raises ValueError."""
        monkeypatch.setenv("BSC_CONFIG_DIR", str(tmp_path))
        monkeypatch.setenv("BSC_STORAGE_BACKEND", "local")
        _reset_session_storage_backend()

        from bsc.config import reset_settings

        reset_settings()

        with pytest.raises(ValueError, match="Invalid status"):
            update_session_status("any", "invalid-status")

    def test_update_nonexistent_session_raises_error(self, tmp_path, monkeypatch):
        """Test that updating non-existent session raises error."""
        monkeypatch.setenv("BSC_CONFIG_DIR", str(tmp_path))
        monkeypatch.setenv("BSC_STORAGE_BACKEND", "local")
        _reset_session_storage_backend()

        from bsc.config import reset_settings

        reset_settings()

        with pytest.raises(SessionNotFoundError):
            update_session_status("non-existent", "active")


class TestGetSessionMetadata:
    """Tests for get_session_metadata function."""

    def setup_method(self) -> None:
        """Reset storage backend before each test."""
        _reset_session_storage_backend()

    def test_get_nonexistent_metadata_returns_none(self, tmp_path, monkeypatch):
        """Test that getting metadata for non-existent session returns None."""
        monkeypatch.setenv("BSC_CONFIG_DIR", str(tmp_path))
        monkeypatch.setenv("BSC_STORAGE_BACKEND", "local")
        _reset_session_storage_backend()

        from bsc.config import reset_settings

        reset_settings()

        metadata = get_session_metadata("non-existent")
        assert metadata is None
