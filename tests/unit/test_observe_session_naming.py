"""Observe run dirs: every writer names them the way the readers look them up.

A session name like ``myshop@alice`` is not a legal run-dir component, and a
legal-but-unslugified one like ``my_shop`` becomes ``my-shop`` on the read
side. Both writers (the ``gp http`` HAR save and the ``observe go`` capture)
must therefore route the name through ``session_dirname`` — otherwise the run
is written where no reader will ever look (#151).
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import requests
from typer.testing import CliRunner

from graftpunk.cli.http_commands import _save_observe_data
from graftpunk.cli.main import app
from graftpunk.observe.storage import session_dirname

runner = CliRunner()

LABELLED = "myshop@alice"
UNDERSCORED = "my_shop"


def strip_ansi(text: str) -> str:
    """Remove ANSI escape codes from text."""
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def _fake_response() -> MagicMock:
    response = MagicMock(spec=requests.Response)
    response.status_code = 200
    response.reason = "OK"
    response.headers = {"Content-Type": "application/json"}
    response.content = b'{"ok": true}'
    response.text = '{"ok": true}'
    response.elapsed = MagicMock()
    response.elapsed.total_seconds.return_value = 0.1
    response.request = MagicMock()
    response.request.headers = {"User-Agent": "test"}
    return response


async def _run_observe_go_with_fake_browser(base_dir: Path, namespace: str) -> None:
    """Drive ``_run_observe_go`` against a fake browser, real ObserveStorage."""
    from graftpunk.cli.main import _run_observe_go

    tab = MagicMock()
    tab.sleep = AsyncMock()
    browser = MagicMock()
    browser.main_tab = tab

    async def browser_get(url: str) -> MagicMock:
        return tab

    browser.get = browser_get

    backend = MagicMock()
    backend.start_capture_async = AsyncMock()
    backend.take_screenshot = AsyncMock(return_value=b"\x89PNGfake")
    backend.get_page_source = AsyncMock(return_value="<html></html>")
    backend.stop_capture_async = AsyncMock()
    backend.get_har_entries = MagicMock(return_value=[])
    backend.get_console_logs = MagicMock(return_value=[])

    nodriver = MagicMock()
    nodriver.start = AsyncMock(return_value=browser)

    with (
        patch.dict("sys.modules", {"nodriver": nodriver}),
        patch("graftpunk.cli.main.OBSERVE_BASE_DIR", base_dir),
        patch("graftpunk.observe.capture.NodriverCaptureBackend", return_value=backend),
    ):
        await _run_observe_go(
            namespace, "https://example.com", 0.0, 5 * 1024 * 1024, session_name=None
        )


class TestHttpWriterReaderRoundTrip:
    """``gp http --observe`` writes where ``gp observe`` reads."""

    @pytest.mark.parametrize("name", [LABELLED, UNDERSCORED])
    def test_saved_run_is_found_by_show(self, tmp_path: Path, name: str) -> None:
        with patch("graftpunk.cli.http_commands.OBSERVE_BASE_DIR", tmp_path):
            storage = _save_observe_data(name, "GET", "https://example.com", _fake_response())

        assert storage is not None, "labelled session names must not break the HAR save"
        assert storage.run_dir.parent.name == session_dirname(name)

        with patch("graftpunk.cli.main.OBSERVE_BASE_DIR", tmp_path):
            result = runner.invoke(app, ["observe", "show", name])

        assert result.exit_code == 0, result.output
        assert session_dirname(name) in strip_ansi(result.output)

    def test_labelled_name_does_not_trip_the_best_effort_warning(self, tmp_path: Path) -> None:
        """The broad except used to swallow ObserveStorage's ValueError (#151)."""
        with (
            patch("graftpunk.cli.http_commands.OBSERVE_BASE_DIR", tmp_path),
            patch("graftpunk.cli.http_commands.LOG") as log,
        ):
            _save_observe_data(LABELLED, "GET", "https://example.com", _fake_response())

        assert log.warning.call_args_list == []

    @pytest.mark.parametrize("name", [LABELLED, UNDERSCORED])
    def test_saved_run_is_found_by_list(self, tmp_path: Path, name: str) -> None:
        with patch("graftpunk.cli.http_commands.OBSERVE_BASE_DIR", tmp_path):
            _save_observe_data(name, "GET", "https://example.com", _fake_response())

        with (
            patch("graftpunk.cli.main.OBSERVE_BASE_DIR", tmp_path),
            patch("graftpunk.cli.main.resolve_session_name_or_exit", return_value=name),
        ):
            result = runner.invoke(app, ["observe", "--session", name, "list"])

        assert result.exit_code == 0, result.output
        output = strip_ansi(result.output)
        assert "No observe runs found" not in output
        assert "1 run(s)" in output


