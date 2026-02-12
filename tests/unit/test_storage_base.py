"""Tests for graftpunk.storage.base module.

Tests for shared storage utilities including:
- SessionMetadata dataclass
- metadata_to_dict and dict_to_metadata conversion functions
- parse_datetime_iso helper function
"""

from datetime import UTC, datetime, timedelta

import pytest

from graftpunk.storage.base import (
    SessionMetadata,
    dict_to_metadata,
    metadata_to_dict,
    parse_datetime_iso,
)


@pytest.fixture
def sample_metadata():
    """Create sample session metadata."""
    now = datetime.now(UTC)
    return SessionMetadata(
        name="test-session",
        checksum="abc123",
        created_at=now,
        modified_at=now,
        expires_at=now + timedelta(hours=24),
        domain="example.com",
        current_url="https://example.com/page",
        cookie_count=5,
        cookie_domains=["example.com"],
        status="active",
    )


class TestParseDatetimeIso:
    """Tests for parse_datetime_iso function."""

    def test_standard_iso_format(self):
        """Test parsing standard ISO format with timezone."""
        result = parse_datetime_iso("2024-01-15T12:00:00+00:00")
        assert result is not None
        assert result.year == 2024
        assert result.month == 1
        assert result.day == 15
        assert result.hour == 12
        assert result.tzinfo is not None

    def test_z_suffix_format(self):
        """Test parsing ISO format with Z suffix (Supabase format)."""
        result = parse_datetime_iso("2024-06-15T12:00:00Z")
        assert result is not None
        assert result.year == 2024
        assert result.month == 6
        assert result.day == 15
        assert result.tzinfo is not None

    def test_none_returns_none(self):
        """Test that None input returns None."""
        assert parse_datetime_iso(None) is None

    def test_empty_string_returns_none(self):
        """Test that empty string returns None."""
        assert parse_datetime_iso("") is None

    def test_invalid_format_returns_none(self):
        """Test that invalid format returns None."""
        assert parse_datetime_iso("not-a-date") is None


class TestMetadataConversion:
    """Tests for dict_to_metadata and metadata_to_dict conversion functions."""

    def test_dict_to_metadata_full_data(self):
        """Test conversion of a complete dict to SessionMetadata."""
        now = datetime.now(UTC)

        data = {
            "name": "my-session",
            "checksum": "abc123",
            "created_at": now.isoformat(),
            "modified_at": now.isoformat(),
            "expires_at": (now + timedelta(hours=24)).isoformat(),
            "domain": "example.com",
            "current_url": "https://example.com/page",
            "cookie_count": 10,
            "cookie_domains": ["example.com", ".example.com"],
            "status": "active",
        }

        metadata = dict_to_metadata(data)
        assert metadata.name == "my-session"
        assert metadata.checksum == "abc123"
        assert metadata.domain == "example.com"
        assert metadata.current_url == "https://example.com/page"
        assert metadata.cookie_count == 10
        assert metadata.cookie_domains == ["example.com", ".example.com"]
        assert metadata.status == "active"
        assert metadata.expires_at is not None

    def test_dict_to_metadata_minimal_data(self):
        """Test conversion with minimal/missing fields uses defaults."""
        data = {}

        metadata = dict_to_metadata(data)
        assert metadata.name == ""
        assert metadata.checksum == ""
        assert metadata.domain is None
        assert metadata.current_url is None
        assert metadata.cookie_count == 0
        assert metadata.cookie_domains == []
        assert metadata.status == "active"
        assert metadata.expires_at is None
        # created_at and modified_at should default to now(UTC)
        assert metadata.created_at is not None
        assert metadata.modified_at is not None

    def test_dict_to_metadata_with_z_suffix_datetime(self):
        """Test conversion handles Supabase Z-suffix datetime strings."""
        data = {
            "name": "test",
            "checksum": "x",
            "created_at": "2024-06-15T12:00:00Z",
            "modified_at": "2024-06-15T13:00:00Z",
            "expires_at": "2024-06-16T12:00:00Z",
        }

        metadata = dict_to_metadata(data)
        assert metadata.created_at.year == 2024
        assert metadata.created_at.month == 6
        assert metadata.expires_at is not None

    def test_metadata_to_dict_roundtrip(self, sample_metadata):
        """Test that metadata can be converted to dict and back."""
        # Convert to dict
        data = metadata_to_dict(sample_metadata)

        # Convert back to metadata
        restored = dict_to_metadata(data)

        assert restored.name == sample_metadata.name
        assert restored.checksum == sample_metadata.checksum
        assert restored.domain == sample_metadata.domain
        assert restored.cookie_count == sample_metadata.cookie_count
        assert restored.status == sample_metadata.status

    def test_metadata_to_dict_expires_at_none(self, sample_metadata):
        """Test that None expires_at is serialized correctly."""
        from dataclasses import replace

        metadata = replace(sample_metadata, expires_at=None)
        data = metadata_to_dict(metadata)
        assert data["expires_at"] is None

    def test_metadata_to_dict_includes_all_fields(self, sample_metadata):
        """Test that all fields are included in dict output."""
        data = metadata_to_dict(sample_metadata)

        expected_keys = {
            "name",
            "checksum",
            "created_at",
            "modified_at",
            "expires_at",
            "domain",
            "current_url",
            "cookie_count",
            "cookie_domains",
            "status",
            "storage_backend",
            "storage_location",
        }
        assert set(data.keys()) == expected_keys


