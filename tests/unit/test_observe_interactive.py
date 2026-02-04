"""Tests for the observe interactive command and _run_observe_interactive function."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from typer.testing import CliRunner

from graftpunk.cli.main import app

runner = CliRunner()


def strip_ansi(text: str) -> str:
    """Remove ANSI escape codes from text."""
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


class TestObserveInteractiveCommandRegistered:
    """Verify the observe interactive subcommand is registered and accessible."""

    def test_observe_interactive_command_help(self) -> None:
        """Verify observe interactive --help works via the CLI runner."""
        result = runner.invoke(app, ["observe", "interactive", "--help"])
        assert result.exit_code == 0
        output = strip_ansi(result.output)
        assert "interactive" in output.lower()
        assert "URL" in output or "url" in output.lower()

    def test_observe_interactive_listed_in_observe_help(self) -> None:
        """Verify interactive appears in observe --help output."""
        result = runner.invoke(app, ["observe", "--help"])
        assert result.exit_code == 0
        output = strip_ansi(result.output)
        assert "interactive" in output

    def test_observe_interactive_requires_session(self) -> None:
        """observe interactive without session should error."""
        result = runner.invoke(app, ["observe", "interactive", "https://example.com"])
        assert result.exit_code != 0
        assert "session" in result.output.lower()


class TestRunObserveInteractiveSavesOnStop:
    """Test the async _run_observe_interactive function end-to-end."""

    @pytest.mark.asyncio
    async def test_saves_har_screenshot_and_logs_on_stop(self, tmp_path: Path) -> None:
        """Test that _run_observe_interactive saves HAR, screenshot, page source,
        and console logs when the stop event fires."""
        from graftpunk.cli.main import _run_observe_interactive

        # Mock session
        mock_session = MagicMock()
        mock_session.cookies = [{"name": "token", "value": "abc", "domain": ".example.com"}]

        # Mock browser and tab
        mock_tab = MagicMock()
        mock_browser = MagicMock()
        mock_browser.main_tab = mock_tab

        async def mock_browser_get(url: str) -> MagicMock:
            return mock_tab

        mock_browser.get = mock_browser_get

        # Mock capture backend
        mock_backend = MagicMock()
        mock_backend.start_capture_async = AsyncMock()
        mock_backend.take_screenshot = AsyncMock(return_value=b"\x89PNGfake_screenshot")
        mock_backend.get_page_source = AsyncMock(return_value="<html>Interactive</html>")
        mock_backend.stop_capture_async = AsyncMock()
        mock_backend.get_har_entries = MagicMock(
            return_value=[{"request": {"url": "https://example.com"}}]
        )
        mock_backend.get_console_logs = MagicMock(
            return_value=[{"level": "log", "message": "hello"}]
        )

        # Mock storage with a real tmp_path-based run_dir (must exist for page-source.html)
        run_dir = tmp_path / "observe" / "test-session" / "run-001"
        run_dir.mkdir(parents=True, exist_ok=True)
        mock_storage = MagicMock()
        mock_storage.run_dir = run_dir
        mock_storage.save_screenshot = MagicMock(return_value=tmp_path / "screenshot.png")

        # Create an Event that is immediately set so the function does not block
        already_set_event = asyncio.Event()
        already_set_event.set()

        mock_loop = MagicMock()
        mock_loop.add_signal_handler = MagicMock()
        mock_loop.remove_signal_handler = MagicMock()

        # Mock nodriver module for the lazy import inside _run_observe_interactive
        mock_nodriver = MagicMock()
        mock_nodriver.start = AsyncMock(return_value=mock_browser)

        with (
            patch.dict("sys.modules", {"nodriver": mock_nodriver}),
            patch(
                "graftpunk.load_session",
                return_value=mock_session,
            ) as mock_load,
            patch(
                "graftpunk.session.inject_cookies_to_nodriver",
                new_callable=AsyncMock,
                return_value=1,
            ) as mock_inject,
            patch(
                "graftpunk.observe.capture.NodriverCaptureBackend",
                return_value=mock_backend,
            ) as mock_backend_cls,
            patch(
                "graftpunk.observe.storage.ObserveStorage",
                return_value=mock_storage,
            ),
            patch("asyncio.get_running_loop", return_value=mock_loop),
            patch("asyncio.Event", return_value=already_set_event),
        ):
            await _run_observe_interactive("test-session", "https://example.com", 5 * 1024 * 1024)

        # Verify session was loaded
        mock_load.assert_called_once_with("test-session")

        # Verify cookies were injected
        mock_inject.assert_called_once_with(mock_tab, mock_session.cookies)

        # Verify capture backend was created and started
        mock_backend_cls.assert_called_once()
        mock_backend.start_capture_async.assert_awaited_once()

        # Verify screenshot was taken
        mock_backend.take_screenshot.assert_awaited_once()
        mock_storage.save_screenshot.assert_called_once()

        # Verify page source was captured
        mock_backend.get_page_source.assert_awaited_once()

        # Verify capture was stopped
        mock_backend.stop_capture_async.assert_awaited_once()

        # Verify HAR entries were written
        mock_backend.get_har_entries.assert_called_once()
        mock_storage.write_har.assert_called_once_with(
            [{"request": {"url": "https://example.com"}}]
        )

        # Verify console logs were written
        mock_backend.get_console_logs.assert_called_once()
        mock_storage.write_console_logs.assert_called_once_with(
            [{"level": "log", "message": "hello"}]
        )

        # Verify browser was stopped
        mock_browser.stop.assert_called_once()

    @pytest.mark.asyncio
    async def test_handles_load_session_failure(self) -> None:
        """Test that _run_observe_interactive handles session load failure gracefully."""
        from graftpunk.cli.main import _run_observe_interactive

        mock_nodriver = MagicMock()

        with (
            patch.dict("sys.modules", {"nodriver": mock_nodriver}),
            patch(
                "graftpunk.load_session",
                side_effect=FileNotFoundError("Session not found"),
            ),
        ):
            # Should not raise; just prints error and returns
            await _run_observe_interactive(
                "nonexistent-session", "https://example.com", 5 * 1024 * 1024
            )


class TestObserveGoInteractiveFlag:
    """Test that observe go --interactive delegates to _run_observe_interactive."""

    def test_interactive_flag_calls_run_observe_interactive(self) -> None:
        """Test that observe go --interactive calls _run_observe_interactive."""
        with (
            patch("graftpunk.cli.main.resolve_session_name", return_value="mysite"),
            patch("graftpunk.cli.main._run_observe_interactive") as _mock_interactive,
            patch("graftpunk.logging.suppress_asyncio_noise"),
            patch("graftpunk.cli.main.asyncio") as mock_asyncio,
        ):
            result = runner.invoke(
                app,
                [
                    "observe",
                    "--session",
                    "mysite",
                    "go",
                    "--interactive",
                    "https://example.com",
                ],
            )

        assert result.exit_code == 0
        # asyncio.run should have been called with the interactive coroutine
        mock_asyncio.run.assert_called_once()

    def test_without_interactive_flag_calls_run_observe_go(self) -> None:
        """Test that observe go without --interactive calls _run_observe_go."""
        with (
            patch("graftpunk.cli.main.resolve_session_name", return_value="mysite"),
            patch("graftpunk.cli.main._run_observe_go") as _mock_go,
            patch("graftpunk.cli.main._run_observe_interactive") as _mock_interactive,
            patch("graftpunk.cli.main.asyncio") as mock_asyncio,
        ):
            result = runner.invoke(
                app,
                [
                    "observe",
                    "--session",
                    "mysite",
                    "go",
                    "https://example.com",
                ],
            )

        assert result.exit_code == 0
        # asyncio.run should have been called with the go coroutine (not interactive)
        mock_asyncio.run.assert_called_once()

    def test_interactive_short_flag(self) -> None:
        """Test that observe go -i also delegates to _run_observe_interactive."""
        with (
            patch("graftpunk.cli.main.resolve_session_name", return_value="mysite"),
            patch("graftpunk.cli.main._run_observe_interactive") as _mock_interactive,
            patch("graftpunk.logging.suppress_asyncio_noise"),
            patch("graftpunk.cli.main.asyncio") as mock_asyncio,
        ):
            result = runner.invoke(
                app,
                [
                    "observe",
                    "--session",
                    "mysite",
                    "go",
                    "-i",
                    "https://example.com",
                ],
            )

        assert result.exit_code == 0
        mock_asyncio.run.assert_called_once()

    def test_observe_go_interactive_flag_in_help(self) -> None:
        """Test that --interactive/-i flag appears in observe go --help."""
        result = runner.invoke(app, ["observe", "go", "--help"])
        assert result.exit_code == 0
        output = strip_ansi(result.output)
        assert "--interactive" in output
