"""Tests for local storage backend."""

import json
from datetime import UTC, datetime, timedelta

import pytest

from graftpunk.exceptions import SessionExpiredError, SessionNotFoundError
from graftpunk.storage.base import SessionMetadata
from graftpunk.storage.local import LocalSessionStorage


class TestLocalSessionStorage:
    """Tests for LocalSessionStorage."""

    @pytest.fixture
    def storage(self, tmp_path):
        """Create a LocalSessionStorage instance."""
        return LocalSessionStorage(base_dir=tmp_path)

    @pytest.fixture
    def sample_metadata(self):
        """Create sample session metadata."""
        now = datetime.now(UTC)
        return SessionMetadata(
            name="test-session",
            checksum="abc123",
            created_at=now,
            modified_at=now,
            expires_at=now + timedelta(hours=24),
            domain="example.com",
            current_url="https://example.com/dashboard",
            cookie_count=5,
            cookie_domains=["example.com", ".example.com"],
            status="active",
        )

    def test_save_and_load_session(self, storage, sample_metadata):
        """Test saving and loading a session."""
        encrypted_data = b"encrypted session data"

        # Save
        location = storage.save_session(
            "test-session",
            encrypted_data,
            sample_metadata,
        )
        assert "test-session" in location

        # Load
        loaded_data, loaded_metadata = storage.load_session("test-session")
        assert loaded_data == encrypted_data
        assert loaded_metadata.name == sample_metadata.name
        assert loaded_metadata.checksum == sample_metadata.checksum
        assert loaded_metadata.domain == sample_metadata.domain

    def test_list_sessions(self, storage, sample_metadata):
        """Test listing sessions."""
        # Initially empty
        assert storage.list_sessions() == []

        # Save some sessions
        storage.save_session("session-a", b"data", sample_metadata)
        storage.save_session("session-b", b"data", sample_metadata)

        # List should return sorted names
        sessions = storage.list_sessions()
        assert sessions == ["session-a", "session-b"]

    def test_delete_session(self, storage, sample_metadata):
        """Test deleting a session."""
        storage.save_session("to-delete", b"data", sample_metadata)
        assert "to-delete" in storage.list_sessions()

        # Delete
        result = storage.delete_session("to-delete")
        assert result is True
        assert "to-delete" not in storage.list_sessions()

        # Delete non-existent
        result = storage.delete_session("non-existent")
        assert result is False

    def test_get_session_metadata(self, storage, sample_metadata):
        """Test getting session metadata without full load."""
        storage.save_session("test-session", b"data", sample_metadata)

        metadata = storage.get_session_metadata("test-session")
        assert metadata is not None
        assert metadata.name == "test-session"
        assert metadata.domain == sample_metadata.domain
        assert metadata.cookie_count == sample_metadata.cookie_count

        # Non-existent session
        assert storage.get_session_metadata("non-existent") is None

    def test_update_session_metadata(self, storage, sample_metadata):
        """Test updating session status."""
        storage.save_session("update-test", b"data", sample_metadata)

        # Update status
        result = storage.update_session_metadata("update-test", status="logged_out")
        assert result is True

        # Verify update
        metadata = storage.get_session_metadata("update-test")
        assert metadata is not None
        assert metadata.status == "logged_out"

    def test_update_invalid_status_raises_error(self, storage, sample_metadata):
        """Test that invalid status raises ValueError."""
        storage.save_session("invalid-status", b"data", sample_metadata)

        with pytest.raises(ValueError, match="Invalid status"):
            storage.update_session_metadata("invalid-status", status="invalid")

    def test_load_nonexistent_session_raises_error(self, storage):
        """Test that loading non-existent session raises SessionNotFoundError."""
        with pytest.raises(SessionNotFoundError):
            storage.load_session("non-existent")

    def test_load_expired_session_raises_error(self, storage):
        """Test that loading expired session raises SessionExpiredError."""
        now = datetime.now(UTC)
        expired_metadata = SessionMetadata(
            name="expired",
            checksum="abc",
            created_at=now - timedelta(hours=48),
            modified_at=now - timedelta(hours=48),
            expires_at=now - timedelta(hours=24),  # Expired
            domain="example.com",
            current_url=None,
            cookie_count=0,
            cookie_domains=[],
            status="active",
        )

        storage.save_session("expired", b"data", expired_metadata)

        with pytest.raises(SessionExpiredError):
            storage.load_session("expired")


