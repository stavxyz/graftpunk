"""render_markdown and render_json over synthetic runs (plugin tooling spec, 2026-09-11)."""

from __future__ import annotations

import json
from pathlib import Path

from graftpunk.har.digest import DigestSource, digest
from graftpunk.har.report import render_json, render_markdown

_EXPECTED_MAX_MARKDOWN_LINES = 400
_LARGE_RUN_ENTRY_COUNT = 700


def _entry(
    method: str, url: str, *, content_type: str = "application/json", body: str = "{}"
) -> dict:
    return {
        "startedDateTime": "2026-09-10T10:00:00.000Z",
        "time": 5,
        "request": {"method": method, "url": url, "headers": [], "cookies": [], "queryString": []},
        "response": {
            "status": 200,
            "statusText": "OK",
            "headers": [{"name": "Content-Type", "value": content_type}],
            "cookies": [],
            "content": {"mimeType": content_type, "text": body, "size": len(body)},
        },
    }


def _write_har(tmp_path: Path, entries: list[dict]) -> Path:
    har_path = tmp_path / "network.har"
    har_path.write_text(json.dumps({"log": {"version": "1.2", "entries": entries}}))
    return har_path


class TestRenderMarkdown:
    def test_sections_present_in_order(self, tmp_path: Path) -> None:
        entries = [_entry("GET", "https://api.myshop.example.com/orders")]
        text = render_markdown(digest(DigestSource.from_har(_write_har(tmp_path, entries))))
        for heading in ("## Summary", "## Login", "## Tokens", "## Cookies", "## Endpoints"):
            assert heading in text
        assert text.index("## Summary") < text.index("## Login") < text.index("## Tokens")
        assert text.index("## Tokens") < text.index("## Cookies") < text.index("## Endpoints")

    def test_json_endpoints_ordered_before_non_json(self, tmp_path: Path) -> None:
        entries = [
            _entry(
                "GET",
                "https://api.myshop.example.com/page",
                content_type="text/html",
                body="<html></html>",
            ),
            _entry("GET", "https://api.myshop.example.com/orders", body="{}"),
        ]
        text = render_markdown(digest(DigestSource.from_har(_write_har(tmp_path, entries))))
        assert text.index("/orders") < text.index("/page")

    def test_limit_caps_endpoints_shown(self, tmp_path: Path) -> None:
        entries = [_entry("GET", f"https://api.myshop.example.com/item-{i}") for i in range(10)]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        text = render_markdown(result, limit=3)
        assert text.count("### GET /item-") == 3
        assert "more endpoint(s)" in text

    def test_large_run_stays_under_400_lines_with_default_limit(self, tmp_path: Path) -> None:
        entries = [
            _entry("GET", f"https://api.myshop.example.com/orders/{i}", body=json.dumps({"id": i}))
            for i in range(_LARGE_RUN_ENTRY_COUNT)
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        text = render_markdown(result)
        assert len(text.splitlines()) < _EXPECTED_MAX_MARKDOWN_LINES

    def test_no_planted_secret_appears_in_rendered_output(self, tmp_path: Path) -> None:
        planted_cookie = "session_id=s3cr3t-cookie-value; Path=/"
        planted_password = "hunter2superSecret"  # noqa: S105 (test fixture, not a credential)
        entries = [
            {
                "startedDateTime": "2026-09-10T10:00:00.000Z",
                "time": 5,
                "request": {
                    "method": "POST",
                    "url": "https://api.myshop.example.com/login",
                    "headers": [{"name": "X-Api-Key", "value": "unshown-header-value-999"}],
                    "cookies": [],
                    "queryString": [],
                    "postData": {
                        "mimeType": "application/json",
                        "text": json.dumps(
                            {"email": "alice@example.com", "password": planted_password}
                        ),
                    },
                },
                "response": {
                    "status": 200,
                    "statusText": "OK",
                    "headers": [
                        {"name": "Content-Type", "value": "application/json"},
                        {"name": "Set-Cookie", "value": planted_cookie},
                    ],
                    "cookies": [],
                    "content": {"mimeType": "application/json", "text": "{}", "size": 2},
                },
            }
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        markdown = render_markdown(result)
        rendered_json = render_json(result)
        for planted in (planted_password, "s3cr3t-cookie-value", "unshown-header-value-999"):
            assert planted not in markdown
            assert planted not in rendered_json


class TestRenderJson:
    def test_output_is_valid_json_with_expected_top_level_keys(self, tmp_path: Path) -> None:
        entries = [_entry("GET", "https://api.myshop.example.com/orders")]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        parsed = json.loads(render_json(result))
        assert set(parsed) == {
            "source",
            "primary_host",
            "hosts",
            "endpoints",
            "login",
            "login_forms",
            "tokens",
            "cookies",
            "dropped",
        }

    def test_is_complete_regardless_of_endpoint_count(self, tmp_path: Path) -> None:
        entries = [_entry("GET", f"https://api.myshop.example.com/item-{i}") for i in range(80)]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        parsed = json.loads(render_json(result))
        assert len(parsed["endpoints"]) == len(result.endpoints) == 80
