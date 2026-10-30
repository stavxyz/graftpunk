"""Tests for the observe interactive command and run_observe_interactive function."""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from typer.testing import CliRunner

from graftpunk.cli.main import app
from graftpunk.plugins import infer_site_name
from tests.unit.cli_harness import strip_ansi

runner = CliRunner()


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
        with (
            patch(
                "graftpunk.cli.observe_commands.run_observe_interactive", new_callable=MagicMock
            ) as mock_interactive,
            patch("graftpunk.cli.observe_commands.asyncio") as mock_asyncio,
        ):
            result = runner.invoke(
                app, ["observe", "--no-session", "interactive", "https://example.com"]
            )
        assert result.exit_code == 0
        mock_asyncio.run.assert_called_once()
        assert mock_interactive.call_args.args[0] == infer_site_name("https://example.com")
        assert mock_interactive.call_args.kwargs["session_name"] is None


class TestRunObserveInteractiveSavesOnStop:
    """Test the async run_observe_interactive function end-to-end."""

    @pytest.mark.asyncio
    async def test_saves_har_screenshot_and_logs_on_stop(self, tmp_path: Path) -> None:
        """Test that run_observe_interactive saves HAR, screenshot, page source,
        and console logs when the stop event fires."""
        from graftpunk.cli.observe_browser import run_observe_interactive

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

        # Mock nodriver module for the lazy import inside run_observe_interactive
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
            await run_observe_interactive(
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

        from graftpunk.cli.observe_browser import run_observe_interactive
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
            await run_observe_interactive(
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

        from graftpunk.cli.observe_browser import run_observe_interactive
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
            await run_observe_interactive(
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
        from graftpunk.cli.observe_browser import run_observe_interactive

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
            await run_observe_interactive(
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
    """Test that observe go --interactive delegates to run_observe_interactive."""

    def test_interactive_flag_calls_run_observe_interactive(self) -> None:
        """Test that observe go --interactive calls run_observe_interactive."""
        with (
            patch(
                "graftpunk.cli.observe_commands.resolve_session_name_or_exit", return_value="mysite"
            ),
            patch(
                "graftpunk.cli.observe_commands.run_observe_go", new_callable=MagicMock
            ) as _mock_go,
            patch(
                "graftpunk.cli.observe_commands.run_observe_interactive", new_callable=MagicMock
            ) as _mock_interactive,
            patch("graftpunk.logging.suppress_asyncio_noise"),
            patch("graftpunk.cli.observe_commands.asyncio") as mock_asyncio,
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
        # asyncio.run should have been called with the interactive coroutine,
        # not the go one
        mock_asyncio.run.assert_called_once()
        _mock_interactive.assert_called_once()
        _mock_go.assert_not_called()

    def test_without_interactive_flag_calls_run_observe_go(self) -> None:
        """Test that observe go without --interactive calls run_observe_go."""
        with (
            patch(
                "graftpunk.cli.observe_commands.resolve_session_name_or_exit", return_value="mysite"
            ),
            patch(
                "graftpunk.cli.observe_commands.run_observe_go", new_callable=MagicMock
            ) as _mock_go,
            patch(
                "graftpunk.cli.observe_commands.run_observe_interactive", new_callable=MagicMock
            ) as _mock_interactive,
            patch("graftpunk.cli.observe_commands.asyncio") as mock_asyncio,
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
        # asyncio.run should have been called with the go coroutine, not interactive
        mock_asyncio.run.assert_called_once()
        _mock_go.assert_called_once()
        _mock_interactive.assert_not_called()

    def test_interactive_short_flag(self) -> None:
        """Test that observe go -i also delegates to run_observe_interactive."""
        with (
            patch(
                "graftpunk.cli.observe_commands.resolve_session_name_or_exit", return_value="mysite"
            ),
            patch(
                "graftpunk.cli.observe_commands.run_observe_interactive", new_callable=MagicMock
            ) as _mock_interactive,
            patch(
                "graftpunk.cli.observe_commands.run_observe_go", new_callable=MagicMock
            ) as _mock_go,
            patch("graftpunk.logging.suppress_asyncio_noise"),
            patch("graftpunk.cli.observe_commands.asyncio") as mock_asyncio,
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
        _mock_interactive.assert_called_once()
        _mock_go.assert_not_called()

    def test_observe_go_interactive_flag_in_help(self) -> None:
        """Test that --interactive/-i flag appears in observe go --help."""
        result = runner.invoke(app, ["observe", "go", "--help"])
        assert result.exit_code == 0
        output = strip_ansi(result.output)
        assert "--interactive" in output


class TestSetupObserveSessionTakesDomSnapshots:
    @pytest.mark.asyncio
    async def test_gp_observe_turns_the_snapshot_on_in_the_run_directory(
        self, tmp_path: Path
    ) -> None:
        """#267 part 2: only gp observe's own setup passes a snapshots directory; the
        capture factory every other recorder uses never does."""
        import inspect

        from graftpunk.cli.observe_browser import setup_observe_session
        from graftpunk.observe.capture import create_capture_backend

        mock_tab = MagicMock()
        mock_browser = MagicMock()
        mock_browser.main_tab = mock_tab
        mock_browser.get = AsyncMock(return_value=mock_tab)
        mock_backend = MagicMock()
        mock_backend.start_capture_async = AsyncMock()

        async def fake_nodriver_start(**kwargs: object) -> MagicMock:
            return mock_browser

        mock_nodriver = MagicMock()
        mock_nodriver.start = fake_nodriver_start
        mock_storage = MagicMock()
        mock_storage.run_dir = tmp_path / "run"

        with (
            patch.dict("sys.modules", {"nodriver": mock_nodriver}),
            patch(
                "graftpunk.observe.capture.NodriverCaptureBackend", return_value=mock_backend
            ) as backend_cls,
            patch("graftpunk.observe.storage.ObserveStorage", return_value=mock_storage),
        ):
            await setup_observe_session(
                "test-ns", "https://example.com", 1024, headless=True, session_name=None
            )

        assert backend_cls.call_args.kwargs["dom_snapshots_dir"] == tmp_path / "run" / "snapshots"
        assert "dom_snapshots_dir" not in inspect.signature(create_capture_backend).parameters


class TestSetupObserveSessionSigintIsolation:
    """SIGINT is ignored while nodriver.start() runs, and Chrome starts in its own session."""

    @pytest.mark.asyncio
    async def test_sigint_ignored_during_nodriver_start(self, tmp_path: Path) -> None:
        """SIGINT handler is set to SIG_IGN while nodriver.start() executes,
        then restored to the original handler afterward."""
        from graftpunk.cli.observe_browser import setup_observe_session

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
            result = await setup_observe_session(
                "test-ns",
                "https://example.com",
                5 * 1024 * 1024,
                headless=False,
                session_name=None,
            )

        # nodriver.start was called (captured_handler is non-empty)
        assert len(captured_handler) == 1, "nodriver.start should have been called exactly once"
        # During nodriver.start(), SIGINT should have been SIG_IGN
        assert captured_handler[0] is signal.SIG_IGN
        # After setup_observe_session returns, the original handler is restored
        assert signal.getsignal(signal.SIGINT) == original_handler

        # Verify the session actually returned successfully
        assert result is not None

    @pytest.mark.skipif(os.name != "posix", reason="sessions are a POSIX notion")
    @pytest.mark.asyncio
    async def test_chrome_starts_in_a_session_of_its_own(self, tmp_path: Path) -> None:
        """#333: an inherited SIG_IGN does not keep Chrome alive, and a terminal's
        Ctrl+C reaches its whole foreground process group, so Chrome has to start
        outside it. The fake start launches a real child through the same call
        nodriver's ``Browser.start`` makes."""
        import nodriver.core.browser as nodriver_launcher

        from graftpunk.cli.observe_browser import setup_observe_session

        launched: list[tuple[int, int]] = []
        mock_tab = MagicMock()
        mock_browser = MagicMock()
        mock_browser.main_tab = mock_tab
        mock_browser.get = AsyncMock(return_value=mock_tab)
        mock_backend = MagicMock()
        mock_backend.start_capture_async = AsyncMock()

        async def fake_nodriver_start(**kwargs: object) -> MagicMock:
            proc = await nodriver_launcher.asyncio.create_subprocess_exec(
                sys.executable,
                "-c",
                "import os; print(os.getsid(0))",
                stdout=asyncio.subprocess.PIPE,
            )
            out, _ = await proc.communicate()
            launched.append((int(out), proc.pid))
            return mock_browser

        mock_nodriver = MagicMock()
        mock_nodriver.start = fake_nodriver_start
        mock_storage = MagicMock()
        mock_storage.run_dir = tmp_path / "run"

        with (
            patch.dict("sys.modules", {"nodriver": mock_nodriver}),
            patch("graftpunk.observe.capture.NodriverCaptureBackend", return_value=mock_backend),
            patch("graftpunk.observe.storage.ObserveStorage", return_value=mock_storage),
        ):
            await setup_observe_session(
                "test-ns", "https://example.com", 1024, headless=True, session_name=None
            )

        [(sid, pid)] = launched
        assert sid == pid
        assert sid != os.getsid(0)
        assert nodriver_launcher.asyncio is asyncio

    @pytest.mark.skipif(os.name != "posix", reason="sessions are a POSIX notion")
    @pytest.mark.asyncio
    async def test_a_chrome_that_shares_this_session_is_reported_on_the_session(
        self, tmp_path: Path
    ) -> None:
        """The outcome is checked after the start: a launch that bypassed the
        seam (nodriver moved its call) marks the run, so interactive can warn."""
        from graftpunk.cli.observe_browser import setup_observe_session

        spawned: list[asyncio.subprocess.Process] = []
        mock_backend = MagicMock()
        mock_backend.start_capture_async = AsyncMock()

        async def fake_nodriver_start(**kwargs: object) -> MagicMock:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-c", "import time; time.sleep(30)"
            )
            spawned.append(proc)
            browser = MagicMock()
            browser._process = proc
            browser.get = AsyncMock(return_value=MagicMock())
            return browser

        mock_nodriver = MagicMock()
        mock_nodriver.start = fake_nodriver_start
        mock_storage = MagicMock()
        mock_storage.run_dir = tmp_path / "run"

        try:
            with (
                patch.dict("sys.modules", {"nodriver": mock_nodriver}),
                patch(
                    "graftpunk.observe.capture.NodriverCaptureBackend", return_value=mock_backend
                ),
                patch("graftpunk.observe.storage.ObserveStorage", return_value=mock_storage),
            ):
                observed = await setup_observe_session(
                    "test-ns", "https://example.com", 1024, headless=True, session_name=None
                )
        finally:
            for proc in spawned:
                if proc.returncode is None:
                    proc.kill()
                    await proc.wait()

        assert observed is not None
        assert observed.own_session is False

    @pytest.mark.skipif(os.name != "posix", reason="sessions are a POSIX notion")
    @pytest.mark.asyncio
    async def test_a_start_that_fails_another_way_still_ends_its_chrome(self) -> None:
        """nodriver can raise after the spawn for reasons other than "Failed to
        connect" (attach() or update_targets() failing); no retry, but the Chrome
        it spawned is ended before the error propagates."""
        import nodriver.core.browser as nodriver_launcher

        from graftpunk.cli.observe_browser import setup_observe_session

        spawned: list[asyncio.subprocess.Process] = []

        async def fake_nodriver_start(**kwargs: object) -> None:
            proc = await nodriver_launcher.asyncio.create_subprocess_exec(
                sys.executable, "-c", "import time; time.sleep(30)"
            )
            spawned.append(proc)
            raise RuntimeError("boom")

        mock_nodriver = MagicMock()
        mock_nodriver.start = fake_nodriver_start

        try:
            with (
                patch.dict("sys.modules", {"nodriver": mock_nodriver}),
                pytest.raises(RuntimeError, match="boom"),
            ):
                await setup_observe_session(
                    "test-ns", "https://example.com", 1024, headless=True, session_name=None
                )
        finally:
            for proc in spawned:
                if proc.returncode is None:
                    proc.kill()
                    await proc.wait()

        assert len(spawned) == 1
        assert spawned[0].returncode == -signal.SIGTERM

    @pytest.mark.skipif(os.name != "posix", reason="sessions are a POSIX notion")
    @pytest.mark.asyncio
    async def test_a_failed_attempts_chrome_is_registered_at_once_and_ended_before_the_retry(
        self, tmp_path: Path
    ) -> None:
        """nodriver raises "Failed to connect" after it has spawned Chrome. That
        Chrome is in the signal registry from the spawn, and is ended before the
        next attempt starts one more."""
        import nodriver.core.browser as nodriver_launcher

        from graftpunk import signals
        from graftpunk.cli.observe_browser import setup_observe_session

        spawned: list[asyncio.subprocess.Process] = []
        registered_at_spawn: list[bool] = []
        first_ended_before_second: list[bool] = []
        mock_tab = MagicMock()
        mock_browser = MagicMock()
        mock_browser.main_tab = mock_tab
        mock_browser.get = AsyncMock(return_value=mock_tab)
        mock_backend = MagicMock()
        mock_backend.start_capture_async = AsyncMock()

        async def fake_nodriver_start(**kwargs: object) -> MagicMock:
            if spawned:
                first_ended_before_second.append(spawned[0].returncode is not None)
                return mock_browser
            proc = await nodriver_launcher.asyncio.create_subprocess_exec(
                sys.executable, "-c", "import time; time.sleep(30)"
            )
            spawned.append(proc)
            registered_at_spawn.append(
                any(getattr(h, "_process", None) is proc for h in signals.live_browsers())
            )
            raise Exception("Failed to connect to browser")

        mock_nodriver = MagicMock()
        mock_nodriver.start = fake_nodriver_start
        mock_storage = MagicMock()
        mock_storage.run_dir = tmp_path / "run"

        try:
            with (
                patch.dict("sys.modules", {"nodriver": mock_nodriver}),
                patch(
                    "graftpunk.observe.capture.NodriverCaptureBackend", return_value=mock_backend
                ),
                patch("graftpunk.observe.storage.ObserveStorage", return_value=mock_storage),
            ):
                observed = await setup_observe_session(
                    "test-ns", "https://example.com", 1024, headless=True, session_name=None
                )
        finally:
            for proc in spawned:
                if proc.returncode is None:
                    proc.kill()
                    await proc.wait()

        assert observed is not None
        assert registered_at_spawn == [True]
        assert first_ended_before_second == [True]
        assert spawned[0].returncode == -signal.SIGTERM
        assert not any(getattr(h, "_process", None) is spawned[0] for h in signals.live_browsers())

    @pytest.mark.asyncio
    async def test_sigint_restored_even_if_nodriver_start_raises(self) -> None:
        """If nodriver.start() raises, the original SIGINT handler is still restored."""
        from graftpunk.cli.observe_browser import setup_observe_session

        async def failing_nodriver_start(**kwargs: object) -> None:
            raise RuntimeError("browser launch failed")

        mock_nodriver = MagicMock()
        mock_nodriver.start = failing_nodriver_start

        original_handler = signal.getsignal(signal.SIGINT)

        with (
            patch.dict("sys.modules", {"nodriver": mock_nodriver}),
            pytest.raises(RuntimeError, match="browser launch failed"),
        ):
            await setup_observe_session(
                "test-ns",
                "https://example.com",
                5 * 1024 * 1024,
                headless=False,
                session_name=None,
            )

        # Original handler must be restored even after an exception
        assert signal.getsignal(signal.SIGINT) == original_handler


class TestObserveBrowserHygiene:
    """gp observe marks its Chrome, registers it, and cleans up after it (#96)."""

    @staticmethod
    def _browser(profile: Path | None) -> MagicMock:
        """A fake browser whose config says nodriver made the profile directory."""
        tab = MagicMock()
        tab.sleep = AsyncMock()
        browser = MagicMock()
        browser.main_tab = tab
        browser.get = AsyncMock(return_value=tab)
        browser.config.uses_custom_data_dir = False
        browser.config.user_data_dir = str(profile) if profile is not None else None
        return browser

    @staticmethod
    def _capture_backend() -> MagicMock:
        backend = MagicMock()
        backend.start_capture_async = AsyncMock()
        return backend

    @pytest.mark.asyncio
    async def test_the_shared_switches_reach_nodriver_start(self, tmp_path: Path) -> None:
        from graftpunk.chrome_orphans import base_browser_args
        from graftpunk.cli.observe_browser import setup_observe_session

        started: list = []

        async def fake_start(**kwargs: object) -> MagicMock:
            started.append(kwargs)
            return self._browser(None)

        mock_nodriver = MagicMock()
        mock_nodriver.start = fake_start
        storage = MagicMock()
        storage.run_dir = tmp_path / "run"

        with (
            patch.dict("sys.modules", {"nodriver": mock_nodriver}),
            patch(
                "graftpunk.observe.capture.NodriverCaptureBackend",
                return_value=self._capture_backend(),
            ),
            patch("graftpunk.observe.storage.ObserveStorage", return_value=storage),
        ):
            await setup_observe_session(
                "test-ns", "https://example.com", 5 * 1024 * 1024, headless=True, session_name=None
            )

        (kwargs,) = started
        assert kwargs["browser_args"] == base_browser_args()

    @pytest.mark.asyncio
    async def test_the_sweep_runs_before_the_browser_starts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from graftpunk.browser_launch import CleanupReport
        from graftpunk.cli.observe_browser import setup_observe_session

        order: list[str] = []

        def fake_prepare() -> CleanupReport:
            order.append("sweep")
            return CleanupReport()

        monkeypatch.setattr("graftpunk.cli.observe_browser.prepare_browser_launch", fake_prepare)

        async def fake_start(**kwargs: object) -> MagicMock:
            order.append("start")
            return self._browser(None)

        mock_nodriver = MagicMock()
        mock_nodriver.start = fake_start
        storage = MagicMock()
        storage.run_dir = tmp_path / "run"

        with (
            patch.dict("sys.modules", {"nodriver": mock_nodriver}),
            patch(
                "graftpunk.observe.capture.NodriverCaptureBackend",
                return_value=self._capture_backend(),
            ),
            patch("graftpunk.observe.storage.ObserveStorage", return_value=storage),
        ):
            await setup_observe_session(
                "test-ns", "https://example.com", 5 * 1024 * 1024, headless=True, session_name=None
            )

        assert order == ["sweep", "start"]

    @pytest.mark.asyncio
    async def test_a_reaped_run_tells_the_user_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        from graftpunk.browser_launch import CleanupReport
        from graftpunk.chrome_orphans import ChromeProcess
        from graftpunk.cli.observe_browser import setup_observe_session

        orphan = ChromeProcess(pid=4242, ppid=1, args="", owner_pid=999999, user_data_dir=None)
        monkeypatch.setattr(
            "graftpunk.cli.observe_browser.prepare_browser_launch",
            lambda: CleanupReport(reaped=[orphan]),
        )
        mock_nodriver = MagicMock()
        mock_nodriver.start = AsyncMock(return_value=self._browser(None))
        storage = MagicMock()
        storage.run_dir = tmp_path / "run"

        with (
            patch.dict("sys.modules", {"nodriver": mock_nodriver}),
            patch(
                "graftpunk.observe.capture.NodriverCaptureBackend",
                return_value=self._capture_backend(),
            ),
            patch("graftpunk.observe.storage.ObserveStorage", return_value=storage),
        ):
            await setup_observe_session(
                "test-ns", "https://example.com", 5 * 1024 * 1024, headless=True, session_name=None
            )

        assert "Cleaned up 1 orphaned Chrome process(es)" in strip_ansi(capsys.readouterr().out)

    @pytest.mark.asyncio
    async def test_the_browser_is_registered_for_signals_and_released_on_stop(
        self, tmp_path: Path
    ) -> None:
        from graftpunk.cli.observe_browser import run_observe_go
        from graftpunk.signals import live_browsers

        registered: list = []
        browser = self._browser(None)
        mock_nodriver = MagicMock()
        mock_nodriver.start = AsyncMock(return_value=browser)
        storage = MagicMock()
        storage.run_dir = tmp_path / "run"

        async def record_then_save(*args: object, **kwargs: object) -> None:
            registered.extend(live_browsers())

        with (
            patch.dict("sys.modules", {"nodriver": mock_nodriver}),
            patch(
                "graftpunk.observe.capture.NodriverCaptureBackend",
                return_value=self._capture_backend(),
            ),
            patch("graftpunk.observe.storage.ObserveStorage", return_value=storage),
            patch("graftpunk.cli.observe_browser.save_observe_run", new=record_then_save),
        ):
            await run_observe_go(
                "test-ns", "https://example.com", 0.0, 5 * 1024 * 1024, session_name=None
            )

        # Registered while the browser was up, and gone once it was stopped.
        assert any(getattr(h, "_browser", None) is browser for h in registered)
        assert not any(getattr(h, "_browser", None) is browser for h in live_browsers())

    @pytest.mark.asyncio
    async def test_the_temp_profile_is_removed_after_the_browser_stops(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The leak fix at the second launch site: observe made the directory too."""
        import tempfile

        from graftpunk.cli.observe_browser import run_observe_go

        temp_root = tmp_path / "tmp"
        temp_root.mkdir()
        monkeypatch.setattr(tempfile, "gettempdir", lambda: str(temp_root))
        profile = temp_root / "uc_observe"
        (profile / "Default").mkdir(parents=True)
        mock_nodriver = MagicMock()
        mock_nodriver.start = AsyncMock(return_value=self._browser(profile))
        storage = MagicMock()
        storage.run_dir = tmp_path / "run"

        with (
            patch.dict("sys.modules", {"nodriver": mock_nodriver}),
            patch(
                "graftpunk.observe.capture.NodriverCaptureBackend",
                return_value=self._capture_backend(),
            ),
            patch("graftpunk.observe.storage.ObserveStorage", return_value=storage),
            patch("graftpunk.cli.observe_browser.save_observe_run", new_callable=AsyncMock),
        ):
            await run_observe_go(
                "test-ns", "https://example.com", 0.0, 5 * 1024 * 1024, session_name=None
            )

        assert not profile.exists()

    def test_a_stop_that_raises_still_releases_the_browser(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The unregister and the profile removal sit in a finally behind the stop."""
        import tempfile

        from graftpunk.browser_launch import NodriverBrowserHandle
        from graftpunk.cli.observe_browser import stop_observe_browser
        from graftpunk.signals import live_browsers, register_live_browser

        temp_root = tmp_path / "tmp"
        temp_root.mkdir()
        monkeypatch.setattr(tempfile, "gettempdir", lambda: str(temp_root))
        profile = temp_root / "uc_raised"
        (profile / "Default").mkdir(parents=True)
        browser = self._browser(profile)
        browser.stop = MagicMock(side_effect=ValueError("stop blew up"))
        handle = NodriverBrowserHandle(browser)
        register_live_browser(handle)

        with pytest.raises(ValueError, match="stop blew up"):
            stop_observe_browser(browser, handle)

        assert handle not in live_browsers()
        assert not profile.exists()


class TestObserveArmsTheSignalHandlers:
    """The root callback sets a flag; it takes no signal slot by itself (#96)."""

    def test_the_callback_sets_the_flag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from graftpunk import signals

        monkeypatch.setattr(signals, "auto_install", False)
        signal.signal(signal.SIGTERM, signal.SIG_DFL)

        result = runner.invoke(app, ["version"])

        assert result.exit_code == 0, result.output
        assert signals.auto_install is True
        # A command that opens no browser leaves the slot alone.
        assert signal.getsignal(signal.SIGTERM) is signal.SIG_DFL

    @pytest.mark.asyncio
    async def test_the_start_path_arms_the_handlers(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Arming runs on this thread, not inside the worker the sweep runs on.

        ``signal.signal`` raises off the main thread and the installer logs
        that at debug, so arming inside the threaded sweep would leave the
        process with no handlers and no error.
        """
        from graftpunk import chrome_orphans, signals
        from graftpunk.browser_launch import CleanupReport
        from graftpunk.cli.observe_browser import setup_observe_session

        monkeypatch.setattr(chrome_orphans, "_ARMED", True)
        # The sweep itself is replaced at its call site, so an armed pass in
        # this test can never reach the real process table.
        monkeypatch.setattr(
            "graftpunk.cli.observe_browser.prepare_browser_launch", lambda: CleanupReport()
        )
        monkeypatch.setattr(signals, "auto_install", True)
        signal.signal(signal.SIGTERM, signal.SIG_DFL)

        hygiene = TestObserveBrowserHygiene
        mock_nodriver = MagicMock()
        mock_nodriver.start = AsyncMock(return_value=hygiene._browser(None))
        storage = MagicMock()
        storage.run_dir = tmp_path / "run"

        with (
            patch.dict("sys.modules", {"nodriver": mock_nodriver}),
            patch(
                "graftpunk.observe.capture.NodriverCaptureBackend",
                return_value=hygiene._capture_backend(),
            ),
            patch("graftpunk.observe.storage.ObserveStorage", return_value=storage),
        ):
            await setup_observe_session(
                "test-ns", "https://example.com", 5 * 1024 * 1024, headless=True, session_name=None
            )

        assert signal.getsignal(signal.SIGTERM) is signals._handle_termination
