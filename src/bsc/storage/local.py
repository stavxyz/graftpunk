"""Local filesystem session storage backend."""

import json
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bsc.exceptions import SessionExpiredError, SessionNotFoundError
from bsc.logging import get_logger
from bsc.storage.base import SessionMetadata, parse_datetime_iso

LOG = get_logger(__name__)


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
        session_dir.mkdir(parents=True, exist_ok=True)

        pickle_path = session_dir / "session.pickle"
        metadata_path = session_dir / "metadata.json"

        LOG.info("session_save_started", name=name, path=str(pickle_path))

        # Write encrypted pickle file with secure permissions from creation
        fd = os.open(pickle_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(encrypted_data)

        # Write metadata
        metadata_dict = self._metadata_to_dict(metadata)
        with metadata_path.open("w") as f:
            json.dump(metadata_dict, f, indent=2, default=str)

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

            LOG.warning("session_not_found", name=name)
            raise SessionNotFoundError(f"Session '{name}' not found")

        pickle_path = session_dir / "session.pickle"
        metadata_path = session_dir / "metadata.json"

        if not pickle_path.exists():
            LOG.warning("session_pickle_not_found", name=name)
            raise SessionNotFoundError(f"Session '{name}' not found")

        LOG.info("session_load_started", name=name, structure="directory")

        # Load and validate metadata
        if not metadata_path.exists():
            LOG.error("session_metadata_missing", name=name)
            raise SessionExpiredError(
                f"Session '{name}' is missing metadata. Please run 'bsc clear' and re-login."
            )

        try:
            with metadata_path.open() as f:
                metadata_dict = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            LOG.error("session_metadata_invalid", name=name, error=str(exc))
            raise SessionExpiredError(
                f"Session '{name}' has invalid metadata. Please run 'bsc clear' and re-login."
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
                        f"Session '{name}' has expired (TTL). Please run 'bsc clear' and re-login."
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
            encrypted_data = f.read()

        metadata = self._dict_to_metadata(metadata_dict)
        LOG.info("session_load_completed", name=name)
        return encrypted_data, metadata

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
        stat = path.stat()
        metadata = SessionMetadata(
            name=name,
            checksum="",  # Unknown for legacy sessions
            created_at=datetime.fromtimestamp(stat.st_ctime, tz=UTC),
            modified_at=datetime.fromtimestamp(stat.st_mtime, tz=UTC),
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
                if item.is_dir() and (item / "session.pickle").exists():
                    names.append(item.name)
                elif item.is_file() and item.suffix == ".pickle":
                    # Legacy flat file structure
                    names.append(item.stem.replace(".session", ""))

            LOG.debug("session_list_completed", count=len(names))
            return sorted(names)
        except OSError as exc:
            LOG.error("failed_to_list_sessions", error=str(exc))
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
            return self._dict_to_metadata(metadata_dict)
        except (OSError, json.JSONDecodeError) as exc:
            LOG.warning("failed_to_read_session_metadata", name=name, error=str(exc))
            return None

    def update_session_metadata(
        self,
        name: str,
        status: str | None = None,
    ) -> bool:
        """Update session metadata fields.

        Args:
            name: Session identifier
            status: New status value

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

            metadata_dict["modified_at"] = datetime.now(UTC).isoformat()

            with metadata_path.open("w") as f:
                json.dump(metadata_dict, f, indent=2, default=str)

            LOG.info("session_metadata_updated", name=name, status=status)
            return True
        except (OSError, json.JSONDecodeError) as exc:
            LOG.error("failed_to_update_session_metadata", name=name, error=str(exc))
            return False

    def _metadata_to_dict(self, metadata: SessionMetadata) -> dict[str, Any]:
        """Convert SessionMetadata to dictionary for JSON serialization.

        Args:
            metadata: SessionMetadata instance

        Returns:
            Dictionary representation
        """
        return {
            "name": metadata.name,
            "checksum": metadata.checksum,
            "created_at": metadata.created_at.isoformat(),
            "modified_at": metadata.modified_at.isoformat(),
            "expires_at": metadata.expires_at.isoformat() if metadata.expires_at else None,
            "domain": metadata.domain,
            "current_url": metadata.current_url,
            "cookie_count": metadata.cookie_count,
            "cookie_domains": metadata.cookie_domains,
            "status": metadata.status,
        }

    def _dict_to_metadata(self, data: dict[str, Any]) -> SessionMetadata:
        """Convert dictionary to SessionMetadata.

        Args:
            data: Dictionary from JSON

        Returns:
            SessionMetadata instance
        """
        # Parse datetime strings using shared utility
        created_at = parse_datetime_iso(data.get("created_at"))
        modified_at = parse_datetime_iso(data.get("modified_at"))
        expires_at = parse_datetime_iso(data.get("expires_at"))

        return SessionMetadata(
            name=data.get("name", ""),
            checksum=data.get("checksum", ""),
            created_at=created_at or datetime.now(UTC),
            modified_at=modified_at or datetime.now(UTC),
            expires_at=expires_at,
            domain=data.get("domain"),
            current_url=data.get("current_url"),
            cookie_count=data.get("cookie_count", 0),
            cookie_domains=data.get("cookie_domains", []),
            status=data.get("status", "active"),
        )
