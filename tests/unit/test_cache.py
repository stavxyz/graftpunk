"""Tests for cache module."""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import dill as pickle
import pytest

from graftpunk.cache import (
    SessionLike,
    _extract_session_metadata,
    _get_session_storage_backend,
    _reset_session_storage_backend,
    cache_session,
    clear_session_cache,
    get_session_metadata,
    list_sessions,
    list_sessions_with_metadata,
    load_session,
    load_session_for_api,
    update_session_cookies,
    update_session_status,
    validate_session_name,
)
from graftpunk.encryption import encrypt_data
from graftpunk.exceptions import EncryptionError, SessionExpiredError, SessionNotFoundError


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
        monkeypatch.setenv("GRAFTPUNK_CONFIG_DIR", str(tmp_path))
        monkeypatch.setenv("GRAFTPUNK_STORAGE_BACKEND", "local")
        monkeypatch.setenv("GRAFTPUNK_SESSION_TTL_HOURS", "24")
        _reset_session_storage_backend()

        from graftpunk.config import reset_settings
        from graftpunk.encryption import reset_encryption_key_cache

        reset_settings()
        reset_encryption_key_cache()

        session = SimpleSession()
        location = cache_session(session, "test-cache")

        assert "test-cache" in location
        assert (tmp_path / "sessions" / "test-cache").exists()

    def test_cache_session_without_name(self, tmp_path, monkeypatch):
        """Test caching a session that has a session_name attribute."""
        monkeypatch.setenv("GRAFTPUNK_CONFIG_DIR", str(tmp_path))
        monkeypatch.setenv("GRAFTPUNK_STORAGE_BACKEND", "local")
        _reset_session_storage_backend()

        from graftpunk.config import reset_settings
        from graftpunk.encryption import reset_encryption_key_cache

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
        monkeypatch.setenv("GRAFTPUNK_CONFIG_DIR", str(tmp_path))
        monkeypatch.setenv("GRAFTPUNK_STORAGE_BACKEND", "local")
        _reset_session_storage_backend()

        from graftpunk.config import reset_settings

        reset_settings()

        with pytest.raises(SessionNotFoundError):
            load_session("non-existent")

    def test_load_session_with_checksum_mismatch(self, tmp_path, monkeypatch):
        """Test that checksum mismatch raises SessionExpiredError."""
        monkeypatch.setenv("GRAFTPUNK_CONFIG_DIR", str(tmp_path))
        monkeypatch.setenv("GRAFTPUNK_STORAGE_BACKEND", "local")
        _reset_session_storage_backend()

        from graftpunk.config import reset_settings
        from graftpunk.encryption import reset_encryption_key_cache

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

        from graftpunk.encryption import encrypt_data

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
        monkeypatch.setenv("GRAFTPUNK_CONFIG_DIR", str(tmp_path))
        monkeypatch.setenv("GRAFTPUNK_STORAGE_BACKEND", "local")
        _reset_session_storage_backend()

        from graftpunk.config import reset_settings

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
        monkeypatch.setenv("GRAFTPUNK_CONFIG_DIR", str(tmp_path))
        monkeypatch.setenv("GRAFTPUNK_STORAGE_BACKEND", "local")
        _reset_session_storage_backend()

        from graftpunk.config import reset_settings

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
        monkeypatch.setenv("GRAFTPUNK_CONFIG_DIR", str(tmp_path))
        monkeypatch.setenv("GRAFTPUNK_STORAGE_BACKEND", "local")
        _reset_session_storage_backend()

        from graftpunk.config import reset_settings

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
        monkeypatch.setenv("GRAFTPUNK_CONFIG_DIR", str(tmp_path))
        monkeypatch.setenv("GRAFTPUNK_STORAGE_BACKEND", "local")
        _reset_session_storage_backend()

        from graftpunk.config import reset_settings

        reset_settings()

        with pytest.raises(ValueError, match="Invalid status"):
            update_session_status("any", "invalid-status")

    def test_update_nonexistent_session_raises_error(self, tmp_path, monkeypatch):
        """Test that updating non-existent session raises error."""
        monkeypatch.setenv("GRAFTPUNK_CONFIG_DIR", str(tmp_path))
        monkeypatch.setenv("GRAFTPUNK_STORAGE_BACKEND", "local")
        _reset_session_storage_backend()

        from graftpunk.config import reset_settings

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
        monkeypatch.setenv("GRAFTPUNK_CONFIG_DIR", str(tmp_path))
        monkeypatch.setenv("GRAFTPUNK_STORAGE_BACKEND", "local")
        _reset_session_storage_backend()

        from graftpunk.config import reset_settings

        reset_settings()

        metadata = get_session_metadata("non-existent")
        assert metadata is None


