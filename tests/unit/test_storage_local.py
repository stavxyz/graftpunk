"""Tests for local storage backend."""

import json
import os
import stat
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
import structlog
from structlog.testing import capture_logs

from graftpunk.exceptions import SessionExpiredError, SessionNotFoundError
from graftpunk.storage.base import SessionMetadata, dict_to_metadata, metadata_to_dict
from graftpunk.storage.local import LocalSessionStorage

# os.replace as imported, for tests that put it back after patching it.
REAL_REPLACE = os.replace


@pytest.fixture
def sample_metadata():
    """Session metadata for a save; the classes that need other values define their own."""
    now = datetime.now(UTC)
    return SessionMetadata(
        name="slot",
        checksum="abc123",
        created_at=now,
        modified_at=now,
        expires_at=now + timedelta(hours=24),
        domain="example.com",
        current_url="https://example.com/dashboard",
        cookie_count=1,
        cookie_domains=["example.com"],
        status="active",
    )


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

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes only")
    def test_load_tightens_a_wide_metadata_mode(self, storage, sample_metadata, tmp_path):
        """A metadata.json left world/group readable is narrowed to 0o600 on load (#178)."""
        storage.save_session("test-session", b"encrypted session data", sample_metadata)
        metadata_path = tmp_path / "test-session" / "metadata.json"
        os.chmod(metadata_path, 0o644)
        assert stat.S_IMODE(metadata_path.stat().st_mode) == 0o644

        storage.load_session("test-session")

        assert stat.S_IMODE(metadata_path.stat().st_mode) == 0o600

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes only")
    def test_load_tightens_a_wide_pickle_mode(self, storage, sample_metadata, tmp_path):
        """session.pickle is tightened on load too, not just metadata.json (#178)."""
        storage.save_session("test-session", b"encrypted session data", sample_metadata)
        pickle_path = tmp_path / "test-session" / "session.pickle"
        os.chmod(pickle_path, 0o644)
        assert stat.S_IMODE(pickle_path.stat().st_mode) == 0o644

        storage.load_session("test-session")

        assert stat.S_IMODE(pickle_path.stat().st_mode) == 0o600

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes only")
    def test_load_does_not_chmod_an_already_tight_file(self, storage, sample_metadata):
        """No gratuitous chmod when a file is already 0o600 (#178, PR #189 review)."""
        storage.save_session("test-session", b"encrypted session data", sample_metadata)

        with patch("os.fchmod") as mock_fchmod:
            storage.load_session("test-session")

        mock_fchmod.assert_not_called()

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes only")
    def test_load_swallows_a_chmod_permission_error(self, storage, sample_metadata, tmp_path):
        """A chmod failure is best-effort: it is logged, never raised (#178, PR #189 review)."""
        storage.save_session("test-session", b"encrypted session data", sample_metadata)
        metadata_path = tmp_path / "test-session" / "metadata.json"
        os.chmod(metadata_path, 0o644)

        # capture_logs() keeps a configured WARNING filter (configure_logging(),
        # as the CLI installs it), which would drop this DEBUG event before it
        # is captured; reset_defaults() lifts any such filter (the autouse
        # _reset_structlog fixture resets again after the test).
        structlog.reset_defaults()
        with (
            patch("os.fchmod", side_effect=PermissionError("no permission")),
            capture_logs() as logs,
        ):
            encrypted_data, metadata = storage.load_session("test-session")

        assert encrypted_data == b"encrypted session data"
        assert metadata.name == sample_metadata.name
        failures = [e for e in logs if e["event"] == "session_file_chmod_failed"]
        assert failures, f"expected session_file_chmod_failed, got: {logs}"

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
        (modern_dir / "metadata.json").write_text("{}")

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


