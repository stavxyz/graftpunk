"""Local filesystem session storage backend."""

import contextlib
import json
import os
import shutil
import stat
import sys
import tempfile
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any

from graftpunk.exceptions import SessionExpiredError, SessionNotFoundError
from graftpunk.logging import get_logger
from graftpunk.storage.base import (
    UNCHANGED,
    SessionMetadata,
    dict_to_metadata,
    metadata_to_dict,
)

LOG = get_logger(__name__)


def _write_private_temp(target: Path, data: bytes) -> Path:
    """Write *data* to a new 0o600 temporary file beside *target*; return its path.

    The caller renames it over *target* with :func:`os.replace`, which is
    atomic on one filesystem, so a reader sees the old file or the new one.
    """
    fd, raw = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
    path = Path(raw)
    try:
        if sys.platform != "win32":
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return path


_backup_copy_logged = False


def _keep_backup(path: Path) -> Path | None:
    """Keep the file at *path* under a new name beside it; None when there is none to keep.

    A hard link costs no copy and keeps the old file's bytes reachable after
    :func:`os.replace` puts a new file at *path*. A filesystem that refuses
    hard links gets a copy instead (logged once per process at debug). Only
    when the copy fails too is there no backup, and a failed save then cannot
    restore the old file.
    """
    global _backup_copy_logged
    if not path.exists():
        return None
    # A name no other backup can have, so this never takes or deletes one an
    # earlier save kept.
    backup = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.bak")
    try:
        os.link(path, backup)
        return backup
    except OSError as exc:
        if not _backup_copy_logged:
            _backup_copy_logged = True
            LOG.debug("session_backup_link_failed", error_type=type(exc).__name__)
    created = False
    try:
        # 0o600 from creation, like the pickle: copy2 would create it with
        # the umask's mode and narrow it only afterwards.
        fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        created = True
        with os.fdopen(fd, "wb") as dst, path.open("rb") as src:
            shutil.copyfileobj(src, dst)
    except OSError as exc:
        if created:
            backup.unlink(missing_ok=True)
        LOG.warning("session_backup_failed", error_type=type(exc).__name__)
        return None
    return backup


def _kept_name(backup: Path) -> Path:
    """The name a backup takes once a failed restore leaves it the only old copy."""
    return backup.with_name(backup.name.removesuffix(".bak") + ".kept")


