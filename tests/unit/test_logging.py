"""Tests for graftpunk logging utilities."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

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

    Only the gp CLI calls configure_logging(). With structlog unconfigured,
    graftpunk's own loggers write WARNING and above to stderr through a
    configuration they keep to themselves, rather than structlog's default
    stdout PrintLogger with no level filter.
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


def _run_isolated(code: str, home: Path) -> subprocess.CompletedProcess[str]:
    """Run *code* in a fresh interpreter from the repo root, with HOME and the
    graftpunk config dir under *home* so nothing touches the real home."""
    env = {
        **os.environ,
        "NO_COLOR": "1",
        "FORCE_COLOR": "",
        "HOME": str(home),
        "GRAFTPUNK_CONFIG_DIR": str(home / "config"),
    }
    env.pop("GRAFTPUNK_SESSION", None)
    return subprocess.run(  # noqa: S603
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=env,
        cwd=Path(__file__).resolve().parents[2],
        check=False,
        timeout=120,
    )


_EXERCISE_GRAFTPUNK = """
import requests
import graftpunk
import graftpunk.plugins
from graftpunk import cache_session, load_session_for_api
cache_session(requests.Session(), "probe")
load_session_for_api("probe")

from graftpunk.plugins.cli_plugin import SitePlugin, command
from tests.unit.cli_harness import invoke_plugin_app

class Probe(SitePlugin):
    site_name = "probesite"
    base_url = "http://localhost:9"
    requires_session = False

    @command(help="Say hi")
    def hi(self, ctx):
        return {"ok": True}