class TestFailedSaves:
    """A local save that fails leaves the slot as it was (#293)."""

    @pytest.fixture
    def storage(self, tmp_path):
        return LocalSessionStorage(base_dir=tmp_path)

    def _two_saves(self, storage, sample_metadata):
        storage.save_session("slot", b"old bytes", sample_metadata)
        return storage.base_dir / "slot"

    @staticmethod
    def _failing_metadata_rename(monkeypatch, *, restore_fails=False):
        real_replace = os.replace

        def replace(src, dst):
            name = Path(dst).name
            if name == "metadata.json":
                raise OSError(28, "No space left on device")
            if restore_fails and name == "session.pickle" and Path(src).suffix == ".bak":
                raise OSError(5, "Input/output error")
            return real_replace(src, dst)

        monkeypatch.setattr("graftpunk.storage.local.os.replace", replace)

    def test_a_failed_restore_keeps_the_backup_and_logs_its_path(
        self, storage, sample_metadata, monkeypatch
    ):
        """The backup is the only copy of the old pickle once the restore fails (#293)."""
        slot = self._two_saves(storage, sample_metadata)
        self._failing_metadata_rename(monkeypatch, restore_fails=True)

        with capture_logs() as logs, pytest.raises(OSError):
            storage.save_session("slot", b"new bytes", sample_metadata)

        backups = list(slot.glob(".session.pickle.*.bak"))
        assert len(backups) == 1
        assert backups[0].read_bytes() == b"old bytes"
        failed = [entry for entry in logs if entry["event"] == "session_restore_failed"]
        assert failed and failed[0]["backup"] == str(backups[0])
        assert failed[0]["log_level"] == "error"

    def test_a_filesystem_without_hard_links_still_restores(
        self, storage, sample_metadata, monkeypatch
    ):
        slot = self._two_saves(storage, sample_metadata)

        def no_links(*_args):
            raise OSError(1, "Operation not permitted")

        monkeypatch.setattr("graftpunk.storage.local.os.link", no_links)
        monkeypatch.setattr("graftpunk.storage.local._backup_copy_logged", False)
        self._failing_metadata_rename(monkeypatch)

        with capture_logs() as logs, pytest.raises(OSError):
            storage.save_session("slot", b"new bytes", sample_metadata)

        assert (slot / "session.pickle").read_bytes() == b"old bytes"
        assert sorted(p.name for p in slot.iterdir()) == ["metadata.json", "session.pickle"]
        assert storage.load_session("slot")[0] == b"old bytes"
        linked = [entry for entry in logs if entry["event"] == "session_backup_link_failed"]
        assert [entry["log_level"] for entry in linked] == ["debug"]

    def test_a_failed_first_save_leaves_another_writers_file(
        self, storage, sample_metadata, monkeypatch
    ):
        """Only what the failed save wrote is removed from a slot it created."""
        real_replace = os.replace
        other = storage.base_dir / "slot" / "other-writer.tmp"

        def replace(src, dst):
            if Path(dst).name == "metadata.json":
                other.write_bytes(b"theirs")
                raise OSError(28, "No space left on device")
            return real_replace(src, dst)

        monkeypatch.setattr("graftpunk.storage.local.os.replace", replace)
        with pytest.raises(OSError):
            storage.save_session("slot", b"new bytes", sample_metadata)

        assert sorted(p.name for p in (storage.base_dir / "slot").iterdir()) == ["other-writer.tmp"]
        assert storage.list_sessions() == []

    def test_an_exception_just_after_the_pickle_rename_still_restores(
        self, storage, sample_metadata, monkeypatch
    ):
        """The flag is set before the rename, so no exception can skip the restore."""
        slot = self._two_saves(storage, sample_metadata)
        real_replace = os.replace

        def replace(src, dst):
            real_replace(src, dst)
            if Path(dst).name == "session.pickle" and Path(src).suffix == ".tmp":
                raise KeyboardInterrupt

        monkeypatch.setattr("graftpunk.storage.local.os.replace", replace)
        with pytest.raises(KeyboardInterrupt):
            storage.save_session("slot", b"new bytes", sample_metadata)
        monkeypatch.setattr("graftpunk.storage.local.os.replace", REAL_REPLACE)

        assert (slot / "session.pickle").read_bytes() == b"old bytes"
        assert sorted(p.name for p in slot.iterdir()) == ["metadata.json", "session.pickle"]

    @staticmethod
    def _no_links(monkeypatch):
        def no_links(*_args):
            raise OSError(1, "Operation not permitted")

        monkeypatch.setattr("graftpunk.storage.local.os.link", no_links)

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes")
    def test_a_copied_backup_is_private_from_creation(self, storage, sample_metadata, monkeypatch):
        slot = self._two_saves(storage, sample_metadata)
        self._no_links(monkeypatch)
        self._failing_metadata_rename(monkeypatch, restore_fails=True)
        old_umask = os.umask(0)
        try:
            with pytest.raises(OSError):
                storage.save_session("slot", b"new bytes", sample_metadata)
        finally:
            os.umask(old_umask)

        (backup,) = slot.glob(".session.pickle.*.bak")
        assert stat.S_IMODE(backup.stat().st_mode) == 0o600
        assert backup.read_bytes() == b"old bytes"

    def test_a_failed_copy_leaves_no_partial_backup(self, storage, sample_metadata, monkeypatch):
        slot = self._two_saves(storage, sample_metadata)
        self._no_links(monkeypatch)

        def broken_copy(*_args, **_kwargs):
            raise OSError(28, "No space left on device")

        monkeypatch.setattr("graftpunk.storage.local.shutil.copyfileobj", broken_copy)
        with capture_logs() as logs:
            storage.save_session("slot", b"new bytes", sample_metadata)

        assert sorted(p.name for p in slot.iterdir()) == ["metadata.json", "session.pickle"]
        assert storage.load_session("slot")[0] == b"new bytes"
        assert "session_backup_failed" in [entry["event"] for entry in logs]

    def test_a_backup_copy_that_fails_leaves_nothing(self, tmp_path, monkeypatch):
        from graftpunk.storage import local

        slot = tmp_path / "slot"
        slot.mkdir()
        target = slot / "session.pickle"
        target.write_bytes(b"old bytes")
        self._no_links(monkeypatch)

        def broken_copy(*_args, **_kwargs):
            raise OSError(28, "No space left on device")

        monkeypatch.setattr("graftpunk.storage.local.shutil.copyfileobj", broken_copy)

        assert local._keep_backup(target) is None
        assert sorted(p.name for p in slot.iterdir()) == ["session.pickle"]

    def test_a_save_through_the_copy_fallback_removes_its_backup(
        self, storage, sample_metadata, monkeypatch
    ):
        slot = self._two_saves(storage, sample_metadata)
        self._no_links(monkeypatch)

        storage.save_session("slot", b"new bytes", sample_metadata)

        assert sorted(p.name for p in slot.iterdir()) == ["metadata.json", "session.pickle"]
        assert storage.load_session("slot")[0] == b"new bytes"

    def test_the_next_save_removes_a_kept_backup(self, storage, sample_metadata, monkeypatch):
        slot = self._two_saves(storage, sample_metadata)
        self._failing_metadata_rename(monkeypatch, restore_fails=True)
        with pytest.raises(OSError):
            storage.save_session("slot", b"new bytes", sample_metadata)
        monkeypatch.setattr("graftpunk.storage.local.os.replace", REAL_REPLACE)
        assert list(slot.glob(".session.pickle.*.bak"))

        storage.save_session("slot", b"newer bytes", sample_metadata)

        assert sorted(p.name for p in slot.iterdir()) == ["metadata.json", "session.pickle"]
        assert storage.load_session("slot")[0] == b"newer bytes"


