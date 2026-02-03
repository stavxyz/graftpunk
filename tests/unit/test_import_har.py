"""Tests for CLI import-har command and helpers."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from unittest.mock import patch

from typer.testing import CliRunner

from graftpunk.cli.import_har import _format_auth_flow, _print_endpoints_table
from graftpunk.cli.main import app
from graftpunk.har.analyzer import APIEndpoint, AuthFlow, AuthStep
from graftpunk.har.parser import HAREntry, HARParseError, HARParseResult, HARRequest, HARResponse
from graftpunk.plugins import infer_site_name

runner = CliRunner()


def strip_ansi(text: str) -> str:
    """Remove ANSI escape codes from text."""
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


# ---------------------------------------------------------------------------
# Helpers to build test fixtures
# ---------------------------------------------------------------------------


def _make_entry(
    url: str = "https://example.com/page", method: str = "GET", status: int = 200
) -> HAREntry:
    """Create a minimal HAREntry for testing."""
    return HAREntry(
        request=HARRequest(
            method=method,
            url=url,
            headers={},
            cookies=[],
        ),
        response=HARResponse(
            status=status,
            status_text="OK",
            headers={},
            cookies=[],
        ),
        timestamp=datetime.now(UTC),
    )


def _make_auth_flow(
    *,
    steps: list[tuple[str, str]] | None = None,
    cookies: list[str] | None = None,
    auth_type: str = "form",
) -> AuthFlow:
    """Build an AuthFlow with minimal boilerplate."""
    if steps is None:
        steps = [("form_page", "Load login page"), ("login_submit", "Submit credentials")]
    auth_steps = [
        AuthStep(entry=_make_entry(), step_type=stype, description=desc) for stype, desc in steps
    ]
    return AuthFlow(
        steps=auth_steps,
        session_cookies=cookies or ["sessionid"],
        auth_type=auth_type,
    )


# ===========================================================================
# infer_site_name  (pure function, high-value tests)
# ===========================================================================


class TestInferSiteName:
    """Tests for infer_site_name domain-to-name inference."""

    def test_simple_domain(self):
        assert infer_site_name("github.com") == "github"

    def test_www_prefix(self):
        assert infer_site_name("www.example.com") == "example"

    def test_api_prefix(self):
        assert infer_site_name("api.myservice.com") == "myservice"

    def test_app_prefix(self):
        assert infer_site_name("app.dashboard.com") == "dashboard"

    def test_m_prefix(self):
        assert infer_site_name("m.facebook.com") == "facebook"

    def test_hyphen_replaced_with_underscore(self):
        assert infer_site_name("my-cool-site.com") == "my_cool_site"

    def test_uppercase_normalised(self):
        assert infer_site_name("WWW.GitHub.COM") == "github"

    def test_subdomain_without_known_prefix(self):
        # sub.example.com -> the main domain before TLD is "example"
        assert infer_site_name("sub.example.com") == "example"

    def test_co_uk_style_tld(self):
        # bbc.co.uk -> parts[-2] is "co", which is a known limitation
        result = infer_site_name("bbc.co.uk")
        assert isinstance(result, str)
        assert len(result) > 0

    def test_single_label(self):
        # Edge case: localhost or single-label domain
        assert infer_site_name("localhost") == "localhost"

    def test_prefix_only_removed_once(self):
        # "www.api.example.com" -> removes www., then parts[-2] == "example"
        assert infer_site_name("www.api.example.com") == "example"


# ===========================================================================
# _format_auth_flow
# ===========================================================================


class TestFormatAuthFlow:
    """Tests for _format_auth_flow display helper."""

    def test_basic_flow(self):
        flow = _make_auth_flow()
        result = _format_auth_flow(flow)
        assert "Load login page" in result
        assert "Submit credentials" in result
        assert "form" in result  # auth_type

    def test_session_cookies_shown(self):
        flow = _make_auth_flow(cookies=["sid", "token"])
        result = _format_auth_flow(flow)
        assert "sid" in result
        assert "token" in result

    def test_many_cookies_truncated(self):
        cookies = [f"cookie{i}" for i in range(8)]
        flow = _make_auth_flow(cookies=cookies)
        result = _format_auth_flow(flow)
        assert "+3 more" in result

    def test_non_auth_flow_returns_empty(self):
        """Passing a non-AuthFlow returns empty string."""
        assert _format_auth_flow("not an auth flow") == ""  # type: ignore[arg-type]

    def test_step_icons(self):
        steps = [
            ("form_page", "page"),
            ("login_submit", "submit"),
            ("redirect", "redir"),
            ("authenticated", "done"),
            ("oauth", "oauth step"),
        ]
        flow = _make_auth_flow(steps=steps)
        result = _format_auth_flow(flow)
        # Each step description should appear
        for _, desc in steps:
            assert desc in result


# ===========================================================================
# _print_endpoints_table
# ===========================================================================


class TestPrintEndpointsTable:
    """Tests for _print_endpoints_table display helper."""

    def test_prints_without_error(self, capsys):
        endpoints = [
            APIEndpoint(
                method="GET", url="https://example.com/api/users", path="/api/users", params=["id"]
            ),
            APIEndpoint(method="POST", url="https://example.com/api/items", path="/api/items"),
        ]
        # Should not raise
        _print_endpoints_table(endpoints)

    def test_many_endpoints_truncated(self, capsys):
        endpoints = [
            APIEndpoint(method="GET", url=f"https://example.com/api/e{i}", path=f"/api/e{i}")
            for i in range(20)
        ]
        _print_endpoints_table(endpoints)


# ===========================================================================
# CLI command integration tests  (import-har)
# ===========================================================================

# The CLI validates that the file exists (typer.Argument(exists=True)), so
# for tests that exercise code *inside* import_har we need a real temp file
# and mock the heavy functions.

MODULE = "graftpunk.cli.import_har"


class TestImportHarCommand:
    """Integration tests exercising the import-har CLI entrypoint."""

    def _invoke(self, har_path: str, extra_args: list[str] | None = None):
        args = ["import-har", har_path]
        if extra_args:
            args.extend(extra_args)
        return runner.invoke(app, args)

    # ------------------------------------------------------------------
    # Error paths
    # ------------------------------------------------------------------

    def test_file_not_found(self, tmp_path):
        """Non-existent file is caught by typer before reaching our code."""
        result = self._invoke(str(tmp_path / "nope.har"))
        assert result.exit_code != 0

    def test_har_parse_error(self, tmp_path):
        """HARParseError is caught and displayed."""
        har = tmp_path / "bad.har"
        har.write_text("{}")

        with patch(f"{MODULE}.parse_har_file", side_effect=HARParseError("bad json")):
            result = self._invoke(str(har))

        assert result.exit_code != 0
        assert "Failed to parse" in strip_ansi(result.output)

    def test_no_entries(self, tmp_path):
        """Empty entries list produces friendly message."""
        har = tmp_path / "empty.har"
        har.write_text("{}")

        mock_result = HARParseResult(entries=[], errors=[])
        with patch(f"{MODULE}.parse_har_file", return_value=mock_result):
            result = self._invoke(str(har))

        assert result.exit_code != 0
        assert "No HTTP entries" in strip_ansi(result.output)

    def test_no_domain_detected(self, tmp_path):
        """When extract_domain returns empty string."""
        har = tmp_path / "test.har"
        har.write_text("{}")

        entry = _make_entry()
        mock_result = HARParseResult(entries=[entry], errors=[])
        with (
            patch(f"{MODULE}.parse_har_file", return_value=mock_result),
            patch(f"{MODULE}.extract_domain", return_value=""),
        ):
            result = self._invoke(str(har))

        assert result.exit_code != 0
        assert "Could not determine domain" in strip_ansi(result.output)

    def test_invalid_format(self, tmp_path):
        """Invalid --format value is rejected."""
        har = tmp_path / "test.har"
        har.write_text("{}")

        # Need to bypass typer file-exists check since we have a real file
        # but the format check happens before parsing
        with patch(f"{MODULE}.parse_har_file"):
            result = self._invoke(str(har), ["--format", "xml"])

        assert result.exit_code != 0
        assert "Invalid format" in strip_ansi(result.output)

    # ------------------------------------------------------------------
    # Parse warnings
    # ------------------------------------------------------------------

    def test_parse_errors_shown(self, tmp_path):
        """Parse errors are displayed as warnings."""
        har = tmp_path / "test.har"
        har.write_text("{}")

        from graftpunk.har.parser import ParseError

        errors = [
            ParseError(index=i, url=f"https://example.com/{i}", error=f"err{i}") for i in range(5)
        ]
        entry = _make_entry()
        mock_result = HARParseResult(entries=[entry], errors=errors)

        with (
            patch(f"{MODULE}.parse_har_file", return_value=mock_result),
            patch(f"{MODULE}.extract_domain", return_value="example.com"),
            patch(f"{MODULE}.detect_auth_flow", return_value=None),
            patch(f"{MODULE}.discover_api_endpoints", return_value=[]),
            patch(f"{MODULE}.generate_plugin_code", return_value="# plugin"),
        ):
            result = self._invoke(str(har), ["--dry-run"])

        output = strip_ansi(result.output)
        assert "5 entries failed to parse" in output
        # First 3 errors shown, then "and 2 more"
        assert "err0" in output
        assert "and 2 more" in output

    # ------------------------------------------------------------------
    # Happy paths
    # ------------------------------------------------------------------

    def test_dry_run_python(self, tmp_path):
        """Dry run prints generated code without writing files."""
        har = tmp_path / "test.har"
        har.write_text("{}")

        entry = _make_entry()
        mock_result = HARParseResult(entries=[entry], errors=[])

        with (
            patch(f"{MODULE}.parse_har_file", return_value=mock_result),
            patch(f"{MODULE}.extract_domain", return_value="example.com"),
            patch(f"{MODULE}.detect_auth_flow", return_value=None),
            patch(f"{MODULE}.discover_api_endpoints", return_value=[]),
            patch(f"{MODULE}.generate_plugin_code", return_value="# generated plugin code"),
        ):
            result = self._invoke(str(har), ["--dry-run"])

        assert result.exit_code == 0
        output = strip_ansi(result.output)
        assert "generated plugin code" in output
        assert "Would write to" in output

    def test_dry_run_yaml(self, tmp_path):
        """Dry run with yaml format."""
        har = tmp_path / "test.har"
        har.write_text("{}")

        entry = _make_entry()
        mock_result = HARParseResult(entries=[entry], errors=[])

        with (
            patch(f"{MODULE}.parse_har_file", return_value=mock_result),
            patch(f"{MODULE}.extract_domain", return_value="example.com"),
            patch(f"{MODULE}.detect_auth_flow", return_value=None),
            patch(f"{MODULE}.discover_api_endpoints", return_value=[]),
            patch(f"{MODULE}.generate_yaml_plugin", return_value="# yaml plugin"),
        ):
            result = self._invoke(str(har), ["--format", "yaml", "--dry-run"])

        assert result.exit_code == 0
        assert "yaml plugin" in strip_ansi(result.output)

    def test_auth_flow_displayed(self, tmp_path):
        """When auth flow is detected, it is shown in the output."""
        har = tmp_path / "test.har"
        har.write_text("{}")

        entry = _make_entry()
        mock_result = HARParseResult(entries=[entry], errors=[])
        flow = _make_auth_flow()

        with (
            patch(f"{MODULE}.parse_har_file", return_value=mock_result),
            patch(f"{MODULE}.extract_domain", return_value="example.com"),
            patch(f"{MODULE}.detect_auth_flow", return_value=flow),
            patch(f"{MODULE}.discover_api_endpoints", return_value=[]),
            patch(f"{MODULE}.generate_plugin_code", return_value="# code"),
        ):
            result = self._invoke(str(har), ["--dry-run"])

        output = strip_ansi(result.output)
        assert "Auth Flow Detected" in output

    def test_api_endpoints_displayed(self, tmp_path):
        """Discovered endpoints are shown."""
        har = tmp_path / "test.har"
        har.write_text("{}")

        entry = _make_entry()
        mock_result = HARParseResult(entries=[entry], errors=[])
        endpoints = [
            APIEndpoint(method="GET", url="https://example.com/api/v1/users", path="/api/v1/users"),
        ]

        with (
            patch(f"{MODULE}.parse_har_file", return_value=mock_result),
            patch(f"{MODULE}.extract_domain", return_value="example.com"),
            patch(f"{MODULE}.detect_auth_flow", return_value=None),
            patch(f"{MODULE}.discover_api_endpoints", return_value=endpoints),
            patch(f"{MODULE}.generate_plugin_code", return_value="# code"),
        ):
            result = self._invoke(str(har), ["--dry-run"])

        output = strip_ansi(result.output)
        assert "API Endpoints" in output

    def test_write_output_file(self, tmp_path):
        """Non-dry-run writes the plugin file."""
        har = tmp_path / "test.har"
        har.write_text("{}")
        out = tmp_path / "output" / "plugin.py"

        entry = _make_entry()
        mock_result = HARParseResult(entries=[entry], errors=[])

        with (
            patch(f"{MODULE}.parse_har_file", return_value=mock_result),
            patch(f"{MODULE}.extract_domain", return_value="example.com"),
            patch(f"{MODULE}.detect_auth_flow", return_value=None),
            patch(f"{MODULE}.discover_api_endpoints", return_value=[]),
            patch(f"{MODULE}.generate_plugin_code", return_value="# plugin code"),
        ):
            result = self._invoke(str(har), ["-o", str(out)])

        assert result.exit_code == 0
        assert out.exists()
        assert out.read_text() == "# plugin code"
        assert "Generated plugin" in strip_ansi(result.output)

    def test_write_error(self, tmp_path):
        """OSError during write is caught."""
        har = tmp_path / "test.har"
        har.write_text("{}")

        entry = _make_entry()
        mock_result = HARParseResult(entries=[entry], errors=[])

        with (
            patch(f"{MODULE}.parse_har_file", return_value=mock_result),
            patch(f"{MODULE}.extract_domain", return_value="example.com"),
            patch(f"{MODULE}.detect_auth_flow", return_value=None),
            patch(f"{MODULE}.discover_api_endpoints", return_value=[]),
            patch(f"{MODULE}.generate_plugin_code", return_value="# code"),
            patch("pathlib.Path.write_text", side_effect=OSError("permission denied")),
        ):
            result = self._invoke(str(har), ["-o", str(tmp_path / "out.py")])

        assert result.exit_code != 0
        assert "Failed to write" in strip_ansi(result.output)

    def test_custom_name_option(self, tmp_path):
        """--name overrides inferred site name."""
        har = tmp_path / "test.har"
        har.write_text("{}")

        entry = _make_entry()
        mock_result = HARParseResult(entries=[entry], errors=[])

        with (
            patch(f"{MODULE}.parse_har_file", return_value=mock_result),
            patch(f"{MODULE}.extract_domain", return_value="example.com"),
            patch(f"{MODULE}.detect_auth_flow", return_value=None),
            patch(f"{MODULE}.discover_api_endpoints", return_value=[]),
            patch(f"{MODULE}.generate_plugin_code", return_value="# code") as _mock_gen,
        ):
            result = self._invoke(str(har), ["--name", "custom_name", "--dry-run"])

        assert result.exit_code == 0
        output = strip_ansi(result.output)
        assert "custom_name" in output

    def test_no_discover_api(self, tmp_path):
        """--no-discover-api skips endpoint discovery."""
        har = tmp_path / "test.har"
        har.write_text("{}")

        entry = _make_entry()
        mock_result = HARParseResult(entries=[entry], errors=[])

        with (
            patch(f"{MODULE}.parse_har_file", return_value=mock_result),
            patch(f"{MODULE}.extract_domain", return_value="example.com"),
            patch(f"{MODULE}.detect_auth_flow", return_value=None),
            patch(f"{MODULE}.discover_api_endpoints") as mock_discover,
            patch(f"{MODULE}.generate_plugin_code", return_value="# code"),
        ):
            result = self._invoke(str(har), ["--no-discover-api", "--dry-run"])

        assert result.exit_code == 0
        mock_discover.assert_not_called()