class LocalSessionStorage:
    """Local filesystem session storage (backward compatible).

    Storage structure: {base_dir}/{name}/session.pickle + metadata.json

    Features:
    - Atomic writes with secure permissions (0o600)
    - TTL enforcement via metadata.json
    - Backward compatible with existing sessions
    """

    def __init__(self, base_dir: Path) -> None:
        """Initialize local session storage.

        Args:
            base_dir: Base directory for session storage
        """
        self.base_dir = base_dir
        self.base_dir.mkdir(parents=True, exist_ok=True)
        LOG.info("local_session_storage_initialized", base_dir=str(base_dir))

    @property
    def storage_backend(self) -> str:
        """Backend type identifier.

        Returns:
            Always "local" for local filesystem storage.
        """
        return "local"

    @property
    def storage_location(self) -> str:
        """Display-friendly storage location using ~ for home directory paths.

        Returns:
            Path string with ~ abbreviation for home directory, or full path.
        """
        try:
            return str("~" / self.base_dir.relative_to(Path.home()))
        except ValueError:
            return str(self.base_dir)

    def save_session(
        self,
        name: str,
        encrypted_data: bytes,
        metadata: SessionMetadata,
    ) -> str:
        """Save encrypted session to local filesystem.

        Args:
            name: Session identifier
            encrypted_data: Already-encrypted session bytes
            metadata: Session metadata

        Returns:
            Path to session directory

        Raises:
            OSError: If save fails
        """
        session_dir = self.base_dir / name
        created_dir = not session_dir.exists()
        session_dir.mkdir(parents=True, exist_ok=True)

        pickle_path = session_dir / "session.pickle"
        metadata_path = session_dir / "metadata.json"
        stale_backups = list(session_dir.glob(f".{pickle_path.name}.*.kept"))

        LOG.info("session_save_started", name=name, path=str(pickle_path))

        # Stamp storage identity fields before serialization
        metadata = replace(
            metadata,
            storage_backend=self.storage_backend,
            storage_location=self.storage_location,
        )
        metadata_bytes = json.dumps(metadata_to_dict(metadata), indent=2, default=str).encode()

        # Both files are written in full to temporary files beside them and
        # only then renamed into place, so a write that fails (a full disk)
        # leaves the slot as it was, or no slot at all. Before the pickle is
        # replaced, the old one is hard-linked (or copied) to a backup; if anything is
        # raised between the two renames, the old pickle is put back (or a
        # slot this save created is removed), so the pickle and the metadata
        # never disagree after a failed save. What remains is a crash, or a
        # reader, between the two renames: such a slot fails its integrity
        # check until it is saved again, as the next login does. The
        # temporary files are 0o600 from creation, like the files they
        # replace: the metadata carries the account identifier, the domain
        # and the URL last visited, not secrets, but not other users'
        # business either (#178, #293).
        written: list[Path] = []
        backup: Path | None = None
        keep_backup = False
        pickle_replaced = False
        try:
            pickle_tmp = _write_private_temp(pickle_path, encrypted_data)
            written.append(pickle_tmp)
            metadata_tmp = _write_private_temp(metadata_path, metadata_bytes)
            written.append(metadata_tmp)
            backup = _keep_backup(pickle_path)
            # Set before the rename: an exception landing just after it
            # returns must still restore. Restoring when the rename never
            # happened puts the same old bytes back (for a hard link, the
            # rename does nothing), and the finally removes the backup.
            pickle_replaced = True
            os.replace(pickle_tmp, pickle_path)
            os.replace(metadata_tmp, metadata_path)
        except BaseException:
            for tmp in written:
                tmp.unlink(missing_ok=True)
            if created_dir:
                # Only what this save wrote, and the directory only when that
                # leaves it empty: another writer may have joined it meanwhile.
                if pickle_replaced:
                    pickle_path.unlink(missing_ok=True)
                with contextlib.suppress(OSError):
                    session_dir.rmdir()
            elif pickle_replaced and backup is not None:
                try:
                    os.replace(backup, pickle_path)
                except OSError as exc:
                    # The backup is now the only copy of the old pickle: keep
                    # it, under a name the next successful save will clear.
                    keep_backup = True
                    kept = _kept_name(backup)
                    with contextlib.suppress(OSError):
                        os.replace(backup, kept)
                        backup = kept
                    LOG.error(
                        "session_restore_failed",
                        name=name,
                        backup=str(backup),
                        error=str(exc),
                    )
            raise
        finally:
            if backup is not None and not keep_backup:
                backup.unlink(missing_ok=True)

        # A backup an earlier save kept after its restore failed is stale once
        # this save has landed both files. Only kept ones, found before this
        # save began: a save still in flight has its backup under the plain
        # name. The save is complete, so a failure here only leaves the file.
        for stale in stale_backups:
            with contextlib.suppress(OSError):
                stale.unlink(missing_ok=True)

        LOG.info("session_save_completed", name=name, path=str(session_dir))
        return str(session_dir)

    def load_session(
        self,
        name: str,
    ) -> tuple[bytes, SessionMetadata]:
        """Load encrypted session from local filesystem.

        Args:
            name: Session identifier

        Returns:
            Tuple of (encrypted_data, metadata)

        Raises:
            SessionNotFoundError: If session doesn't exist
            SessionExpiredError: If session TTL exceeded or metadata invalid
        """
        session_dir = self.base_dir / name

        if not session_dir.is_dir():
            # Check for old flat file structure for backward compatibility
            old_path = self.base_dir / f"{name}.session.pickle"
            if old_path.exists():
                return self._load_legacy_session(name, old_path)

            # A miss is a normal query result, not a problem: the raised
            # SessionNotFoundError is the signal callers act on, and a bare
            # base name resolving to an account is expected to miss here on
            # the way to a hit (#178).
            LOG.debug("session_not_found", name=name)
            raise SessionNotFoundError(f"Session '{name}' not found")

        pickle_path = session_dir / "session.pickle"
        metadata_path = session_dir / "metadata.json"

        if not pickle_path.exists():
            # NOT a plain miss: the session directory exists (so a bare-name
            # resolution already matched it) but its pickle is gone, which
            # means a partial write or tampering rather than "nothing was
            # ever cached here". That is worth a WARNING (#178, PR #189
            # review).
            LOG.warning("session_pickle_not_found", name=name)
            raise SessionNotFoundError(f"Session '{name}' not found")

        LOG.info("session_load_started", name=name, structure="directory")

        # Load and validate metadata
        if not metadata_path.exists():
            LOG.error("session_metadata_missing", name=name)
            raise SessionExpiredError(
                f"Session '{name}' is missing metadata. Please run 'graftpunk clear' and re-login."
            )

        try:
            with metadata_path.open() as f:
                self._tighten_mode(f, name)
                metadata_dict = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            LOG.error("session_metadata_invalid", name=name, error=str(exc))
            raise SessionExpiredError(
                f"Session '{name}' has invalid metadata. Please run 'graftpunk clear' and re-login."
            ) from exc

        # Check TTL
        expires_at_str = metadata_dict.get("expires_at")
        if expires_at_str:
            try:
                expires_at = datetime.fromisoformat(expires_at_str)
                if expires_at.tzinfo is None:
                    expires_at = expires_at.replace(tzinfo=UTC)
                if datetime.now(UTC) > expires_at:
                    LOG.warning("session_expired_ttl", name=name, expires_at=expires_at_str)
                    raise SessionExpiredError(
                        f"Session '{name}' has expired (TTL). Run 'gp clear' and re-login."
                    )
            except (ValueError, TypeError) as exc:
                LOG.warning(
                    "invalid_expires_at",
                    name=name,
                    expires_at=expires_at_str,
                    error=str(exc),
                )

        # Load encrypted data
        with pickle_path.open("rb") as f:
            self._tighten_mode(f, name)
            encrypted_data = f.read()

        metadata = dict_to_metadata(metadata_dict)
        LOG.info("session_load_completed", name=name)
        return encrypted_data, metadata

    def _tighten_mode(self, f: IO[Any], name: str) -> None:
        """Best-effort: narrow an already-open file's mode to 0o600.

        New files are written with 0o600 from creation, and a re-save
        fchmods to it too (see ``save_session``), but a file saved before
        either of those, or copied in by some other means, may carry a
        wider mode. Neither the pickle nor the metadata is itself a
        secret, but they are not other users' business either. This works
        off the descriptor *f* already has open for the read that follows,
        so it costs no extra path lookup and has no window between
        checking the mode and a different file landing at that path.
        Skipped on Windows, where POSIX modes do not apply; a failure to
        chmod is logged at debug and never blocks the load.
        """
        if sys.platform == "win32":
            return
        try:
            fd = f.fileno()
            current_mode = stat.S_IMODE(os.fstat(fd).st_mode)
            if current_mode & ~0o600:
                os.fchmod(fd, 0o600)
        except OSError as exc:
            LOG.debug("session_file_chmod_failed", name=name, error=str(exc))

    def _load_legacy_session(
        self,
        name: str,
        path: Path,
    ) -> tuple[bytes, SessionMetadata]:
        """Load session from legacy flat file structure.

        Args:
            name: Session identifier
            path: Path to legacy pickle file

        Returns:
            Tuple of (encrypted_data, metadata)
        """
        LOG.info("session_load_started", name=name, structure="flat_file")

        with path.open("rb") as f:
            encrypted_data = f.read()

        # Create minimal metadata from file stats
        file_stat = path.stat()
        metadata = SessionMetadata(
            name=name,
            checksum="",  # Unknown for legacy sessions
            created_at=datetime.fromtimestamp(file_stat.st_ctime, tz=UTC),
            modified_at=datetime.fromtimestamp(file_stat.st_mtime, tz=UTC),
            expires_at=None,  # No TTL for legacy sessions
            domain=None,
            current_url=None,
            cookie_count=0,
            cookie_domains=[],
            status="active",
        )

        LOG.info("session_load_completed", name=name, structure="flat_file")
        return encrypted_data, metadata

    def list_sessions(self) -> list[str]:
        """List all session names.

        Returns:
            Sorted list of session names
        """
        if not self.base_dir.exists():
            return []

        names = []
        try:
            for item in self.base_dir.iterdir():
                # A slot is listed only with both files: a directory a crashed
                # save left with a pickle and no metadata is not a session.
                if (
                    item.is_dir()
                    and (item / "session.pickle").exists()
                    and (item / "metadata.json").exists()
                ):
                    names.append(item.name)
                elif item.is_file() and item.suffix == ".pickle":
                    # Legacy flat file structure
                    names.append(item.stem.replace(".session", ""))

            LOG.debug("session_list_completed", count=len(names))
            return sorted(names)
        except OSError as exc:
            LOG.error("failed_to_list_sessions", error=str(exc))
            return []

    def incomplete_sessions(self) -> list[str]:
        """Slot directories holding a pickle but no metadata.

        A crash during a save, or an older release, can leave one. They are
        not sessions (:meth:`list_sessions` leaves them out, and loading one
        fails), but clearing every session removes them too.
        """
        if not self.base_dir.exists():
            return []
        try:
            return sorted(
                item.name
                for item in self.base_dir.iterdir()
                if item.is_dir()
                and (item / "session.pickle").exists()
                and not (item / "metadata.json").exists()
            )
        except OSError as exc:
            LOG.error("failed_to_list_incomplete_sessions", error=str(exc))
            return []

    def delete_session(self, name: str) -> bool:
        """Delete a session.

        Args:
            name: Session identifier

        Returns:
            True if deleted, False if not found
        """
        session_dir = self.base_dir / name

        if session_dir.is_dir():
            try:
                shutil.rmtree(session_dir)
                LOG.info("session_delete_completed", name=name, structure="directory")
                return True
            except OSError as exc:
                LOG.error("failed_to_delete_session", name=name, error=str(exc))
                return False

        # Try legacy flat file structure
        old_path = self.base_dir / f"{name}.session.pickle"
        if old_path.exists():
            try:
                old_path.unlink()
                LOG.info("session_delete_completed", name=name, structure="flat_file")
                return True
            except OSError as exc:
                LOG.error("failed_to_delete_session", name=name, error=str(exc))
                return False

        return False

    def get_session_metadata(self, name: str) -> SessionMetadata | None:
        """Get session metadata without loading the full session.

        Args:
            name: Session identifier

        Returns:
            SessionMetadata if session exists, None otherwise
        """
        session_dir = self.base_dir / name
        metadata_path = session_dir / "metadata.json"

        if not metadata_path.exists():
            return None

        try:
            with metadata_path.open() as f:
                metadata_dict = json.load(f)
            return dict_to_metadata(metadata_dict)
        except (OSError, json.JSONDecodeError) as exc:
            LOG.warning("failed_to_read_session_metadata", name=name, error=str(exc))
            return None

    def update_session_metadata(
        self,
        name: str,
        status: str | None = None,
        *,
        account_identifier: Any = UNCHANGED,
        account_rule: Any = UNCHANGED,
    ) -> bool:
        """Update session metadata fields.

        Args:
            name: Session identifier
            status: New status value
            account_identifier: New account identifier (None clears it);
                left alone when not given.
            account_rule: New account rule; left alone when not given.

        Returns:
            True if updated, False if session not found

        Raises:
            ValueError: If status is not a valid value
        """
        session_dir = self.base_dir / name
        metadata_path = session_dir / "metadata.json"

        if not metadata_path.exists():
            return False

        try:
            with metadata_path.open("r") as f:
                metadata_dict = json.load(f)

            if status is not None:
                if status not in ("active", "logged_out"):
                    raise ValueError(f"Invalid status '{status}'. Must be 'active' or 'logged_out'")
                metadata_dict["status"] = status

            if account_identifier is not UNCHANGED:
                metadata_dict["account_identifier"] = account_identifier
            if account_rule is not UNCHANGED:
                metadata_dict["account_rule"] = account_rule

            metadata_dict["modified_at"] = datetime.now(UTC).isoformat()

            tmp = _write_private_temp(
                metadata_path, json.dumps(metadata_dict, indent=2, default=str).encode()
            )
            try:
                os.replace(tmp, metadata_path)
            except BaseException:
                tmp.unlink(missing_ok=True)
                raise

            LOG.info("session_metadata_updated", name=name, status=status)
            return True
        except (OSError, json.JSONDecodeError) as exc:
            LOG.error("failed_to_update_session_metadata", name=name, error=str(exc))
            return False