class TestIncompleteSlots:
    """A slot directory with a pickle and no metadata (#293)."""

    @pytest.fixture
    def storage(self, tmp_path):
        return LocalSessionStorage(base_dir=tmp_path)

    def test_clearing_every_session_removes_a_pickle_only_slot(self, tmp_path, monkeypatch):
        from graftpunk import cache as cache_mod

        monkeypatch.setenv("GRAFTPUNK_CONFIG_DIR", str(tmp_path))
        cache_mod._reset_session_storage_backend()
        try:
            backend = cache_mod._get_session_storage_backend()
            half = backend.base_dir / "half-provider"
            half.mkdir(parents=True)
            (half / "session.pickle").write_bytes(b"data")

            assert cache_mod.list_sessions() == []
            assert cache_mod.list_incomplete_sessions() == ["half-provider"]
            assert cache_mod.clear_session_cache() == ["half-provider"]
            assert not half.exists()
        finally:
            cache_mod._reset_session_storage_backend()

    def test_gp_session_clear_all_removes_a_pickle_only_slot(self, tmp_path, monkeypatch):
        import typer
        from typer.testing import CliRunner

        from graftpunk import cache as cache_mod
        from graftpunk.cli.session_commands import session_app

        monkeypatch.setenv("GRAFTPUNK_CONFIG_DIR", str(tmp_path))
        cache_mod._reset_session_storage_backend()
        try:
            backend = cache_mod._get_session_storage_backend()
            half = backend.base_dir / "half-provider"
            half.mkdir(parents=True)
            (half / "session.pickle").write_bytes(b"data")
            app = typer.Typer()
            app.add_typer(session_app, name="session")

            result = CliRunner().invoke(
                app, ["session", "clear", "--all", "--force"], env={"COLUMNS": "200"}
            )

            assert result.exit_code == 0, result.output
            assert "half-provider" in result.output
            assert not half.exists()
        finally:
            cache_mod._reset_session_storage_backend()

    def test_a_directory_with_only_a_pickle_is_not_listed(self, storage, tmp_path):
        """What a save interrupted before its metadata landed leaves is not a session (#293)."""
        half = tmp_path / "half-provider"
        half.mkdir()
        (half / "session.pickle").write_bytes(b"data")

        assert storage.list_sessions() == []

    def test_gp_session_clear_all_prompt_names_a_pickle_only_slot(self, tmp_path, monkeypatch):
        import typer
        from typer.testing import CliRunner

        from graftpunk import cache as cache_mod
        from graftpunk.cli.session_commands import session_app

        monkeypatch.setenv("GRAFTPUNK_CONFIG_DIR", str(tmp_path))
        cache_mod._reset_session_storage_backend()
        try:
            backend = cache_mod._get_session_storage_backend()
            half = backend.base_dir / "half-provider"
            half.mkdir(parents=True)
            (half / "session.pickle").write_bytes(b"data")
            app = typer.Typer()
            app.add_typer(session_app, name="session")

            result = CliRunner().invoke(
                app, ["session", "clear", "--all"], input="n\n", env={"COLUMNS": "200"}
            )

            assert result.exit_code == 0, result.output
            assert "This will remove 1 session(s):" in result.output
            assert "half-provider (incomplete, no metadata)" in result.output
            assert "Cancelled" in result.output
            assert half.exists()
        finally:
            cache_mod._reset_session_storage_backend()


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

    def test_load_session_missing_pickle_warns(self, storage, tmp_path):
        """A session directory without its pickle is not a plain miss: it warns.

        The directory existing means a bare-name resolution already matched
        it, so a missing pickle signals a partial write or tampering rather
        than "nothing was ever cached here" (#178, PR #189 review).
        """
        session_dir = tmp_path / "empty-dir"
        session_dir.mkdir()
        # No session.pickle

        with capture_logs() as logs, pytest.raises(SessionNotFoundError):
            storage.load_session("empty-dir")

        warnings = [e for e in logs if e.get("log_level") == "warning"]
        assert warnings, f"expected a warning-level event, got: {logs}"
        assert warnings[0]["event"] == "session_pickle_not_found"

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


