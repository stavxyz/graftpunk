"""Tests for graftpunk logging utilities."""

from __future__ import annotations

import logging
import os
import subprocess
import sys

import pytest

from graftpunk.logging import suppress_asyncio_noise


class TestSuppressAsyncioNoise:
    """Tests for suppress_asyncio_noise context manager."""

    def test_suppresses_asyncio_warnings(self) -> None:
        asyncio_logger = logging.getLogger("asyncio")
        original_level = asyncio_logger.level

        with suppress_asyncio_noise():
            assert asyncio_logger.level == logging.CRITICAL

        assert asyncio_logger.level == original_level

    def test_restores_level_on_exception(self) -> None:
        asyncio_logger = logging.getLogger("asyncio")
        original_level = asyncio_logger.level

        with pytest.raises(RuntimeError), suppress_asyncio_noise():
            raise RuntimeError("test")

        assert asyncio_logger.level == original_level

    def test_restores_custom_level(self) -> None:
        asyncio_logger = logging.getLogger("asyncio")
        asyncio_logger.setLevel(logging.DEBUG)

        with suppress_asyncio_noise():
            assert asyncio_logger.level == logging.CRITICAL

        assert asyncio_logger.level == logging.DEBUG
        # Reset
        asyncio_logger.setLevel(logging.WARNING)

    def test_overlapping_windows_on_one_loop_restore_the_original_level(self) -> None:
        """Two backends' stop_async() gathered on one loop end in the other order."""
        import asyncio

        asyncio_logger = logging.getLogger("asyncio")
        asyncio_logger.setLevel(logging.INFO)
        levels_inside: list[int] = []

        async def window(delay: float) -> None:
            with suppress_asyncio_noise():
                await asyncio.sleep(delay)
                levels_inside.append(asyncio_logger.level)

        async def overlap() -> None:
            await asyncio.gather(window(0.05), window(0.1))

        try:
            asyncio.run(overlap())
            assert levels_inside == [logging.CRITICAL, logging.CRITICAL]
            assert asyncio_logger.level == logging.INFO
        finally:
            asyncio_logger.setLevel(logging.NOTSET)

    def test_overlapping_windows_on_two_threads_restore_the_original_level(self) -> None:
        """The first window in is the first out, the second restores last."""
        import threading

        asyncio_logger = logging.getLogger("asyncio")
        asyncio_logger.setLevel(logging.INFO)
        first_in = threading.Event()
        second_in = threading.Event()
        first_out = threading.Event()
        level_after_first_out: list[int] = []

        def first() -> None:
            with suppress_asyncio_noise():
                first_in.set()
                second_in.wait(5)
            first_out.set()

        def second() -> None:
            first_in.wait(5)
            with suppress_asyncio_noise():
                second_in.set()
                first_out.wait(5)
                level_after_first_out.append(asyncio_logger.level)

        threads = [threading.Thread(target=first), threading.Thread(target=second)]
        try:
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(5)
            assert level_after_first_out == [logging.CRITICAL]
            assert asyncio_logger.level == logging.INFO
        finally:
            asyncio_logger.setLevel(logging.NOTSET)


_HIDE_JMESPATH = """
import builtins
_real_import = builtins.__import__
def _hide(name, *args, **kwargs):
    if name.split(".")[0] == "jmespath":
        raise ImportError("hidden for test")
    return _real_import(name, *args, **kwargs)
builtins.__import__ = _hide
"""


def _run(code: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "NO_COLOR": "1", "FORCE_COLOR": ""}
    return subprocess.run(  # noqa: S603
        [sys.executable, "-c", code], capture_output=True, text=True, env=env, check=False
    )


