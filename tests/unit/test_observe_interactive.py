"""Tests for the observe interactive command and _run_observe_interactive function."""

from __future__ import annotations

import asyncio
import re
import signal
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

    def test_observe_interactive_without_session_requires_session(self) -> None:
        """observe interactive without session or --no-session should fail."""
        result = runner.invoke(app, ["observe", "interactive", "https://example.com"])
        assert result.exit_code == 1
        output = strip_ansi(result.output)
        assert "no session" in output.lower() or "--no-session" in output

    def test_observe_interactive_with_no_session_flag_proceeds(self) -> None:
        """observe interactive --no-session should infer namespace and proceed."""
        with patch("graftpunk.cli.main.asyncio") as mock_asyncio:
            result = runner.invoke(
                app, ["observe", "--no-session", "interactive", "https://example.com"]
            )
        assert result.exit_code == 0
        mock_asyncio.run.assert_called_once()


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
                return_value=(1, 0),
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
            await _run_observe_interactive(
                "test-session",
                "https://example.com",
                5 * 1024 * 1024,
                session_name="test-session",
            )

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
    async def test_session_not_found_exits_with_error(self) -> None:
        """Session not found raises typer.Exit(1) — browser is never started."""
        import typer

        from graftpunk.cli.main import _run_observe_interactive
        from graftpunk.exceptions import SessionNotFoundError

        mock_nodriver = MagicMock()
        mock_nodriver.start = AsyncMock()

        with (
            patch.dict("sys.modules", {"nodriver": mock_nodriver}),
            patch(
                "graftpunk.load_session",
                side_effect=SessionNotFoundError("Session not found"),
            ) as mock_load,
            pytest.raises(typer.Exit) as exc_info,
        ):
            await _run_observe_interactive(
                "nonexistent-session",
                "https://example.com",
                5 * 1024 * 1024,
                session_name="nonexistent-session",
            )

        assert exc_info.value.exit_code == 1
        # Session was attempted
        mock_load.assert_called_once_with("nonexistent-session")
        # Browser was never started
        mock_nodriver.start.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_session_expired_exits_with_error(self) -> None:
        """Session expired raises typer.Exit(1) with --no-session hint."""
        import typer

        from graftpunk.cli.main import _run_observe_interactive
        from graftpunk.exceptions import SessionExpiredError

        mock_nodriver = MagicMock()
        mock_nodriver.start = AsyncMock()

        with (
            patch.dict("sys.modules", {"nodriver": mock_nodriver}),
            patch(
                "graftpunk.load_session",
                side_effect=SessionExpiredError("Session expired"),
            ),
            pytest.raises(typer.Exit) as exc_info,
        ):
            await _run_observe_interactive(
                "expired-session",
                "https://example.com",
                5 * 1024 * 1024,
                session_name="expired-session",
            )

        assert exc_info.value.exit_code == 1
        mock_nodriver.start.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_session_name_opens_browser_without_cookies(self) -> None:
        """When session_name=None, browser opens without cookies."""
        from graftpunk.cli.main import _run_observe_interactive

        mock_tab = MagicMock()
        mock_browser = MagicMock()
        mock_browser.main_tab = mock_tab
        mock_browser.get = AsyncMock(return_value=mock_tab)

        mock_backend = MagicMock()
        mock_backend.start_capture_async = AsyncMock()
        mock_backend.take_screenshot = AsyncMock(return_value=None)
        mock_backend.get_page_source = AsyncMock(return_value=None)
        mock_backend.stop_capture_async = AsyncMock()
        mock_backend.get_har_entries = MagicMock(return_value=[])
        mock_backend.get_console_logs = MagicMock(return_value=[])

        mock_nodriver = MagicMock()
        mock_nodriver.start = AsyncMock(return_value=mock_browser)

        already_set_event = asyncio.Event()
        already_set_event.set()
        mock_loop = MagicMock()
        mock_loop.add_signal_handler = MagicMock()
        mock_loop.remove_signal_handler = MagicMock()

        with (
            patch.dict("sys.modules", {"nodriver": mock_nodriver}),
            patch(
                "graftpunk.session.inject_cookies_to_nodriver",
                new_callable=AsyncMock,
            ) as mock_inject,
            patch(
                "graftpunk.observe.capture.NodriverCaptureBackend",
                return_value=mock_backend,
            ),
            patch("graftpunk.observe.storage.ObserveStorage", return_value=MagicMock()),
            patch("asyncio.get_running_loop", return_value=mock_loop),
            patch("asyncio.Event", return_value=already_set_event),
        ):
            await _run_observe_interactive(
                "example",
                "https://example.com",
                5 * 1024 * 1024,
                session_name=None,
            )

        # Cookies were NOT injected (no session)
        mock_inject.assert_not_called()
        # Browser was started and stopped
        mock_nodriver.start.assert_awaited_once()
        mock_browser.stop.assert_called_once()


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


