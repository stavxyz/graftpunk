"""Tests for HAR file parser."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

from graftpunk.har.parser import (
    HAREntry,
    HARParseError,
    HARParseResult,
    HARRequest,
    HARResponse,
    LoginInteraction,
    ParseError,
    _parse_response,
    _set_cookie_lines,
    parse_har_file,
    parse_har_string,
    validate_har_schema,
)
from tests.unit.guide_harness import REPO_ROOT


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
        """HARParseError names the file, once, for invalid JSON: an author with
        several recordings cannot tell which one is broken otherwise."""
        bad_file = tmp_path / "bad.har"
        bad_file.write_text("not json")

        with pytest.raises(HARParseError, match="not valid JSON") as caught:
            parse_har_file(bad_file)
        assert str(caught.value).count(str(bad_file)) == 1

    def test_invalid_schema(self, tmp_path: Path) -> None:
        """HARParseError names the file, once, for an invalid schema too:
        validate_har_schema's own messages carry no path, so parse_har_file
        is the one place that adds it."""
        bad_file = tmp_path / "bad.har"
        bad_file.write_text('{"not": "valid har"}')

        with pytest.raises(HARParseError, match="must contain 'log'") as caught:
            parse_har_file(bad_file)
        assert str(caught.value).count(str(bad_file)) == 1

    def test_a_directory_is_named_once_not_twice(self, tmp_path: Path) -> None:
        """str(OSError) already ends with the path (repr-quoted); using
        exc.strerror instead of str(exc) keeps the f-string's own prefix the
        only copy."""
        a_directory = tmp_path / "a_directory.har"
        a_directory.mkdir()

        with pytest.raises(HARParseError, match="Is a directory") as caught:
            parse_har_file(a_directory)
        assert str(caught.value).count(str(a_directory)) == 1

    def test_non_utf8_file_is_a_har_parse_error(self, tmp_path: Path) -> None:
        """A HAR file that is not valid UTF-8 must not raise UnicodeDecodeError
        past this function: every caller catches (FileNotFoundError, HARParseError)."""
        bad_file = tmp_path / "bad.har"
        bad_file.write_bytes(b"\xff\xfe")

        with pytest.raises(HARParseError, match="not valid UTF-8"):
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
        assert response.set_cookie_names == ()
        assert response.redirect_url == ""

    def test_redirect_url_comes_from_the_har_field(self) -> None:
        """Chrome records the target in the entry's own redirectURL."""
        response = _parse_response(
            {
                "status": 302,
                "statusText": "Found",
                "headers": [{"name": "Location", "value": "/ignored"}],
                "cookies": [],
                "redirectURL": "https://app.example.com/dashboard",
            }
        )
        assert response.redirect_url == "https://app.example.com/dashboard"

    def test_redirect_url_falls_back_to_the_location_header(self) -> None:
        """Recorders that leave redirectURL empty still carry Location."""
        response = _parse_response(
            {
                "status": 302,
                "statusText": "Found",
                "headers": [{"name": "location", "value": "/dashboard"}],
                "cookies": [],
                "redirectURL": "",
            }
        )
        assert response.redirect_url == "/dashboard"

    def test_redirect_url_is_empty_without_either(self) -> None:
        """Neither source present means no target, not a guess."""
        response = _parse_response(
            {
                "status": 200,
                "statusText": "OK",
                "headers": [{"name": "Content-Type", "value": "text/html"}],
                "cookies": [],
            }
        )
        assert response.redirect_url == ""

    def test_multiple_set_cookie_headers_all_names_captured(self) -> None:
        """Every Set-Cookie header's name is kept, not just the last one."""
        response = _parse_response(
            {
                "status": 200,
                "statusText": "OK",
                "headers": [
                    {"name": "Set-Cookie", "value": "session_id=abc123; Path=/"},
                    {"name": "Set-Cookie", "value": "csrf_token=xyz; HttpOnly"},
                ],
                "cookies": [],
            }
        )
        assert response.set_cookie_names == ("session_id", "csrf_token")

    def test_every_line_of_a_newline_joined_set_cookie_value_is_a_cookie(self) -> None:
        """Chromium joins several Set-Cookie headers into one value with newlines;
        both name fields read every line, with one parse."""
        response = _parse_response(
            {
                "status": 200,
                "statusText": "OK",
                "headers": [
                    {
                        "name": "set-cookie",
                        "value": "lot=12; Path=/\nmyshop_session=Zq9Xk; HttpOnly\nnot a cookie",
                    },
                    {"name": "Set-Cookie", "value": "csrftoken=abc; path=/; httponly=1"},
                ],
                "cookies": [],
            }
        )
        assert response.set_cookie_names == ("lot", "myshop_session", "csrftoken")
        assert response.set_cookie_http_only == ("myshop_session", "csrftoken")

    @pytest.mark.parametrize(
        ("value", "names", "http_only"),
        [
            ("a=1\rb=2; HttpOnly", ("a", "b"), ("b",)),
            ("a=1; HttpOnly\r\nb=2", ("a", "b"), ("a",)),
            ("a=1\n\nb=2; HttpOnly\n", ("a", "b"), ("b",)),
            ("a=1; Http-Only\nb=2; HttpOnlyX\nc=3; Http Only", ("a", "b", "c"), ()),
        ],
        ids=["bare-carriage-return", "crlf", "empty-lines", "near-miss-attributes"],
    )
    def test_set_cookie_lines_end_at_any_line_break_and_match_httponly_exactly(
        self, value: str, names: tuple[str, ...], http_only: tuple[str, ...]
    ) -> None:
        response = _parse_response(
            {
                "status": 200,
                "statusText": "OK",
                "headers": [{"name": "Set-Cookie", "value": value}],
                "cookies": [],
            }
        )
        assert response.set_cookie_names == names
        assert response.set_cookie_http_only == http_only

    def test_no_set_cookie_headers_is_empty(self) -> None:
        """A response with no Set-Cookie header has an empty tuple, not None."""
        response = _parse_response(
            {
                "status": 200,
                "statusText": "OK",
                "headers": [{"name": "Content-Type", "value": "application/json"}],
                "cookies": [],
            }
        )
        assert response.set_cookie_names == ()


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

    @staticmethod
    def _request_with_headers(headers: list[dict], cookies: list | None = None) -> HARRequest:
        request: dict = {
            "method": "GET",
            "url": "https://myshop.example.com/",
            "headers": headers,
        }
        if cookies is not None:
            request["cookies"] = cookies
        entry = {
            "startedDateTime": "2024-01-01T00:00:00Z",
            "request": request,
            "response": {"status": 200, "statusText": "OK", "headers": [], "cookies": []},
        }
        result = parse_har_string(json.dumps({"log": {"entries": [entry]}}))
        assert not result.errors
        return result.entries[0].request

    def test_cookie_headers_fill_a_missing_cookies_array(self) -> None:
        """Each line of a joined value and each ; pair is a cookie; a pair with no
        = or no name is skipped; names and values are stripped."""
        request = self._request_with_headers(
            [{"name": "Cookie", "value": "a=1\nb= two ; flag; c=3;=orphan"}]
        )
        assert request.cookies == [
            {"name": "a", "value": "1"},
            {"name": "b", "value": "two"},
            {"name": "c", "value": "3"},
        ]

    @pytest.mark.parametrize(
        "header",
        [
            {"name": "Cookie", "value": None},
            {"name": "Cookie", "value": 7},
            {"name": None, "value": "a=1"},
            {"name": 5, "value": "a=1"},
            {"value": "a=1"},
        ],
        ids=["null-value", "number-value", "null-name", "number-name", "no-name"],
    )
    def test_a_malformed_cookie_header_is_skipped_not_fatal(self, header: dict) -> None:
        """The fallback must never fail an entry that parses without it."""
        request = self._request_with_headers([header, {"name": "cookie", "value": "b=2"}])
        assert request.cookies == [{"name": "b", "value": "2"}]

    @pytest.mark.parametrize("array", ["absent", "empty"])
    def test_every_cookie_header_counts_when_the_array_has_none(self, array: str) -> None:
        """HTTP/2 can split one Cookie header into several, in any case."""
        headers = [
            {"name": "cookie", "value": "sessionid=s1; lot=12"},
            {"name": "Cookie", "value": "visit=v1"},
        ]
        request = self._request_with_headers(headers, None if array == "absent" else [])
        assert request.cookies == [
            {"name": "sessionid", "value": "s1"},
            {"name": "lot", "value": "12"},
            {"name": "visit", "value": "v1"},
        ]

    def test_the_cookie_header_is_not_read_when_the_array_has_cookies(self) -> None:
        """The array is the recorder's own parse of the same header; reading both
        would count one cookie twice."""
        request = self._request_with_headers(
            [{"name": "Cookie", "value": "lot=12; extra=1"}], [{"name": "lot", "value": "12"}]
        )
        assert request.cookies == [{"name": "lot", "value": "12"}]

    def test_the_recorder_and_the_parser_read_a_cookie_header_alike(self) -> None:
        """The recorder writes request.cookies from the same helper the parser
        falls back to, so a HAR with or without the array gives one answer."""
        from graftpunk.observe.capture import _request_cookies

        value = "a=1\rb= two ; flag\r\nc=3;=orphan\nd=4"
        parsed = self._request_with_headers([{"name": "Cookie", "value": value}])
        assert parsed.cookies == [
            {"name": "a", "value": "1"},
            {"name": "b", "value": "two"},
            {"name": "c", "value": "3"},
            {"name": "d", "value": "4"},
        ]
        assert _request_cookies({"Cookie": value}) == parsed.cookies


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
        assert result.body_file_outside is False

    @pytest.mark.parametrize("how", ["dotdot", "symlink"])
    def test_a_body_file_outside_the_har_directory_is_not_read(
        self, tmp_path: Path, how: str
    ) -> None:
        """The same refusal as a request body file's and a DOM snapshot's: a
        reference or a symlink that leaves the HAR's directory is not followed,
        so the body counts as not captured."""
        run = tmp_path / "run"
        (run / "bodies").mkdir(parents=True)
        outside = tmp_path / "shared.html"
        outside.write_text("<html>elsewhere</html>")
        if how == "dotdot":
            ref = "../shared.html"
        else:
            ref = "bodies/1.html"
            (run / "bodies" / "1.html").symlink_to(outside)
        response_data = {
            "status": 200,
            "statusText": "OK",
            "headers": [],
            "cookies": [],
            "content": {"size": 0, "mimeType": "text/html", "_bodyFile": ref},
        }
        result = _parse_response(response_data, base_dir=run)

        assert result.body is None
        assert result.body_file == ref
        assert result.body_file_outside is True

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


