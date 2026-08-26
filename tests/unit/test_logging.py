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