class TestLibraryLoggingDefaults:
    """Importing graftpunk as a library must never write log lines to stdout.

    Only the gp CLI calls configure_logging(); a library consumer gets whatever
    structlog does by default, which is a stdout PrintLogger with no level filter.
    Regression for https://github.com/stavxyz/graftpunk/issues/163.
    """

    def test_import_with_jmespath_missing_writes_nothing_to_stdout(self) -> None:
        proc = _run(_HIDE_JMESPATH + "import graftpunk\nimport graftpunk.plugins.yaml_plugin\n")
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout == ""

    def test_library_default_routes_to_stderr_and_filters_below_warning(self) -> None:
        proc = _run(
            "import graftpunk\n"
            "from graftpunk.logging import get_logger\n"
            "log = get_logger('t')\n"
            "log.debug('dbg_marker')\n"
            "log.info('info_marker')\n"
            "log.warning('warn_marker')\n"
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout == ""
        assert "warn_marker" in proc.stderr
        assert "dbg_marker" not in proc.stderr
        assert "info_marker" not in proc.stderr

    def test_consumer_structlog_configuration_is_not_overridden(self) -> None:
        proc = _run(
            "import io, structlog\n"
            "buf = io.StringIO()\n"
            "structlog.configure(logger_factory=structlog.PrintLoggerFactory(file=buf))\n"
            "factory = structlog.get_config()['logger_factory']\n"
            "import graftpunk\n"
            "from graftpunk.logging import get_logger\n"
            "assert structlog.get_config()['logger_factory'] is factory\n"
            "get_logger('t').warning('consumer_marker')\n"
            "assert 'consumer_marker' in buf.getvalue(), buf.getvalue()\n"
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout == ""
        assert "consumer_marker" not in proc.stderr

    def test_consumer_configuration_after_import_wins(self) -> None:
        proc = _run(
            "import io, structlog\n"
            "import graftpunk\n"
            "buf = io.StringIO()\n"
            "structlog.configure(logger_factory=structlog.PrintLoggerFactory(file=buf))\n"
            "from graftpunk.logging import get_logger\n"
            "get_logger('t').warning('late_marker')\n"
            "assert 'late_marker' in buf.getvalue(), buf.getvalue()\n"
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout == ""
        assert "late_marker" not in proc.stderr

    def test_importing_cli_module_preserves_consumer_configuration(self) -> None:
        proc = _run(
            "import io, structlog\n"
            "buf = io.StringIO()\n"
            "structlog.configure(logger_factory=structlog.PrintLoggerFactory(file=buf))\n"
            "factory = structlog.get_config()['logger_factory']\n"
            "import graftpunk.cli.main\n"
            "assert structlog.get_config()['logger_factory'] is factory\n"
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout == ""

    def test_cli_module_still_honours_env_log_level(self) -> None:
        proc = _run(
            "import os; os.environ['GRAFTPUNK_LOG_LEVEL'] = 'DEBUG'\n"
            "import graftpunk.cli.main\n"
            "from graftpunk.logging import get_logger\n"
            "get_logger('t').debug('cli_dbg_marker')\n"
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout == ""
        assert "cli_dbg_marker" in proc.stderr

    def test_no_color_is_honoured_on_stderr(self) -> None:
        proc = _run(
            "import graftpunk\n"
            "from graftpunk.logging import get_logger\n"
            "get_logger('t').warning('plain_marker')\n"
        )
        assert proc.returncode == 0, proc.stderr
        assert "plain_marker" in proc.stderr
        assert "\x1b[" not in proc.stderr


@pytest.mark.parametrize("color_env", ["unset", "NO_COLOR"])
def test_importing_graftpunk_works_without_stdout_and_stderr(color_env: str) -> None:
    """pythonw runs with sys.stdout and sys.stderr set to None; importing graftpunk
    and logging must still work, and the log line must not reach a stream."""
    env = {k: v for k, v in os.environ.items() if k not in {"FORCE_COLOR", "NO_COLOR"}}
    if color_env == "NO_COLOR":
        env["NO_COLOR"] = "1"
    code = (
        "import sys\n"
        "sys.stdout = sys.stderr = None\n"
        "import graftpunk\n"
        "from graftpunk.logging import get_logger\n"
        "get_logger('probe').warning('probe_event')\n"
        "sys.exit(0)\n"
    )
    result = subprocess.run(  # noqa: S603 - fixed argv
        [sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=60
    )
    assert result.returncode == 0, result.stderr
    assert "probe_event" not in result.stdout + result.stderr


def test_with_only_stderr_missing_log_lines_do_not_reach_stdout() -> None:
    """stdout may carry a program's output, so a missing stderr must not send logs there."""
    env = {k: v for k, v in os.environ.items() if k not in {"FORCE_COLOR", "NO_COLOR"}}
    code = (
        "import atexit, sys\n"
        "sys.stderr = None\n"
        "def at_exit():\n"
        "    from graftpunk.logging import get_logger\n"
        "    get_logger('probe').warning('probe_at_exit')\n"
        "    print('exit handler done')\n"
        "atexit.register(at_exit)\n"
        "import graftpunk\n"
        "from graftpunk.logging import get_logger\n"
        "get_logger('probe').warning('probe_event')\n"
        "print('program output')\n"
    )
    result = subprocess.run(  # noqa: S603 - fixed argv
        [sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=60
    )
    assert result.returncode == 0, result.stderr
    # The exit handler was registered before graftpunk, so it runs after anything
    # graftpunk registers; its log call must still find an open sink.
    assert result.stdout == "program output\nexit handler done\n"