class TestSetupObserveSessionSigintIsolation:
    """Verify that Chrome is isolated from SIGINT during nodriver.start()."""

    @pytest.mark.asyncio
    async def test_sigint_ignored_during_nodriver_start(self, tmp_path: Path) -> None:
        """SIGINT handler is set to SIG_IGN while nodriver.start() executes,
        then restored to the original handler afterward."""
        from graftpunk.cli.main import _setup_observe_session

        captured_handler: list[signal.Handlers] = []

        mock_tab = MagicMock()
        mock_browser = MagicMock()
        mock_browser.main_tab = mock_tab
        mock_browser.get = AsyncMock(return_value=mock_tab)

        mock_backend = MagicMock()
        mock_backend.start_capture_async = AsyncMock()

        async def fake_nodriver_start(**kwargs: object) -> MagicMock:
            """Capture the SIGINT handler that is active when nodriver.start runs."""
            captured_handler.append(signal.getsignal(signal.SIGINT))
            return mock_browser

        mock_nodriver = MagicMock()
        mock_nodriver.start = fake_nodriver_start

        mock_storage = MagicMock()
        mock_storage.run_dir = tmp_path / "fake-run-dir"

        original_handler = signal.getsignal(signal.SIGINT)

        with (
            patch.dict("sys.modules", {"nodriver": mock_nodriver}),
            patch(
                "graftpunk.observe.capture.NodriverCaptureBackend",
                return_value=mock_backend,
            ),
            patch(
                "graftpunk.observe.storage.ObserveStorage",
                return_value=mock_storage,
            ),
        ):
            result = await _setup_observe_session(
                "test-ns",
                "https://example.com",
                5 * 1024 * 1024,
                headless=False,
                session_name=None,
            )

        # nodriver.start was called (captured_handler is non-empty)
        assert len(captured_handler) == 1, "nodriver.start should have been called exactly once"
        # During nodriver.start(), SIGINT should have been SIG_IGN
        assert captured_handler[0] is signal.SIG_IGN, (
            f"Expected SIG_IGN during nodriver.start(), got {captured_handler[0]}"
        )
        # After _setup_observe_session returns, the original handler is restored
        assert signal.getsignal(signal.SIGINT) == original_handler

        # Verify the session actually returned successfully
        assert result is not None

    @pytest.mark.asyncio
    async def test_sigint_restored_even_if_nodriver_start_raises(self) -> None:
        """If nodriver.start() raises, the original SIGINT handler is still restored."""
        from graftpunk.cli.main import _setup_observe_session

        async def failing_nodriver_start(**kwargs: object) -> None:
            raise RuntimeError("browser launch failed")

        mock_nodriver = MagicMock()
        mock_nodriver.start = failing_nodriver_start

        original_handler = signal.getsignal(signal.SIGINT)

        with (
            patch.dict("sys.modules", {"nodriver": mock_nodriver}),
            pytest.raises(RuntimeError, match="browser launch failed"),
        ):
            await _setup_observe_session(
                "test-ns",
                "https://example.com",
                5 * 1024 * 1024,
                headless=False,
                session_name=None,
            )

        # Original handler must be restored even after an exception
        assert signal.getsignal(signal.SIGINT) == original_handler