# The graft skill's HAR reader runs on the standard library alone, so it carries
# its own copy of the Set-Cookie parse; these hold the two equal.
_HAR_FACTS = REPO_ROOT / "skills" / "graft" / "scripts" / "har-facts.py"


def _load_har_facts() -> ModuleType:
    spec = importlib.util.spec_from_file_location("har_facts", _HAR_FACTS)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _header(name: str, value: str) -> dict[str, str]:
    return {"name": name, "value": value}


@pytest.mark.parametrize(
    "headers",
    [
        [_header("Set-Cookie", "a=1; HttpOnly")],
        [_header("set-cookie", "a=1; Path=/\r\nb=2; httponly\rc=3;HTTPONLY=x\n\nd")],
        [
            _header("Set-Cookie", "=v; HttpOnly"),
            _header("Set-Cookie", " e = 5 ; Secure; HttpOnly "),
        ],
        [
            _header("Set-Cookie", "f=1; Path=/"),
            _header("X-Other", "g=1; HttpOnly"),
            _header("Set-Cookie", "f=2; HttpOnly"),
        ],
        [_header("Set-Cookie", "noequals; HttpOnly"), _header("Set-Cookie", "h=a=b; Secure")],
        [],
    ],
)
def test_the_set_cookie_parse_is_graftpunks(headers: list[dict[str, str]]) -> None:
    """The script cannot import graftpunk, so it carries its own copy of the parse
    the digest reads cookie flags with; the two must agree."""
    script = _load_har_facts()
    ours = [
        (name, "httponly" in attributes) for name, attributes in script.set_cookie_lines(headers)
    ]
    assert ours == _set_cookie_lines(headers)


