"""Tests for graftpunk.observe.run: run ids, HAR redaction, guarded run saves."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from graftpunk.observe.run import REDACTED, make_run_id, redact_har_entries, save_observe_run
from graftpunk.observe.storage import _SAFE_NAME_RE, ObserveStorage


def _entry(**overrides: object) -> dict:
    e: dict = {
        "request": {
            "url": "https://example.com/login?next=%2F",
            "headers": [{"name": "Authorization", "value": "Basic dXNlcjpodW50ZXIy"}],
            "postData": {
                "mimeType": "application/x-www-form-urlencoded",
                "text": "u=alice&p=hunter2",
            },
        },
        "response": {"headers": [], "content": {"text": '{"echo":"hunter2"}'}},
    }
    e.update(overrides)
    return e


class TestMakeRunId:
    def test_shape(self) -> None:
        rid = make_run_id()
        assert _SAFE_NAME_RE.match(rid)
        assert rid.count("-") == 2


class TestRedactHarEntries:
    def test_raw_value_in_post_body_and_response(self) -> None:
        out = redact_har_entries([_entry()], ["hunter2"])
        assert out[0]["request"]["postData"]["text"] == f"u=alice&p={REDACTED}"
        assert out[0]["response"]["content"]["text"] == f'{{"echo":"{REDACTED}"}}'

    def test_url_encoded_and_json_escaped_forms(self) -> None:
        secret = "p@ss w/ëird"  # noqa: S105 — test fixture, not a credential
        e = _entry()
        e["request"]["postData"]["text"] = "p=p%40ss+w%2F%C3%ABird&q=p%40ss%20w%2F%C3%ABird"
        e["response"]["content"]["text"] = json.dumps({"pw": secret})
        out = redact_har_entries([e], [secret])
        assert secret not in json.dumps(out)
        assert "p%40ss" not in json.dumps(out)
        assert out[0]["request"]["postData"]["text"] == f"p={REDACTED}&q={REDACTED}"

    def test_base64_form(self) -> None:
        b64 = base64.b64encode(b"hunter2").decode()
        e = _entry()
        e["request"]["headers"] = [{"name": "X-Token", "value": b64}]
        out = redact_har_entries([e], ["hunter2"])
        assert out[0]["request"]["headers"][0]["value"] == REDACTED

    def test_short_or_empty_secrets_are_skipped(self) -> None:
        e = _entry()
        out = redact_har_entries([e], ["", "ab"])
        assert out == [e]

    def test_does_not_mutate_input(self) -> None:
        e = _entry()
        before = json.dumps(e)
        redact_har_entries([e], ["hunter2"])
        assert json.dumps(e) == before

    def test_no_secrets_returns_entries_unchanged(self) -> None:
        e = [_entry()]
        assert redact_har_entries(e, []) is e


def _backend(har: list | None = None) -> MagicMock:
    b = MagicMock()
    b.take_screenshot = AsyncMock(return_value=b"\x89PNG")
    b.get_page_source = AsyncMock(return_value="<html/>")
    b.stop_capture_async = AsyncMock()
    b.get_har_entries = MagicMock(return_value=har if har is not None else [_entry()])
    b.get_console_logs = MagicMock(return_value=[{"level": "log"}])
    return b


class TestSaveObserveRun:
    @pytest.mark.asyncio
    async def test_writes_everything_and_redacts_har(self, tmp_path: Path) -> None:
        storage = ObserveStorage(tmp_path, "sess", "run1")
        backend = _backend()

        await save_observe_run(storage, backend, "login", redact=["hunter2"])

        backend.stop_capture_async.assert_awaited_once()
        assert (storage.run_dir / "page-source.html").read_text() == "<html/>"
        assert list((storage.run_dir / "screenshots").glob("*login*.png"))
        har = json.loads((storage.run_dir / "network.har").read_text())
        assert "hunter2" not in json.dumps(har)
        assert REDACTED in json.dumps(har)

    @pytest.mark.asyncio
    async def test_one_failed_write_does_not_stop_the_others_or_raise(self, tmp_path: Path) -> None:
        storage = ObserveStorage(tmp_path, "sess", "run2")
        backend = _backend()
        console = MagicMock()

        with (
            patch.object(storage, "write_har", side_effect=OSError("disk full")),
            patch("graftpunk.observe.run.LOG") as mock_log,
        ):
            await save_observe_run(storage, backend, "go", console=console)

        # HAR failed, logged; console logs and page source still landed.
        assert any(
            c.args[0] == "observe_run_write_failed" and c.kwargs["step"] == "HAR"
            for c in mock_log.error.call_args_list
        )
        assert (storage.run_dir / "console.jsonl").exists()
        assert (storage.run_dir / "page-source.html").exists()
        printed = " ".join(str(c.args[0]) for c in console.print.call_args_list)
        assert "HAR failed" in printed
        assert "Observe run:" in printed

    @pytest.mark.asyncio
    async def test_stop_capture_failure_still_writes_har(self, tmp_path: Path) -> None:
        storage = ObserveStorage(tmp_path, "sess", "run3")
        backend = _backend()
        backend.stop_capture_async = AsyncMock(side_effect=ConnectionError("browser gone"))

        await save_observe_run(storage, backend, "go")

        assert (storage.run_dir / "network.har").exists()

    @pytest.mark.asyncio
    async def test_console_none_is_silent(self, tmp_path: Path) -> None:
        storage = ObserveStorage(tmp_path, "sess", "run4")
        await save_observe_run(storage, _backend(), "go")  # no console -> no error