class TestSessionMetadata:
    """Tests for SessionMetadata dataclass."""

    def test_frozen_dataclass(self, sample_metadata):
        """Test that SessionMetadata is immutable."""
        with pytest.raises(AttributeError):
            sample_metadata.name = "new-name"  # type: ignore[misc]

    def test_default_status(self):
        """Test that status defaults to 'active'."""
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


class TestStorageFields:
    """Tests for storage_backend and storage_location fields."""

    def test_metadata_defaults_storage_fields_to_empty(self):
        """Constructing SessionMetadata without new fields defaults both to ''."""
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
        assert metadata.storage_backend == ""
        assert metadata.storage_location == ""

    def test_metadata_accepts_storage_fields(self):
        """Constructing with explicit storage fields preserves values."""
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
            storage_backend="s3",
            storage_location="s3://bucket/sessions/test",
        )
        assert metadata.storage_backend == "s3"
        assert metadata.storage_location == "s3://bucket/sessions/test"

    def test_metadata_to_dict_includes_storage_fields(self):
        """metadata_to_dict output includes storage_backend and storage_location."""
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
            storage_backend="local",
            storage_location="/home/user/.cache/graftpunk/sessions/test",
        )
        data = metadata_to_dict(metadata)
        assert data["storage_backend"] == "local"
        assert data["storage_location"] == "/home/user/.cache/graftpunk/sessions/test"

    def test_dict_to_metadata_missing_storage_fields_defaults(self):
        """dict_to_metadata defaults missing storage fields to ''."""
        data = {
            "name": "test",
            "checksum": "abc",
            "created_at": datetime.now(UTC).isoformat(),
            "modified_at": datetime.now(UTC).isoformat(),
        }
        metadata = dict_to_metadata(data)
        assert metadata.storage_backend == ""
        assert metadata.storage_location == ""

    def test_metadata_to_dict_roundtrip_with_storage_fields(self):
        """Roundtrip through metadata_to_dict/dict_to_metadata preserves storage fields."""
        now = datetime.now(UTC)
        original = SessionMetadata(
            name="roundtrip",
            checksum="xyz",
            created_at=now,
            modified_at=now,
            expires_at=None,
            domain="example.com",
            current_url="https://example.com",
            cookie_count=3,
            cookie_domains=["example.com"],
            storage_backend="s3",
            storage_location="s3://my-bucket/sessions/roundtrip",
        )
        data = metadata_to_dict(original)
        restored = dict_to_metadata(data)
        assert restored.storage_backend == original.storage_backend
        assert restored.storage_location == original.storage_location