class TestHttpObserveUsesTheResolvedName:
    """The run dir is keyed by the session the request was MADE with."""

    def test_base_name_files_the_run_under_the_resolved_account(self, tmp_path: Path) -> None:
        from graftpunk.cli.main import app as root_app

        session = MagicMock(spec=requests.Session)
        session.headers = {}
        session.request.return_value = _fake_response()

        with (
            patch("graftpunk.cli.http_commands.load_session_for_api", return_value=session),
            patch("graftpunk.cli.plugin_commands._registered_plugins_for_teardown", []),
            patch("graftpunk.cli.plugin_commands._plugin_session_map", {"myshop": "myshop"}),
            patch("graftpunk.cli.plugin_commands.list_sessions", return_value=["myshop@alice"]),
            patch("graftpunk.cli.http_commands.OBSERVE_BASE_DIR", tmp_path),
        ):
            result = runner.invoke(
                root_app, ["http", "get", "--session", "myshop", "https://example.com"]
            )

        assert result.exit_code == 0, result.output
        # Written under the RESOLVED name, not the argument the user typed.
        assert (tmp_path / "myshop-alice").is_dir()
        assert not (tmp_path / "myshop").exists()

        with patch("graftpunk.cli.main.OBSERVE_BASE_DIR", tmp_path):
            found = runner.invoke(root_app, ["observe", "show", "myshop@alice"])
            listed = runner.invoke(root_app, ["observe", "--session", "myshop@alice", "list"])

        assert found.exit_code == 0, found.output
        assert "myshop-alice" in strip_ansi(found.output)
        assert listed.exit_code == 0, listed.output
        assert "1 run(s)" in strip_ansi(listed.output)


class TestBuildObserveContextWriterReaderRoundTrip:
    """``build_observe_context`` (the plugin-command entry point) writes where readers look."""

    @pytest.mark.parametrize("name", [LABELLED, UNDERSCORED])
    def test_context_run_dir_is_found_by_show(self, tmp_path: Path, name: str) -> None:
        from graftpunk.observe.context import build_observe_context

        with patch("graftpunk.observe.context.OBSERVE_BASE_DIR", tmp_path):
            ctx = build_observe_context(name, "selenium", None, "full")

        assert ctx._storage is not None
        assert ctx._storage.run_dir.parent.name == session_dirname(name)
        assert (tmp_path / session_dirname(name)).is_dir()

        with patch("graftpunk.cli.main.OBSERVE_BASE_DIR", tmp_path):
            result = runner.invoke(app, ["observe", "show", name])

        assert result.exit_code == 0, result.output
        assert session_dirname(name) in strip_ansi(result.output)


class TestObserveGoWriterReaderRoundTrip:
    """``gp observe go`` writes where ``gp observe`` reads."""

    @pytest.mark.parametrize("name", [LABELLED, UNDERSCORED])
    @pytest.mark.asyncio
    async def test_captured_run_is_found_by_show(self, tmp_path: Path, name: str) -> None:
        await _run_observe_go_with_fake_browser(tmp_path, name)

        assert (tmp_path / session_dirname(name)).is_dir()

        with patch("graftpunk.cli.main.OBSERVE_BASE_DIR", tmp_path):
            result = runner.invoke(app, ["observe", "show", name])

        assert result.exit_code == 0, result.output
        assert session_dirname(name) in strip_ansi(result.output)