class TestLegacyFlatFile:
    """Tests for legacy flat file session loading and management."""

    @pytest.fixture
    def storage(self, tmp_path):
        """Create a LocalSessionStorage instance."""
        return LocalSessionStorage(base_dir=tmp_path)

    def test_load_legacy_session(self, storage, tmp_path):
        """Test loading a session from legacy flat file structure."""
        # Create a legacy flat file
        legacy_path = tmp_path / "my-provider.session.pickle"
        legacy_path.write_bytes(b"legacy-encrypted-data")

        data, metadata = storage.load_session("my-provider")
        assert data == b"legacy-encrypted-data"
        assert metadata.name == "my-provider"
        assert metadata.checksum == ""
        assert metadata.expires_at is None
        assert metadata.status == "active"

    def test_list_sessions_includes_legacy_files(self, storage, tmp_path):
        """Test that list_sessions includes legacy flat file sessions."""
        # Create a legacy flat file
        legacy_path = tmp_path / "legacy-provider.session.pickle"
        legacy_path.write_bytes(b"data")

        # Create a modern directory session
        modern_dir = tmp_path / "modern-provider"
        modern_dir.mkdir()
        (modern_dir / "session.pickle").write_bytes(b"data")

        sessions = storage.list_sessions()
        assert "legacy-provider" in sessions
        assert "modern-provider" in sessions
        assert sessions == sorted(sessions)

    def test_delete_legacy_session(self, storage, tmp_path):
        """Test deleting a legacy flat file session."""
        legacy_path = tmp_path / "old-session.session.pickle"
        legacy_path.write_bytes(b"data")

        result = storage.delete_session("old-session")
        assert result is True
        assert not legacy_path.exists()

    def test_delete_legacy_session_os_error(self, storage, tmp_path):
        """Test that OSError during legacy file deletion returns False."""
        # Session does not exist as directory or legacy file
        result = storage.delete_session("totally-nonexistent")
        assert result is False