class TestDomSnapshotFields:
    """``request._domSnapshot`` and ``request._domSnapshotUrl`` (#267, part 2)."""

    @staticmethod
    def _har(tmp_path: Path, request_extra: dict[str, object]) -> Path:
        request = {
            "method": "POST",
            "url": "https://myshop.example/api/login",
            "headers": [],
            "cookies": [],
            **request_extra,
        }
        entry = {
            "startedDateTime": "2026-10-24T10:00:00.000Z",
            "time": 1,
            "request": request,
            "response": {
                "status": 200,
                "statusText": "OK",
                "headers": [],
                "cookies": [],
                "content": {"mimeType": "application/json", "text": "{}"},
            },
        }
        run = tmp_path / "run"
        run.mkdir(exist_ok=True)
        path = run / "network.har"
        path.write_text(json.dumps({"log": {"version": "1.2", "entries": [entry]}}))
        return path

    def test_both_fields_are_read(self, tmp_path: Path) -> None:
        har = self._har(
            tmp_path,
            {
                "_domSnapshot": "snapshots/7.1.html",
                "_domSnapshotUrl": "https://myshop.example/",
            },
        )
        (har.parent / "snapshots").mkdir()
        (har.parent / "snapshots" / "7.1.html").write_text("<div id='m'></div>")

        (entry,) = parse_har_file(har).entries

        assert entry.request.dom_snapshot_file == "snapshots/7.1.html"
        assert entry.request.dom_snapshot == "<div id='m'></div>"
        assert entry.request.dom_snapshot_url == "https://myshop.example/"

    def test_a_path_outside_the_har_directory_is_refused(self, tmp_path: Path) -> None:
        (tmp_path / "outside.html").write_text("<div id='elsewhere'></div>")
        har = self._har(
            tmp_path,
            {"_domSnapshot": "../outside.html", "_domSnapshotUrl": "https://myshop.example/"},
        )

        (entry,) = parse_har_file(har).entries

        assert entry.request.dom_snapshot is None
        assert entry.request.dom_snapshot_file is None

    def test_a_missing_snapshot_or_none_at_all_reads_as_none(self, tmp_path: Path) -> None:
        har = self._har(tmp_path, {"_domSnapshot": "snapshots/gone.html"})
        (entry,) = parse_har_file(har).entries
        assert entry.request.dom_snapshot is None

        plain = HARRequest(method="GET", url="https://myshop.example/", headers={}, cookies=[])
        assert (plain.dom_snapshot, plain.dom_snapshot_url) == (None, None)

    def test_the_login_interactions_are_read_oldest_first(self, tmp_path: Path) -> None:
        """``request._loginInteractions`` (#267, part 3): kind, selector, msBefore."""
        har = self._har(
            tmp_path,
            {
                "_loginInteractions": [
                    {"kind": "click", "selector": "#signin-link", "msBefore": 5000},
                    {"kind": "focus", "selector": "#m-email", "msBefore": 3000.5},
                    {"kind": "click", "selector": None, "msBefore": 40},
                ]
            },
        )
        (entry,) = parse_har_file(har).entries
        assert entry.request.login_interactions == (
            LoginInteraction("click", "#signin-link", 5000.0),
            LoginInteraction("focus", "#m-email", 3000.5),
            LoginInteraction("click", None, 40.0),
        )

    def test_they_are_put_oldest_first_whatever_the_file_order(self, tmp_path: Path) -> None:
        har = self._har(
            tmp_path,
            {
                "_loginInteractions": [
                    {"kind": "focus", "selector": "#m-email", "msBefore": 10},
                    {"kind": "click", "selector": "#signin-link", "msBefore": 900},
                ]
            },
        )
        (entry,) = parse_har_file(har).entries
        assert [i.kind for i in entry.request.login_interactions] == ["click", "focus"]

    def test_a_malformed_interaction_is_skipped(self, tmp_path: Path) -> None:
        har = self._har(
            tmp_path,
            {
                "_loginInteractions": [
                    {"kind": "scroll", "selector": "#a", "msBefore": 1},
                    {"kind": "click", "selector": 7, "msBefore": 1},
                    {"kind": "click", "selector": "#a", "msBefore": -1},
                    {"kind": "click", "selector": "#a", "msBefore": True},
                    {"kind": "click", "selector": "#a", "msBefore": float("inf")},
                    {"kind": "click", "selector": "#a"},
                    "click",
                    {"kind": "focus", "msBefore": 2},
                ]
            },
        )
        (entry,) = parse_har_file(har).entries
        assert entry.request.login_interactions == (LoginInteraction("focus", None, 2.0),)

    def test_no_interactions_or_a_wrong_shape_reads_as_none(self, tmp_path: Path) -> None:
        har = self._har(tmp_path, {"_loginInteractions": {"kind": "click"}})
        (entry,) = parse_har_file(har).entries
        assert entry.request.login_interactions == ()
        plain = HARRequest(method="GET", url="https://myshop.example/", headers={}, cookies=[])
        assert plain.login_interactions == ()

    def test_the_in_login_flag_and_the_selector_cap(self, tmp_path: Path) -> None:
        """Review round 1: a click flagged inside the login, and a selector over 256
        characters read as none."""
        har = self._har(
            tmp_path,
            {
                "_loginInteractions": [
                    {"kind": "click", "selector": "#a" + "b" * 255, "msBefore": 30},
                    {"kind": "click", "selector": "#a" + "b" * 254, "msBefore": 20},
                    {"kind": "click", "selector": "#lbl", "msBefore": 10, "inLogin": True},
                    {"kind": "click", "selector": "#x", "msBefore": 5, "inLogin": "yes"},
                ]
            },
        )
        (entry,) = parse_har_file(har).entries
        assert entry.request.login_interactions == (
            LoginInteraction("click", None, 30.0),
            LoginInteraction("click", "#a" + "b" * 254, 20.0),
            LoginInteraction("click", "#lbl", 10.0, in_login=True),
            LoginInteraction("click", "#x", 5.0),
        )

    def test_the_resource_type_is_read(self, tmp_path: Path) -> None:
        (entry,) = parse_har_file(self._har(tmp_path, {"_resourceType": "Document"})).entries
        assert entry.request.resource_type == "Document"
        (other,) = parse_har_file(self._har(tmp_path, {"_resourceType": 7})).entries
        assert other.request.resource_type is None

    def test_a_click_s_state_is_read_only_when_true(self, tmp_path: Path) -> None:
        """Review rounds 2 and 3: control, inDialog, and revealed, each a boolean; the
        shown fields, bounded; and the page's path."""
        har = self._har(
            tmp_path,
            {
                "_loginInteractions": [
                    {
                        "kind": "click",
                        "selector": "#a",
                        "msBefore": 30,
                        "control": True,
                        "inDialog": False,
                        "revealed": True,
                        "shownFields": [
                            "#m-email",
                            "#" + "a" * 300,
                            7,
                            *[f"#f{n}" for n in range(20)],
                        ],
                        "path": "/login",
                    },
                    {
                        "kind": "click",
                        "selector": "",
                        "msBefore": 20,
                        "control": "yes",
                        "inDialog": 1,
                        "revealed": "true",
                        "shownFields": "#m-email",
                        "path": 7,
                    },
                ]
            },
        )
        (entry,) = parse_har_file(har).entries
        assert entry.request.login_interactions == (
            LoginInteraction(
                "click",
                "#a",
                30.0,
                control=True,
                revealed=True,
                shown_fields=("#m-email", *[f"#f{n}" for n in range(15)]),
                path="/login",
            ),
            LoginInteraction("click", None, 20.0),
        )