def _setup_local_env(tmp_path, monkeypatch):
    """Helper to set up local storage environment for tests."""
    monkeypatch.setenv("GRAFTPUNK_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("GRAFTPUNK_STORAGE_BACKEND", "local")
    monkeypatch.setenv("GRAFTPUNK_SESSION_TTL_HOURS", "24")
    _reset_session_storage_backend()

    from graftpunk.config import reset_settings
    from graftpunk.encryption import reset_encryption_key_cache

    reset_settings()
    reset_encryption_key_cache()


def _create_session_on_disk(tmp_path, name, session_obj, *, checksum_override=None):
    """Helper to create a session directory with encrypted data and metadata."""
    session_dir = tmp_path / "sessions" / name
    session_dir.mkdir(parents=True, exist_ok=True)

    pickled = pickle.dumps(session_obj)
    if checksum_override is not None:
        checksum = checksum_override
    else:
        checksum = hashlib.sha256(pickled).hexdigest()
    encrypted = encrypt_data(pickled)
    (session_dir / "session.pickle").write_bytes(encrypted)

    now = datetime.now(UTC)
    metadata = {
        "name": name,
        "checksum": checksum,
        "created_at": now.isoformat(),
        "modified_at": now.isoformat(),
        "expires_at": (now + timedelta(hours=24)).isoformat(),
        "domain": "example.com",
        "current_url": "https://example.com",
        "cookie_count": 0,
        "cookie_domains": [],
        "status": "active",
    }
    (session_dir / "metadata.json").write_text(json.dumps(metadata))
    return session_dir


class TestGetSessionStorageBackendSupabase:
    """Tests for _get_session_storage_backend supabase path."""

    def setup_method(self) -> None:
        _reset_session_storage_backend()

    def teardown_method(self) -> None:
        _reset_session_storage_backend()

    def test_supabase_backend_returned(self, monkeypatch):
        """Test that supabase backend is instantiated when configured."""
        mock_settings = MagicMock()
        mock_settings.storage_backend = "supabase"
        mock_settings.get_storage_config.return_value = {
            "url": "https://test.supabase.co",
            "service_key": "test-key",
            "bucket_name": "test-bucket",
        }

        mock_supabase_cls = MagicMock()
        mock_supabase_instance = MagicMock()
        mock_supabase_cls.return_value = mock_supabase_instance

        with (
            patch("graftpunk.cache.get_settings", return_value=mock_settings),
            patch.dict(
                "sys.modules",
                {"graftpunk.storage.supabase": MagicMock(SupabaseSessionStorage=mock_supabase_cls)},
            ),
        ):
            backend = _get_session_storage_backend()

        mock_supabase_cls.assert_called_once_with(
            url="https://test.supabase.co",
            service_key="test-key",
            bucket_name="test-bucket",
        )
        assert backend is mock_supabase_instance

    def test_cached_backend_returned_on_second_call(self, tmp_path, monkeypatch):
        """Test that the cached backend is returned without re-creating."""
        _setup_local_env(tmp_path, monkeypatch)
        backend1 = _get_session_storage_backend()
        backend2 = _get_session_storage_backend()
        assert backend1 is backend2


class TestCacheSessionErrors:
    """Tests for cache_session error handling paths."""

    def setup_method(self) -> None:
        _reset_session_storage_backend()

    def test_pickle_error_during_serialization(self, tmp_path, monkeypatch):
        """Test that PickleError during pickle.dumps is propagated."""
        _setup_local_env(tmp_path, monkeypatch)

        session = SimpleSession()
        with (
            patch("graftpunk.cache.pickle.dumps", side_effect=pickle.PickleError("cannot pickle")),
            pytest.raises(pickle.PickleError, match="cannot pickle"),
        ):
            cache_session(session, "pickle-fail")

    def test_encryption_error_during_cache(self, tmp_path, monkeypatch):
        """Test that EncryptionError during encrypt_data is wrapped in the except clause."""
        _setup_local_env(tmp_path, monkeypatch)

        session = SimpleSession()
        # RuntimeError IS caught by the except clause
        with (
            patch("graftpunk.cache.encrypt_data", side_effect=RuntimeError("encrypt fail")),
            pytest.raises(RuntimeError, match="encrypt fail"),
        ):
            cache_session(session, "encrypt-fail")


class TestLoadSessionDecryptionError:
    """Tests for load_session EncryptionError path."""

    def setup_method(self) -> None:
        _reset_session_storage_backend()

    def test_encryption_error_raises_session_expired(self, tmp_path, monkeypatch):
        """Test that EncryptionError during decryption raises SessionExpiredError."""
        _setup_local_env(tmp_path, monkeypatch)

        # Create a session directory with valid structure but data that will
        # fail decryption when we mock decrypt_data
        session_dir = tmp_path / "sessions" / "decrypt-fail"
        session_dir.mkdir(parents=True)

        session = SimpleSession()
        pickled = pickle.dumps(session)
        encrypted = encrypt_data(pickled)
        (session_dir / "session.pickle").write_bytes(encrypted)

        now = datetime.now(UTC)
        metadata = {
            "name": "decrypt-fail",
            "checksum": hashlib.sha256(pickled).hexdigest(),
            "created_at": now.isoformat(),
            "modified_at": now.isoformat(),
            "expires_at": (now + timedelta(hours=24)).isoformat(),
            "domain": None,
            "current_url": None,
            "cookie_count": 0,
            "cookie_domains": [],
            "status": "active",
        }
        (session_dir / "metadata.json").write_text(json.dumps(metadata))

        with (
            patch("graftpunk.cache.decrypt_data", side_effect=EncryptionError("bad key")),
            pytest.raises(SessionExpiredError, match="cannot be decrypted"),
        ):
            load_session("decrypt-fail")


class TestLoadSessionChecksumPaths:
    """Tests for load_session checksum verification paths."""

    def setup_method(self) -> None:
        _reset_session_storage_backend()

    def test_legacy_session_without_checksum_loads_with_warning(self, tmp_path, monkeypatch):
        """Test that legacy sessions with empty checksum load successfully."""
        _setup_local_env(tmp_path, monkeypatch)

        # Create session with empty checksum (legacy)
        _create_session_on_disk(tmp_path, "legacy-session", SimpleSession(), checksum_override="")

        session = load_session("legacy-session")
        assert hasattr(session, "cookies")
        assert hasattr(session, "headers")


class TestLoadSessionUnpicklingError:
    """Tests for load_session UnpicklingError path."""

    def setup_method(self) -> None:
        _reset_session_storage_backend()

    def test_unpickling_error_raises_session_expired(self, tmp_path, monkeypatch):
        """Test that UnpicklingError during deserialization raises SessionExpiredError."""
        _setup_local_env(tmp_path, monkeypatch)

        # Create a valid session on disk first
        _create_session_on_disk(tmp_path, "unpickle-fail", SimpleSession())

        with (
            patch("graftpunk.cache.pickle.loads", side_effect=pickle.UnpicklingError("bad")),
            pytest.raises(SessionExpiredError, match="Failed to load session"),
        ):
            load_session("unpickle-fail")

    def test_runtime_error_during_unpickle_raises_session_expired(self, tmp_path, monkeypatch):
        """Test that RuntimeError during deserialization raises SessionExpiredError."""
        _setup_local_env(tmp_path, monkeypatch)

        _create_session_on_disk(tmp_path, "runtime-fail", SimpleSession())

        with (
            patch("graftpunk.cache.pickle.loads", side_effect=RuntimeError("unexpected")),
            pytest.raises(SessionExpiredError, match="Failed to load session"),
        ):
            load_session("runtime-fail")


class TestLoadSessionForApiSuccess:
    """Tests for load_session_for_api successful path."""

    def setup_method(self) -> None:
        _reset_session_storage_backend()

    def test_load_session_for_api_returns_requests_session(self, tmp_path, monkeypatch):
        """Test that load_session_for_api returns a requests.Session with cookies and headers."""
        import requests

        _setup_local_env(tmp_path, monkeypatch)

        # Create a valid session on disk
        session = SimpleSession()
        session.headers = {"Authorization": "Bearer token123"}
        _create_session_on_disk(tmp_path, "api-session", session)

        api_session = load_session_for_api("api-session")

        assert isinstance(api_session, requests.Session)
        assert api_session.headers.get("Authorization") == "Bearer token123"

    def test_load_session_for_api_copies_cookies(self, tmp_path, monkeypatch):
        """Test that cookies are copied from the cached session."""
        import requests

        _setup_local_env(tmp_path, monkeypatch)

        # Create a session with cookies in a jar
        session = SimpleSession()
        jar = requests.cookies.RequestsCookieJar()
        jar.set("session_id", "abc123", domain="example.com")
        session.cookies = jar
        _create_session_on_disk(tmp_path, "cookie-session", session)

        api_session = load_session_for_api("cookie-session")

        assert isinstance(api_session, requests.Session)
        assert api_session.cookies.get("session_id") == "abc123"


class TestLoadSessionForApiGraftpunkSession:
    """Tests for load_session_for_api returning GraftpunkSession."""

    def test_load_session_for_api_returns_graftpunk_session(self, monkeypatch):
        """load_session_for_api should return GraftpunkSession when profiles exist."""
        import requests

        from graftpunk.graftpunk_session import GraftpunkSession

        mock_session = MagicMock()
        mock_session.cookies = requests.cookies.RequestsCookieJar()
        mock_session.headers = {"User-Agent": "test"}
        mock_session._gp_header_profiles = {"navigation": {"User-Agent": "Browser"}}
        monkeypatch.setattr("graftpunk.cache.load_session", lambda name: mock_session)

        api_session = load_session_for_api("test")
        assert isinstance(api_session, GraftpunkSession)
        assert api_session._gp_header_profiles == {"navigation": {"User-Agent": "Browser"}}

    def test_load_session_for_api_no_profiles_returns_graftpunk_session(self, monkeypatch):
        """load_session_for_api returns GraftpunkSession even without profiles."""
        import requests

        from graftpunk.graftpunk_session import GraftpunkSession

        mock_session = MagicMock()
        mock_session.cookies = requests.cookies.RequestsCookieJar()
        mock_session.headers = {"User-Agent": "test"}
        # Simulate old session without _gp_header_profiles attribute
        del mock_session._gp_header_profiles
        monkeypatch.setattr("graftpunk.cache.load_session", lambda name: mock_session)

        api_session = load_session_for_api("test")
        assert isinstance(api_session, GraftpunkSession)
        assert api_session._gp_header_profiles == {}

    def test_load_session_for_api_copies_token_cache(self, monkeypatch):
        """Token cache is transferred from browser session to API session."""
        import requests

        from graftpunk.tokens import _CACHE_ATTR, CachedToken

        mock_session = MagicMock()
        mock_session.cookies = requests.cookies.RequestsCookieJar()
        mock_session.headers = {"User-Agent": "test"}
        mock_session._gp_header_profiles = {}

        token_cache = {
            "X-CSRF": CachedToken(name="X-CSRF", value="tok123", extracted_at=1000, ttl=300)
        }
        setattr(mock_session, _CACHE_ATTR, token_cache)
        monkeypatch.setattr("graftpunk.cache.load_session", lambda name: mock_session)

        api_session = load_session_for_api("cached-session")
        assert hasattr(api_session, _CACHE_ATTR)
        assert getattr(api_session, _CACHE_ATTR) == token_cache

    def test_load_session_for_api_browser_identity_not_clobbered(self, monkeypatch):
        """Browser identity headers from profiles must survive browser_session.headers copy.

        Regression test for #52: load_session_for_api copied browser_session.headers
        (which contains python-requests defaults) on top of the Chrome UA that
        _apply_browser_identity() set during GraftpunkSession init.
        """
        import requests

        mock_session = MagicMock()
        mock_session.cookies = requests.cookies.RequestsCookieJar()
        # Simulate a real pickled BrowserSession: its headers dict contains
        # the requests library default User-Agent.
        mock_session.headers = requests.utils.default_headers()
        mock_session._gp_header_profiles = {
            "navigation": {
                "User-Agent": "Mozilla/5.0 Chrome/144.0.0.0",
                "Accept": "text/html",
                "sec-ch-ua": '"Chromium";v="144"',
            }
        }
        del mock_session._gp_cached_tokens
        monkeypatch.setattr("graftpunk.cache.load_session", lambda name: mock_session)

        api_session = load_session_for_api("test")

        # The Chrome UA from profiles must win, not python-requests default
        assert api_session.headers["User-Agent"] == "Mozilla/5.0 Chrome/144.0.0.0"
        assert api_session.headers["sec-ch-ua"] == '"Chromium";v="144"'

    def test_load_session_for_api_custom_browser_session_headers_preserved(self, monkeypatch):
        """Non-default headers from browser_session.headers should still be copied."""
        import requests

        mock_session = MagicMock()
        mock_session.cookies = requests.cookies.RequestsCookieJar()
        mock_session.headers = {
            **requests.utils.default_headers(),
            "X-Custom-Header": "custom-value",
            "User-Agent": "CustomBot/1.0",  # explicitly set, not requests default
        }
        mock_session._gp_header_profiles = {}
        del mock_session._gp_cached_tokens
        monkeypatch.setattr("graftpunk.cache.load_session", lambda name: mock_session)

        api_session = load_session_for_api("test")

        assert api_session.headers["X-Custom-Header"] == "custom-value"
        # Non-default UA should be copied through
        assert api_session.headers["User-Agent"] == "CustomBot/1.0"

    def test_load_session_for_api_no_token_cache(self, monkeypatch):
        """API session works fine when browser session has no token cache."""
        import requests

        mock_session = MagicMock()
        mock_session.cookies = requests.cookies.RequestsCookieJar()
        mock_session.headers = {"User-Agent": "test"}
        mock_session._gp_header_profiles = {}
        # No _gp_cached_tokens attribute
        del mock_session._gp_cached_tokens
        monkeypatch.setattr("graftpunk.cache.load_session", lambda name: mock_session)

        api_session = load_session_for_api("no-cache-session")
        from graftpunk.tokens import _CACHE_ATTR

        assert not hasattr(api_session, _CACHE_ATTR)


class TestListSessionsWithMetadata:
    """Tests for list_sessions_with_metadata function."""

    def setup_method(self) -> None:
        _reset_session_storage_backend()

    def test_list_sessions_with_metadata_empty(self, tmp_path, monkeypatch):
        """Test listing sessions when none exist."""
        _setup_local_env(tmp_path, monkeypatch)

        results = list_sessions_with_metadata()
        assert results == []

    def test_list_sessions_with_metadata_multiple(self, tmp_path, monkeypatch):
        """Test listing multiple sessions returns metadata sorted by modified_at."""
        _setup_local_env(tmp_path, monkeypatch)

        # Create two sessions
        _create_session_on_disk(tmp_path, "session-a", SimpleSession())
        _create_session_on_disk(tmp_path, "session-b", SimpleSession())

        results = list_sessions_with_metadata()

        assert len(results) == 2
        names = {r["name"] for r in results}
        assert names == {"session-a", "session-b"}

        # Verify metadata fields are present
        for result in results:
            assert "checksum" in result
            assert "created_at" in result
            assert "modified_at" in result
            assert "expires_at" in result
            assert "domain" in result
            assert "cookie_count" in result
            assert "cookie_domains" in result
            assert "status" in result
            assert "path" in result

    def test_list_sessions_with_metadata_skips_missing_metadata(self, tmp_path, monkeypatch):
        """Test that sessions without metadata are skipped."""
        _setup_local_env(tmp_path, monkeypatch)

        # Create one valid session
        _create_session_on_disk(tmp_path, "valid-session", SimpleSession())

        # Create a session dir without metadata file
        bad_dir = tmp_path / "sessions" / "no-metadata"
        bad_dir.mkdir(parents=True)
        (bad_dir / "session.pickle").write_bytes(b"dummy")

        results = list_sessions_with_metadata()

        # Only the valid session should appear
        assert len(results) == 1
        assert results[0]["name"] == "valid-session"


class TestClearSessionCacheAll:
    """Tests for clear_session_cache clearing all sessions."""

    def setup_method(self) -> None:
        _reset_session_storage_backend()

    def test_clear_all_sessions(self, tmp_path, monkeypatch):
        """Test clearing all sessions when multiple exist."""
        _setup_local_env(tmp_path, monkeypatch)

        # Create sessions
        _create_session_on_disk(tmp_path, "sess-1", SimpleSession())
        _create_session_on_disk(tmp_path, "sess-2", SimpleSession())

        removed = clear_session_cache()
        assert len(removed) == 2
        assert set(removed) == {"sess-1", "sess-2"}

        # Verify they're gone
        remaining = list_sessions()
        assert remaining == []


class TestGetSessionMetadataSuccess:
    """Tests for get_session_metadata with existing session."""

    def setup_method(self) -> None:
        _reset_session_storage_backend()

    def test_get_existing_metadata(self, tmp_path, monkeypatch):
        """Test getting metadata for an existing session."""
        _setup_local_env(tmp_path, monkeypatch)

        _create_session_on_disk(tmp_path, "meta-session", SimpleSession())

        metadata = get_session_metadata("meta-session")

        assert metadata is not None
        assert metadata["name"] == "meta-session"
        assert metadata["status"] == "active"
        assert metadata["domain"] == "example.com"
        assert "checksum" in metadata
        assert "created_at" in metadata
        assert "modified_at" in metadata


class TestValidateSessionName:
    """Tests for session name validation."""

    def test_valid_names(self):
        """Valid session names don't raise."""
        for name in ["hackernews", "my-site", "my_site", "site123", "a", "a1b2"]:
            validate_session_name(name)  # Should not raise

    def test_rejects_dots(self):
        """Session names with dots are rejected (dots indicate domains)."""
        with pytest.raises(ValueError, match="cannot contain dots"):
            validate_session_name("example.com")

    def test_rejects_empty(self):
        """Empty session names are rejected."""
        with pytest.raises(ValueError, match="must be non-empty"):
            validate_session_name("")

    def test_rejects_invalid_characters(self):
        """Session names with invalid characters are rejected."""
        for name in ["my site", "my/site", "MY_SITE", "Site", "@site"]:
            with pytest.raises(ValueError, match="must match"):
                validate_session_name(name)

    def test_rejects_leading_hyphen(self):
        """Session names starting with hyphen are rejected."""
        with pytest.raises(ValueError, match="must match"):
            validate_session_name("-my-site")

    def test_rejects_leading_underscore(self):
        """Session names starting with underscore are rejected."""
        with pytest.raises(ValueError, match="must match"):
            validate_session_name("_my-site")


class TestUpdateSessionCookies:
    """Tests for update_session_cookies() — persisting API session changes."""

    def test_updates_cookies_on_cached_session(self) -> None:
        """Cookies from the API session are merged into the cached session."""
        import requests

        cached_session = MagicMock()
        cached_session.cookies = requests.cookies.RequestsCookieJar()
        cached_session.cookies.set("original", "value")

        api_session = requests.Session()
        api_session.cookies.set("original", "updated")
        api_session.cookies.set("new_cookie", "new_value")

        with (
            patch("graftpunk.cache.load_session") as mock_load,
            patch("graftpunk.cache.cache_session") as mock_cache,
        ):
            mock_load.return_value = cached_session
            update_session_cookies(api_session, "testsession")
            mock_load.assert_called_once_with("testsession")
            mock_cache.assert_called_once_with(cached_session, "testsession")

    def test_load_failure_logs_warning_and_returns(self) -> None:
        """If loading the cached session fails, log a warning and return."""
        import requests

        api_session = requests.Session()
        with patch("graftpunk.cache.load_session", side_effect=Exception("corrupt")):
            update_session_cookies(api_session, "badsession")  # Should not raise

    def test_cache_failure_logs_warning_and_returns(self) -> None:
        """If re-caching fails, log a warning and return."""
        import requests

        cached_session = MagicMock()
        cached_session.cookies = requests.cookies.RequestsCookieJar()
        api_session = requests.Session()

        with (
            patch("graftpunk.cache.load_session", return_value=cached_session),
            patch("graftpunk.cache.cache_session", side_effect=OSError("disk full")),
        ):
            update_session_cookies(api_session, "testsession")  # Should not raise

    def test_update_session_cookies_persists_token_cache(self) -> None:
        """Token cache survives update_session_cookies round-trip."""
        import requests

        from graftpunk.tokens import _CACHE_ATTR, CachedToken

        cached_session = MagicMock()
        cached_session.cookies = requests.cookies.RequestsCookieJar()

        api_session = requests.Session()
        token_cache = {
            "X-CSRF": CachedToken(name="X-CSRF", value="tok123", extracted_at=1000, ttl=300)
        }
        setattr(api_session, _CACHE_ATTR, token_cache)

        with (
            patch("graftpunk.cache.load_session", return_value=cached_session) as mock_load,
            patch("graftpunk.cache.cache_session") as mock_cache,
        ):
            update_session_cookies(api_session, "testsession")

        mock_load.assert_called_once_with("testsession")
        mock_cache.assert_called_once_with(cached_session, "testsession")
        # Verify token cache was transferred to the original session
        assert getattr(cached_session, _CACHE_ATTR) == token_cache