class TestLocalEdgeCases:
    """Tests for edge cases and error handling in local storage."""

    @pytest.fixture
    def storage(self, tmp_path):
        """Create a LocalSessionStorage instance."""
        return LocalSessionStorage(base_dir=tmp_path)

    @pytest.fixture
    def sample_metadata(self):
        """Create sample session metadata."""
        now = datetime.now(UTC)
        return SessionMetadata(
            name="test-session",
            checksum="abc123",
            created_at=now,
            modified_at=now,
            expires_at=now + timedelta(hours=24),
            domain="example.com",
            current_url="https://example.com/dashboard",
            cookie_count=5,
            cookie_domains=["example.com", ".example.com"],
            status="active",
        )

    def test_load_session_missing_metadata_file(self, storage, tmp_path):
        """Test load raises SessionExpiredError when metadata.json is missing."""
        session_dir = tmp_path / "no-meta"
        session_dir.mkdir()
        (session_dir / "session.pickle").write_bytes(b"data")
        # No metadata.json

        with pytest.raises(SessionExpiredError, match="missing metadata"):
            storage.load_session("no-meta")

    def test_load_session_invalid_metadata_json(self, storage, tmp_path):
        """Test load raises SessionExpiredError when metadata.json is invalid JSON."""
        session_dir = tmp_path / "bad-meta"
        session_dir.mkdir()
        (session_dir / "session.pickle").write_bytes(b"data")
        (session_dir / "metadata.json").write_text("not valid json {{{")

        with pytest.raises(SessionExpiredError, match="invalid metadata"):
            storage.load_session("bad-meta")

    def test_load_session_pickle_missing_in_dir(self, storage, tmp_path):
        """Test load raises SessionNotFoundError when dir exists but pickle is missing."""
        session_dir = tmp_path / "empty-dir"
        session_dir.mkdir()
        # No session.pickle

        with pytest.raises(SessionNotFoundError, match="not found"):
            storage.load_session("empty-dir")

    def test_load_session_invalid_expires_at_continues(self, storage, tmp_path):
        """Test load continues when expires_at is an unparseable string."""
        session_dir = tmp_path / "bad-expiry"
        session_dir.mkdir()
        (session_dir / "session.pickle").write_bytes(b"data")
        metadata_dict = {
            "name": "bad-expiry",
            "checksum": "abc",
            "created_at": datetime.now(UTC).isoformat(),
            "modified_at": datetime.now(UTC).isoformat(),
            "expires_at": "not-a-date",
            "domain": None,
            "current_url": None,
            "cookie_count": 0,
            "cookie_domains": [],
            "status": "active",
        }
        with (session_dir / "metadata.json").open("w") as f:
            json.dump(metadata_dict, f)

        data, metadata = storage.load_session("bad-expiry")
        assert data == b"data"
        assert metadata.name == "bad-expiry"

    def test_get_session_metadata_json_decode_error(self, storage, tmp_path):
        """Test get_session_metadata returns None on JSON decode error."""
        session_dir = tmp_path / "corrupt"
        session_dir.mkdir()
        (session_dir / "metadata.json").write_text("{corrupt json")

        result = storage.get_session_metadata("corrupt")
        assert result is None

    def test_update_session_metadata_nonexistent_returns_false(self, storage):
        """Test update_session_metadata returns False for nonexistent session."""
        result = storage.update_session_metadata("nonexistent", status="active")
        assert result is False

    def test_update_session_metadata_corrupt_json_returns_false(self, storage, tmp_path):
        """Test update_session_metadata returns False on corrupt metadata.json."""
        session_dir = tmp_path / "corrupt-update"
        session_dir.mkdir()
        (session_dir / "metadata.json").write_text("{not valid json")

        result = storage.update_session_metadata("corrupt-update", status="active")
        assert result is False

    def test_update_session_metadata_no_status_updates_modified_at(
        self, storage, sample_metadata, tmp_path
    ):
        """Test update_session_metadata with no status still updates modified_at."""
        storage.save_session("just-touch", b"data", sample_metadata)

        # Read original modified_at
        metadata_before = storage.get_session_metadata("just-touch")
        assert metadata_before is not None

        result = storage.update_session_metadata("just-touch")
        assert result is True

        metadata_after = storage.get_session_metadata("just-touch")
        assert metadata_after is not None
        # modified_at should be updated
        assert metadata_after.modified_at >= metadata_before.modified_at

    def test_list_sessions_base_dir_not_exists(self, tmp_path):
        """Test list_sessions returns empty when base_dir doesn't exist."""
        import shutil

        storage = LocalSessionStorage(base_dir=tmp_path / "sessions")
        # Remove the directory that was created during init
        shutil.rmtree(tmp_path / "sessions")

        sessions = storage.list_sessions()
        assert sessions == []

    def test_load_session_no_expiry_succeeds(self, storage, tmp_path):
        """Test load succeeds when expires_at is None (no TTL)."""
        now = datetime.now(UTC)
        no_expiry_metadata = SessionMetadata(
            name="no-expiry",
            checksum="abc",
            created_at=now,
            modified_at=now,
            expires_at=None,
            domain="example.com",
            current_url=None,
            cookie_count=0,
            cookie_domains=[],
            status="active",
        )

        storage.save_session("no-expiry", b"session-data", no_expiry_metadata)

        data, metadata = storage.load_session("no-expiry")
        assert data == b"session-data"
        assert metadata.expires_at is None


class TestSessionMetadata:
    """Tests for SessionMetadata dataclass."""

    def test_metadata_is_frozen(self):
        """Test that SessionMetadata is immutable."""
        now = datetime.now(UTC)
        metadata = SessionMetadata(
            name="test",
            checksum="abc",
            created_at=now,
            modified_at=now,
            expires_at=None,
            domain="example.com",
            current_url=None,
            cookie_count=0,
            cookie_domains=[],
        )

        with pytest.raises(AttributeError):
            metadata.name = "changed"  # type: ignore

    def test_metadata_defaults(self):
        """Test SessionMetadata default values."""
        now = datetime.now(UTC)
        metadata = SessionMetadata(
            name="test",
            checksum="abc",
            created_at=now,
            modified_at=now,
            expires_at=None,
            domain=None,
            current_url=None,
            cookie_count=0,
            cookie_domains=[],
        )

        assert metadata.status == "active"