result = invoke_plugin_app(Probe(), ["probesite", "hi"])
assert result.exit_code == 0, result.output
"""


class TestProgramStructlogIsUntouched:
    """graftpunk never configures structlog for the program that imports it."""

    def test_using_graftpunk_leaves_structlog_unconfigured(self, tmp_path: Path) -> None:
        proc = _run_isolated(
            "import sys, warnings, structlog\n"
            + _EXERCISE_GRAFTPUNK
            + "assert 'graftpunk.cli.main' not in sys.modules\n"
            "assert not structlog.is_configured()\n"
            "structlog.get_logger().info('consumer_info_line')\n"
            "with warnings.catch_warnings():\n"
            "    warnings.simplefilter('error')\n"
            "    structlog.configure_once(processors=[structlog.processors.JSONRenderer()])\n"
            "structlog.get_logger().warning('after_configure_once')\n",
            tmp_path,
        )
        assert proc.returncode == 0, proc.stderr
        lines = proc.stdout.splitlines()
        assert "consumer_info_line" in lines[0], proc.stdout
        assert lines[1:] == ['{"event": "after_configure_once"}']

    @pytest.mark.parametrize("configure_first", [True, False])
    def test_graftpunk_lines_follow_the_program_configuration(self, configure_first: bool) -> None:
        configure = (
            "import io, logging, structlog\n"
            "buf = io.StringIO()\n"
            "structlog.configure(\n"
            "    processors=[structlog.processors.JSONRenderer()],\n"
            "    wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),\n"
            "    logger_factory=structlog.PrintLoggerFactory(file=buf),\n"
            ")\n"
        )
        use = (
            "from graftpunk.logging import get_logger\n"
            "get_logger('t').debug('debug_marker')\n"
            "get_logger('t').info('info_marker', k=1)\n"
            'assert buf.getvalue() == \'{"k": 1, "event": "info_marker"}\\n\', buf.getvalue()\n'
        )
        imported = "import graftpunk\n"
        code = configure + imported + use if configure_first else imported + configure + use
        proc = _run(code)
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout == ""
        assert proc.stderr == ""

    def test_a_logger_bound_before_configuration_follows_it(self) -> None:
        proc = _run(
            "import io, structlog\n"
            "from graftpunk.logging import get_logger\n"
            "log = get_logger('t').bind(site='s')\n"
            "log.info('dropped_info')\n"
            "buf = io.StringIO()\n"
            "structlog.configure(\n"
            "    processors=[structlog.processors.JSONRenderer()],\n"
            "    logger_factory=structlog.PrintLoggerFactory(file=buf),\n"
            ")\n"
            "log.info('kept_info')\n"
            "assert buf.getvalue() == "
            '\'{"site": "s", "event": "kept_info"}\\n\', buf.getvalue()\n'
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout == ""
        assert "dropped_info" not in proc.stderr


class TestGraftpunkLoggerContext:
    def test_bind_new_unbind_and_try_unbind(self, captured_logs: list[dict[str, Any]]) -> None:
        from graftpunk.logging import get_logger

        log = get_logger("t").bind(a=1, b=2)
        log.info("bound")
        log.unbind("a").info("unbound")
        log.try_unbind("a", "missing").info("try_unbound")
        log.new(c=3).info("fresh")
        log.bind(b=5).warning("rebound")

        contexts = [{k: v for k, v in e.items() if k != "log_level"} for e in captured_logs]
        assert contexts == [
            {"a": 1, "b": 2, "event": "bound"},
            {"b": 2, "event": "unbound"},
            {"b": 2, "event": "try_unbound"},
            {"c": 3, "event": "fresh"},
            {"a": 1, "b": 5, "event": "rebound"},
        ]

    def test_unbind_of_an_unbound_key_raises(self) -> None:
        from graftpunk.logging import get_logger

        with pytest.raises(KeyError):
            get_logger("t").unbind("missing")

    def test_copy_and_pickle_keep_the_context(self) -> None:
        import copy
        import pickle

        from graftpunk.logging import get_logger

        log = get_logger("t").bind(a=1)
        for clone in (copy.copy(log), copy.deepcopy(log), pickle.loads(pickle.dumps(log))):  # noqa: S301
            assert clone._context == {"a": 1}

    def test_unconfigured_info_and_debug_are_dropped_and_warning_reaches_stderr(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        import structlog

        from graftpunk.logging import get_logger

        assert not structlog.is_configured()
        log = get_logger("t")
        log.debug("debug_marker")
        log.info("info_marker")
        log.warning("warning_marker")
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "warning_marker" in captured.err
        assert "info_marker" not in captured.err
        assert "debug_marker" not in captured.err
        assert not structlog.is_configured()


# The keyword parameters of structlog.wrap_logger and structlog.get_logger. A
# context key with one of these names must reach the line like any other key.
_WRAP_LOGGER_PARAMETER_NAMES = [
    "logger",
    "processors",
    "wrapper_class",
    "context_class",
    "cache_logger_on_first_use",
    "logger_factory_args",
]


class TestBoundKeysNamedLikeStructlogParameters:
    @pytest.mark.parametrize("key", _WRAP_LOGGER_PARAMETER_NAMES)
    def test_unconfigured(self, key: str, capsys: pytest.CaptureFixture[str]) -> None:
        import structlog

        from graftpunk.logging import get_logger

        structlog.get_logger().bind(**{key: "bound_value"}).warning("line")
        plain = capsys.readouterr().out
        get_logger("t").bind(**{key: "bound_value"}).warning("line")
        captured = capsys.readouterr()
        assert captured.out == ""
        assert not structlog.is_configured()
        # The timestamp formats differ; the rest of the console line is the same.
        level = "[warning  ] "
        assert "bound_value" in plain
        assert captured.err.split(level, 1)[1] == plain.split(level, 1)[1]

    @pytest.mark.parametrize("key", _WRAP_LOGGER_PARAMETER_NAMES)
    def test_configured(self, key: str) -> None:
        import io

        import structlog

        from graftpunk.logging import get_logger

        buf = io.StringIO()
        structlog.configure(
            processors=[structlog.processors.JSONRenderer(sort_keys=True)],
            logger_factory=structlog.PrintLoggerFactory(file=buf),
        )
        structlog.get_logger("t").bind(**{key: "bound_value"}).warning("line")
        get_logger("t").bind(**{key: "bound_value"}).warning("line")
        plain, graftpunk = buf.getvalue().splitlines()
        assert json.loads(plain) == {"event": "line", key: "bound_value"}
        assert graftpunk == plain


class TestQuietLoggerStream:
    def test_dropped_lines_keep_no_redirected_stderr_alive(self) -> None:
        import gc
        import weakref
        from contextlib import redirect_stderr
        from io import StringIO

        from graftpunk.logging import get_logger

        log = get_logger("t")
        refs = []
        for _ in range(1000):
            stream = StringIO()
            refs.append(weakref.ref(stream))
            with redirect_stderr(stream):
                log.debug("dropped")
            del stream
        gc.collect()
        assert [ref for ref in refs if ref() is not None] == []

    def test_a_warning_goes_to_the_stderr_of_the_moment(self) -> None:
        from contextlib import redirect_stderr
        from io import StringIO

        from graftpunk.logging import get_logger

        log = get_logger("t")
        first, second = StringIO(), StringIO()
        with redirect_stderr(first):
            log.warning("first_marker")
        with redirect_stderr(second):
            log.warning("second_marker")
        assert "first_marker" in first.getvalue()
        assert "second_marker" not in first.getvalue()
        assert "second_marker" in second.getvalue()


def _log_an_exception() -> None:
    from graftpunk.logging import get_logger

    try:
        1 / 0  # noqa: B018
    except ZeroDivisionError:
        get_logger("t").exception("boom_marker")


class TestExceptionRendering:
    """``exception()`` renders the traceback in every format, with no structlog warning."""

    @pytest.mark.parametrize("setup", ["unconfigured", "cli_console", "cli_json"])
    def test_exception_renders_the_traceback(
        self, setup: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        import warnings

        from graftpunk.logging import configure_logging

        if setup != "unconfigured":
            configure_logging(level="WARNING", json_output=setup == "cli_json")
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            _log_an_exception()
        captured = capsys.readouterr()
        assert captured.out == ""
        if setup == "cli_json":
            line = json.loads(captured.err)
            assert line["event"] == "boom_marker"
            assert line["exception"].startswith("Traceback (most recent call last):")
            assert "ZeroDivisionError: division by zero" in line["exception"]
        else:
            assert "boom_marker" in captured.err
            assert "Traceback (most recent call last):" in captured.err
            assert "ZeroDivisionError: division by zero" in captured.err
            assert "\n    1 / 0" in captured.err