class TestLocalStorageIdentity:
    """Tests for storage identity properties and metadata stamping."""

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

    def test_storage_backend_is_local(self, storage):
        """Test that storage_backend property returns 'local'."""
        assert storage.storage_backend == "local"

    def test_storage_location_uses_tilde(self):
        """Test that storage_location abbreviates home directory with ~."""
        home_based_path = Path.home() / ".config/graftpunk/sessions"
        storage = LocalSessionStorage(base_dir=home_based_path)
        assert storage.storage_location.startswith("~")

    def test_storage_location_non_home_path(self, tmp_path):
        """Test that storage_location returns full path for non-home paths."""
        storage = LocalSessionStorage(base_dir=tmp_path)
        assert storage.storage_location == str(tmp_path)

    def test_save_stamps_storage_fields_in_metadata(self, storage, sample_metadata, tmp_path):
        """Test that save_session stamps storage_backend and storage_location."""
        storage.save_session("stamped", b"data", sample_metadata)

        metadata_path = tmp_path / "stamped" / "metadata.json"
        with metadata_path.open() as f:
            saved_dict = json.load(f)

        assert saved_dict["storage_backend"] == "local"
        assert "storage_location" in saved_dict
        assert saved_dict["storage_location"] != ""

    def test_shared_serializer_includes_storage_fields(self, storage):
        """Test that metadata_to_dict includes storage_backend and storage_location.

        LocalSessionStorage delegates to the shared base.py serializers (no
        private per-backend duplicates), so this exercises them directly.
        """
        now = datetime.now(UTC)
        location = "~/.config/graftpunk/sessions"
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
            storage_backend="local",
            storage_location=location,
        )
        result = metadata_to_dict(metadata)
        assert result["storage_backend"] == "local"
        assert result["storage_location"] == location

    def test_shared_deserializer_defaults_missing_fields(self, storage):
        """Test that dict_to_metadata defaults missing storage fields to empty string."""
        old_dict = {
            "name": "old-session",
            "checksum": "abc",
            "created_at": datetime.now(UTC).isoformat(),
            "modified_at": datetime.now(UTC).isoformat(),
            "expires_at": None,
            "domain": None,
            "current_url": None,
            "cookie_count": 0,
            "cookie_domains": [],
            "status": "active",
        }
        metadata = dict_to_metadata(old_dict)
        assert metadata.storage_backend == ""
        assert metadata.storage_location == ""
