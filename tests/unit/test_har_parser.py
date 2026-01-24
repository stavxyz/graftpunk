"""Tests for HAR file parser."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from graftpunk.har.parser import (
    HAREntry,
    HARParseError,
    HARRequest,
    HARResponse,
    parse_har_file,
    parse_har_string,
    validate_har_schema,
)


@pytest.fixture
def sample_har_path() -> Path:
    """Path to sample HAR fixture."""
    return Path(__file__).parent.parent / "fixtures" / "sample.har"


@pytest.fixture
def minimal_har() -> str:
    """Minimal valid HAR content."""
    return json.dumps(
        {
            "log": {
                "version": "1.2",
                "entries": [
                    {
                        "startedDateTime": "2024-01-15T10:00:00.000Z",
                        "request": {
                            "method": "GET",
                            "url": "https://example.com/test",
                            "headers": [],
                            "cookies": [],
                        },
                        "response": {
                            "status": 200,
                            "statusText": "OK",
                            "headers": [],
                            "cookies": [],
                            "content": {},
                        },
                    }
                ],
            }
        }
    )


class TestValidateHarSchema:
    """Tests for HAR schema validation."""

    def test_valid_schema(self) -> None:
        """Valid HAR schema passes validation."""
        data = {"log": {"entries": []}}
        validate_har_schema(data)  # Should not raise

    def test_missing_log(self) -> None:
        """Missing 'log' object raises error."""
        with pytest.raises(HARParseError, match="must contain 'log'"):
            validate_har_schema({})

    def test_missing_entries(self) -> None:
        """Missing 'entries' array raises error."""
        with pytest.raises(HARParseError, match="must contain 'entries'"):
            validate_har_schema({"log": {}})

    def test_log_not_object(self) -> None:
        """Non-object 'log' raises error."""
        with pytest.raises(HARParseError, match="'log' must be an object"):
            validate_har_schema({"log": "not an object"})

    def test_entries_not_array(self) -> None:
        """Non-array 'entries' raises error."""
        with pytest.raises(HARParseError, match="'entries' must be an array"):
            validate_har_schema({"log": {"entries": "not an array"}})

    def test_not_dict(self) -> None:
        """Non-dict data raises error."""
        with pytest.raises(HARParseError, match="must contain a JSON object"):
            validate_har_schema([])  # type: ignore[arg-type]


class TestParseHarFile:
    """Tests for parsing HAR files from disk."""

    def test_parse_sample_har(self, sample_har_path: Path) -> None:
        """Parse sample HAR file successfully."""
        entries = parse_har_file(sample_har_path)

        assert len(entries) == 7
        assert all(isinstance(e, HAREntry) for e in entries)

    def test_parse_requests(self, sample_har_path: Path) -> None:
        """Request data is parsed correctly."""
        entries = parse_har_file(sample_har_path)

        # First entry is GET /login
        first = entries[0]
        assert first.request.method == "GET"
        assert first.request.url == "https://example.com/login"
        assert "User-Agent" in first.request.headers

    def test_parse_responses(self, sample_har_path: Path) -> None:
        """Response data is parsed correctly."""
        entries = parse_har_file(sample_har_path)

        # Second entry is POST /login with redirect
        second = entries[1]
        assert second.response.status == 302
        assert second.response.status_text == "Found"
        assert len(second.response.cookies) == 1
        assert second.response.cookies[0]["name"] == "sessionId"

    def test_parse_post_data(self, sample_har_path: Path) -> None:
        """POST data is parsed correctly."""
        entries = parse_har_file(sample_har_path)

        # Second entry has POST data
        second = entries[1]
        assert second.request.post_data is not None
        assert "username" in second.request.post_data

    def test_file_not_found(self, tmp_path: Path) -> None:
        """FileNotFoundError for missing file."""
        with pytest.raises(FileNotFoundError):
            parse_har_file(tmp_path / "nonexistent.har")

    def test_invalid_json(self, tmp_path: Path) -> None:
        """HARParseError for invalid JSON."""
        bad_file = tmp_path / "bad.har"
        bad_file.write_text("not json")

        with pytest.raises(HARParseError, match="Invalid JSON"):
            parse_har_file(bad_file)

    def test_invalid_schema(self, tmp_path: Path) -> None:
        """HARParseError for invalid schema."""
        bad_file = tmp_path / "bad.har"
        bad_file.write_text('{"not": "valid har"}')

        with pytest.raises(HARParseError, match="must contain 'log'"):
            parse_har_file(bad_file)


class TestParseHarString:
    """Tests for parsing HAR content from strings."""

    def test_parse_minimal(self, minimal_har: str) -> None:
        """Parse minimal HAR string."""
        entries = parse_har_string(minimal_har)

        assert len(entries) == 1
        assert entries[0].request.method == "GET"
        assert entries[0].request.url == "https://example.com/test"

    def test_invalid_json(self) -> None:
        """HARParseError for invalid JSON string."""
        with pytest.raises(HARParseError, match="Invalid JSON"):
            parse_har_string("not json")

    def test_empty_entries(self) -> None:
        """Empty entries array returns empty list."""
        content = json.dumps({"log": {"entries": []}})
        entries = parse_har_string(content)
        assert entries == []


class TestHARRequest:
    """Tests for HARRequest dataclass."""

    def test_default_values(self) -> None:
        """Default values are set correctly."""
        request = HARRequest(
            method="GET",
            url="https://example.com",
            headers={},
            cookies=[],
        )
        assert request.post_data is None
        assert request.query_string == []


class TestHARResponse:
    """Tests for HARResponse dataclass."""

    def test_default_values(self) -> None:
        """Default values are set correctly."""
        response = HARResponse(
            status=200,
            status_text="OK",
            headers={},
            cookies=[],
        )
        assert response.content_type is None
        assert response.body is None
        assert response.body_size == 0


class TestTimestampParsing:
    """Tests for timestamp parsing edge cases."""

    def test_z_suffix(self, minimal_har: str) -> None:
        """Timestamps with Z suffix are parsed."""
        entries = parse_har_string(minimal_har)
        assert entries[0].timestamp.year == 2024
        assert entries[0].timestamp.month == 1

    def test_timezone_offset(self) -> None:
        """Timestamps with timezone offset are parsed."""
        content = json.dumps(
            {
                "log": {
                    "entries": [
                        {
                            "startedDateTime": "2024-06-15T14:30:00+05:00",
                            "request": {
                                "method": "GET",
                                "url": "https://x.com",
                                "headers": [],
                                "cookies": [],
                            },
                            "response": {
                                "status": 200,
                                "statusText": "OK",
                                "headers": [],
                                "cookies": [],
                            },
                        }
                    ]
                }
            }
        )
        entries = parse_har_string(content)
        assert entries[0].timestamp.month == 6


class TestHeaderParsing:
    """Tests for header parsing."""

    def test_duplicate_headers(self) -> None:
        """Later headers overwrite earlier ones."""
        content = json.dumps(
            {
                "log": {
                    "entries": [
                        {
                            "startedDateTime": "2024-01-01T00:00:00Z",
                            "request": {
                                "method": "GET",
                                "url": "https://x.com",
                                "headers": [
                                    {"name": "X-Custom", "value": "first"},
                                    {"name": "X-Custom", "value": "second"},
                                ],
                                "cookies": [],
                            },
                            "response": {
                                "status": 200,
                                "statusText": "OK",
                                "headers": [],
                                "cookies": [],
                            },
                        }
                    ]
                }
            }
        )
        entries = parse_har_string(content)
        assert entries[0].request.headers["X-Custom"] == "second"

    def test_empty_header_name(self) -> None:
        """Empty header names are skipped."""
        content = json.dumps(
            {
                "log": {
                    "entries": [
                        {
                            "startedDateTime": "2024-01-01T00:00:00Z",
                            "request": {
                                "method": "GET",
                                "url": "https://x.com",
                                "headers": [
                                    {"name": "", "value": "ignored"},
                                    {"name": "Valid", "value": "kept"},
                                ],
                                "cookies": [],
                            },
                            "response": {
                                "status": 200,
                                "statusText": "OK",
                                "headers": [],
                                "cookies": [],
                            },
                        }
                    ]
                }
            }
        )
        entries = parse_har_string(content)
        assert "" not in entries[0].request.headers
        assert "Valid" in entries[0].request.headers
