"""Tests for HAR file parser."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from graftpunk.har.parser import (
    HAREntry,
    HARParseError,
    HARParseResult,
    HARRequest,
    HARResponse,
    ParseError,
    _parse_response,
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
        result = parse_har_file(sample_har_path)

        assert isinstance(result, HARParseResult)
        assert len(result.entries) == 7
        assert all(isinstance(e, HAREntry) for e in result.entries)

    def test_parse_requests(self, sample_har_path: Path) -> None:
        """Request data is parsed correctly."""
        result = parse_har_file(sample_har_path)

        # First entry is GET /login
        first = result.entries[0]
        assert first.request.method == "GET"
        assert first.request.url == "https://example.com/login"
        assert "User-Agent" in first.request.headers

    def test_parse_responses(self, sample_har_path: Path) -> None:
        """Response data is parsed correctly."""
        result = parse_har_file(sample_har_path)

        # Second entry is POST /login with redirect
        second = result.entries[1]
        assert second.response.status == 302
        assert second.response.status_text == "Found"
        assert len(second.response.cookies) == 1
        assert second.response.cookies[0]["name"] == "sessionId"

    def test_parse_post_data(self, sample_har_path: Path) -> None:
        """POST data is parsed correctly."""
        result = parse_har_file(sample_har_path)
        entries = result.entries

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
        result = parse_har_string(minimal_har)

        assert isinstance(result, HARParseResult)
        assert len(result.entries) == 1
        assert result.entries[0].request.method == "GET"
        assert result.entries[0].request.url == "https://example.com/test"

    def test_invalid_json(self) -> None:
        """HARParseError for invalid JSON string."""
        with pytest.raises(HARParseError, match="Invalid JSON"):
            parse_har_string("not json")

    def test_empty_entries(self) -> None:
        """Empty entries array returns empty list."""
        content = json.dumps({"log": {"entries": []}})
        result = parse_har_string(content)
        assert result.entries == []
        assert result.errors == []


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
        result = parse_har_string(minimal_har)
        assert result.entries[0].timestamp.year == 2024
        assert result.entries[0].timestamp.month == 1

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
        result = parse_har_string(content)
        assert result.entries[0].timestamp.month == 6

    def test_invalid_timestamp_uses_epoch_fallback(self) -> None:
        """Invalid timestamps use datetime.min (epoch) for predictable ordering."""
        content = json.dumps(
            {
                "log": {
                    "entries": [
                        {
                            "startedDateTime": "not-a-valid-timestamp",
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
        result = parse_har_string(content)

        # Should be datetime.min (year 1), not current time
        assert result.entries[0].timestamp.year == 1
        assert result.entries[0].timestamp.tzinfo is not None


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
        result = parse_har_string(content)
        assert result.entries[0].request.headers["X-Custom"] == "second"

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
        result = parse_har_string(content)
        assert "" not in result.entries[0].request.headers
        assert "Valid" in result.entries[0].request.headers


class TestHARParseResult:
    """Tests for HARParseResult and error handling."""

    def test_has_errors_property(self) -> None:
        """has_errors property reflects error count."""
        result = HARParseResult(entries=[], errors=[])
        assert result.has_errors is False

        result_with_error = HARParseResult(
            entries=[],
            errors=[ParseError(index=0, url="https://x.com", error="test error")],
        )
        assert result_with_error.has_errors is True

    def test_malformed_entry_collected_as_error(self) -> None:
        """Malformed entries are collected as errors, not raised."""
        content = json.dumps(
            {
                "log": {
                    "entries": [
                        {
                            "startedDateTime": "2024-01-01T00:00:00Z",
                            "request": {
                                "method": "GET",
                                "url": "https://valid.com/test",
                                "headers": [],
                                "cookies": [],
                            },
                            "response": {
                                "status": 200,
                                "statusText": "OK",
                                "headers": [],
                                "cookies": [],
                            },
                        },
                        {
                            # Missing required fields - malformed entry
                            "startedDateTime": "2024-01-01T00:00:00Z",
                            "request": None,  # Invalid - should cause error
                            "response": {"status": 200},
                        },
                        {
                            "startedDateTime": "2024-01-01T00:00:00Z",
                            "request": {
                                "method": "GET",
                                "url": "https://also-valid.com/test",
                                "headers": [],
                                "cookies": [],
                            },
                            "response": {
                                "status": 200,
                                "statusText": "OK",
                                "headers": [],
                                "cookies": [],
                            },
                        },
                    ]
                }
            }
        )
        result = parse_har_string(content)

        # Valid entries are parsed
        assert len(result.entries) == 2
        assert result.entries[0].request.url == "https://valid.com/test"
        assert result.entries[1].request.url == "https://also-valid.com/test"

        # Malformed entry recorded as error
        assert result.has_errors is True
        assert len(result.errors) == 1
        assert result.errors[0].index == 1
        assert result.errors[0].url == "unknown"

    def test_parse_error_contains_context(self) -> None:
        """ParseError includes index, url, and error message."""
        error = ParseError(index=5, url="https://x.com/api", error="missing field")
        assert error.index == 5
        assert error.url == "https://x.com/api"
        assert error.error == "missing field"


class TestHARParserBodyFile:
    """Tests for _bodyFile support in HAR parser."""

    def test_parse_response_with_body_file(self, tmp_path: Path) -> None:
        """_bodyFile reference loads text content from disk."""
        body_content = "<html><body>Hello</body></html>"
        body_file = tmp_path / "response_0.html"
        body_file.write_text(body_content)

        response_data = {
            "status": 200,
            "statusText": "OK",
            "headers": [],
            "cookies": [],
            "content": {
                "size": 0,
                "mimeType": "text/html",
                "_bodyFile": "response_0.html",
            },
        }
        result = _parse_response(response_data, base_dir=tmp_path)

        assert result.body == body_content
        assert result.body_size == body_file.stat().st_size
        assert result.body_file == "response_0.html"

    def test_parse_response_with_binary_body_file(self, tmp_path: Path) -> None:
        """_bodyFile for binary content stores path reference string."""
        body_file = tmp_path / "image.png"
        body_file.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)

        response_data = {
            "status": 200,
            "statusText": "OK",
            "headers": [],
            "cookies": [],
            "content": {
                "size": 0,
                "mimeType": "image/png",
                "_bodyFile": "image.png",
            },
        }
        result = _parse_response(response_data, base_dir=tmp_path)

        assert result.body == "[binary file: image.png]"
        assert result.body_size == body_file.stat().st_size
        assert result.body_file == "image.png"

    def test_parse_response_with_missing_body_file(self, tmp_path: Path) -> None:
        """Missing _bodyFile does not crash, body is None."""
        response_data = {
            "status": 200,
            "statusText": "OK",
            "headers": [],
            "cookies": [],
            "content": {
                "size": 0,
                "mimeType": "text/html",
                "_bodyFile": "nonexistent.html",
            },
        }
        result = _parse_response(response_data, base_dir=tmp_path)

        assert result.body is None
        assert result.body_file == "nonexistent.html"

    def test_parse_har_file_passes_base_dir(self, tmp_path: Path) -> None:
        """parse_har_file passes its directory as base_dir for body resolution."""
        body_content = '{"key": "value"}'
        body_file = tmp_path / "response_body.json"
        body_file.write_text(body_content)

        har_data = {
            "log": {
                "version": "1.2",
                "entries": [
                    {
                        "startedDateTime": "2024-01-15T10:00:00.000Z",
                        "request": {
                            "method": "GET",
                            "url": "https://api.example.com/data",
                            "headers": [],
                            "cookies": [],
                        },
                        "response": {
                            "status": 200,
                            "statusText": "OK",
                            "headers": [],
                            "cookies": [],
                            "content": {
                                "size": 0,
                                "mimeType": "application/json",
                                "_bodyFile": "response_body.json",
                            },
                        },
                    }
                ],
            }
        }
        har_file = tmp_path / "test.har"
        har_file.write_text(json.dumps(har_data))

        result = parse_har_file(har_file)

        assert len(result.entries) == 1
        assert result.entries[0].response.body == body_content
        assert result.entries[0].response.body_file == "response_body.json"

    def test_parse_response_prefers_inline_text_over_body_file(self) -> None:
        """If both text and _bodyFile present, text takes precedence."""
        response_data = {
            "status": 200,
            "statusText": "OK",
            "headers": [],
            "cookies": [],
            "content": {
                "size": 11,
                "mimeType": "text/plain",
                "text": "inline body",
                "_bodyFile": "should_not_load.txt",
            },
        }
        # No base_dir needed since inline text takes precedence
        result = _parse_response(response_data, base_dir=Path("/nonexistent"))

        assert result.body == "inline body"
        assert result.body_size == 11

    def test_body_file_field_set_on_response(self, tmp_path: Path) -> None:
        """body_file field on HARResponse is populated from _bodyFile."""
        response_data = {
            "status": 200,
            "statusText": "OK",
            "headers": [],
            "cookies": [],
            "content": {
                "size": 0,
                "mimeType": "text/plain",
                "_bodyFile": "some_file.txt",
            },
        }
        # No base_dir provided -- body_file should still be set
        result = _parse_response(response_data)

        assert result.body_file == "some_file.txt"
        assert result.body is None
