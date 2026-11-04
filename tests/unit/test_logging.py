"""Tests for graftpunk logging utilities."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, NamedTuple

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
    """With structlog unconfigured, importing and using graftpunk as a library
    must never write log lines to stdout.

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
        from tests.unit.cli_harness import strip_ansi

        structlog.get_logger().bind(**{key: "bound_value"}).warning("line")
        plain = capsys.readouterr().out
        get_logger("t").bind(**{key: "bound_value"}).warning("line")
        captured = capsys.readouterr()
        assert captured.out == ""
        assert not structlog.is_configured()
        # The timestamp formats differ; the rest of the console line is the same.
        # graftpunk colours by FORCE_COLOR, NO_COLOR and the terminal, while
        # structlog's default renderer decided once at import, so compare text.
        level = "[warning  ] "
        err, plain = strip_ansi(captured.err), strip_ansi(plain)
        assert "bound_value" in plain
        assert err.split(level, 1)[1] == plain.split(level, 1)[1]

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

    def test_a_closed_stderr_drops_the_line(self, capsys: pytest.CaptureFixture[str]) -> None:
        from contextlib import redirect_stderr
        from io import StringIO

        from graftpunk.logging import get_logger

        closed = StringIO()
        closed.close()
        with redirect_stderr(closed):
            get_logger("t").warning("dropped_marker")
        assert capsys.readouterr().out == ""

    @pytest.mark.parametrize("failing", ["write", "flush"])
    def test_a_stderr_that_raises_oserror_drops_the_line(
        self, failing: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from contextlib import redirect_stderr
        from io import StringIO

        from graftpunk.logging import get_logger

        class Broken(StringIO):
            def write(self, s: str) -> int:
                if failing == "write":
                    raise OSError(5, "Input/output error")
                return super().write(s)

            def flush(self) -> None:
                if failing == "flush":
                    raise BrokenPipeError(32, "Broken pipe")

        stream = Broken()
        with redirect_stderr(stream):
            get_logger("t").warning("first_marker")
            get_logger("t").warning("second_marker")
        assert capsys.readouterr().out == ""
        if failing == "flush":
            assert "second_marker" in stream.getvalue()


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


def test_unnamed_logger_is_named_after_the_calling_module(tmp_path: Path) -> None:
    """With the stdlib LoggerFactory, ``get_logger()`` names the module that called it."""
    (tmp_path / "plugin_mod.py").write_text(
        "from graftpunk.logging import get_logger\n"
        "LOG = get_logger()\n"
        "def run():\n"
        "    LOG.warning('named_marker')\n"
    )
    proc = _run(
        "import logging, sys, structlog\n"
        f"sys.path.insert(0, {str(tmp_path)!r})\n"
        "logging.basicConfig(stream=sys.stderr, format='%(name)s|%(message)s')\n"
        "structlog.configure(\n"
        "    processors=[structlog.stdlib.add_logger_name,\n"
        "                structlog.processors.KeyValueRenderer(key_order=['event'])],\n"
        "    wrapper_class=structlog.stdlib.BoundLogger,\n"
        "    logger_factory=structlog.stdlib.LoggerFactory(),\n"
        ")\n"
        "import plugin_mod\n"
        "plugin_mod.run()\n"
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stderr == "plugin_mod|event='named_marker' logger='plugin_mod'\n"


# redacted_values over a line holding a container more than once, or itself (#305).
_ACCOUNT = "acct-secret-value"


def _redacted_line(**values: Any) -> dict[str, Any]:
    from structlog.testing import capture_logs

    from graftpunk.logging import get_logger, redacted_values

    with capture_logs() as logs, redacted_values((_ACCOUNT, "<acct>")):
        get_logger("t").warning("line", **values)
    (line,) = logs
    return line


def _shared_tower(depth: int, bottom: str = "plain") -> list[Any]:
    """A list holding one sub-list twice, nested *depth* levels, its innermost
    list also holding a list that holds itself, a dict that holds itself, or
    (``back edge``) the outermost list."""
    innermost: list[Any] = [_ACCOUNT]
    value = innermost
    for _ in range(depth):
        value = [value, value]
    if bottom == "list cycle":
        loop: list[Any] = []
        loop.append(loop)
        innermost.append(loop)
    elif bottom == "dict cycle":
        node: dict[str, Any] = {}
        node["self"] = node
        innermost.append(node)
    elif bottom == "back edge":
        innermost.append(value)
    return value


def test_redacted_values_ends_on_a_container_that_holds_itself() -> None:
    """The rewrite once recursed to Python's limit and raised from the log call."""
    loop: list[Any] = [_ACCOUNT]
    loop.append(loop)
    node: dict[str, Any] = {"key": _ACCOUNT}
    node["self"] = node
    line = _redacted_line(loop=loop, node=node, pair=(_ACCOUNT, loop))
    assert line["loop"] == ["<acct>", "[...]"]
    assert line["node"] == {"key": "<acct>", "self": "{...}"}
    assert line["pair"] == ("<acct>", ["<acct>", "[...]"])
    assert _ACCOUNT not in repr(line)


def test_redacted_values_rewrites_a_shared_container_at_each_of_its_places() -> None:
    expected: Any = ["<acct>"]
    for _ in range(10):
        expected = [expected, expected]
    assert _redacted_line(value=_shared_tower(10))["value"] == expected


class _Pair(NamedTuple):
    a: Any
    b: Any


class _KeywordPair(_Pair):
    """A namedtuple subclass whose constructor takes keywords only."""

    def __new__(cls, *, a: Any, b: Any) -> _KeywordPair:
        return super().__new__(cls, a, b)


class _List(list):  # noqa: FURB189 - a list subclass is the case under test
    pass


def test_redacted_values_rebuilds_a_namedtuple() -> None:
    """The rewrite once passed a namedtuple one generator, and the log call
    raised TypeError, logged or bound."""
    from structlog.testing import capture_logs

    from graftpunk.logging import get_logger, redacted_values

    with capture_logs() as logs, redacted_values((_ACCOUNT, "<acct>")):
        get_logger("t").bind(bound=_Pair(_ACCOUNT, 1)).warning(
            "line",
            pair=_Pair(_ACCOUNT, [_ACCOUNT]),
            keywords=_KeywordPair(a=_ACCOUNT, b=1),
            subclass=_List([_ACCOUNT]),
        )
    (line,) = logs
    assert line["bound"] == _Pair("<acct>", 1)
    assert type(line["pair"]) is _Pair
    assert line["pair"] == _Pair("<acct>", ["<acct>"])
    assert line["keywords"] == ("<acct>", 1)
    assert type(line["subclass"]) is list
    assert line["subclass"] == ["<acct>"]


@pytest.mark.parametrize("kind", ["list", "dict"])
def test_redacted_values_ends_nesting_two_thousand_levels_deep(kind: str) -> None:
    """The rewrite once recursed past Python's limit at about 500 nested lists
    and 1,000 nested dicts, and the log call raised RecursionError. Past
    ``_MAX_SCRUB_DEPTH`` levels a container is a placeholder."""
    from graftpunk.logging import _MAX_SCRUB_DEPTH, _TOO_DEEP

    deep: Any = [_ACCOUNT] if kind == "list" else {"k": _ACCOUNT}
    for _ in range(2_000):
        deep = [deep] if kind == "list" else {"k": deep}
    value = _redacted_line(value=deep)["value"]
    # The line's keyword dict is depth 0, so *value* is depth 1.
    for _ in range(_MAX_SCRUB_DEPTH - 2):
        value = value[0] if kind == "list" else value["k"]
    assert value == ([_TOO_DEEP] if kind == "list" else {"k": _TOO_DEEP})


def _rewrite_cost(value: Any) -> float:
    import time

    best = float("inf")
    for _ in range(3):
        start = time.process_time()
        _redacted_line(value=value)
        best = min(best, time.process_time() - start)
    return best


@pytest.mark.parametrize("bottom", ["plain", "list cycle", "dict cycle"])
def test_redacted_values_on_a_shared_container_costs_what_its_distinct_lists_cost(
    bottom: str,
) -> None:
    """A list holding one sub-list twice, nested 18 levels, within 10 times the
    CPU time of a chain of as many distinct lists. The rewrite once followed
    every path, 2 ** 18 to the innermost list: 6.9 s of CPU for a line at 22
    levels (2026-11-03). With a loop at the bottom it still did, as a container
    whose rewrite closed a loop anywhere below was not kept: about 0.5 s at 18
    levels (2026-11-04)."""
    chain: list[Any] = [_ACCOUNT]
    for _ in range(18):
        chain = [chain, [_ACCOUNT]]
    ratio = _rewrite_cost(_shared_tower(18, bottom)) / max(_rewrite_cost(chain), 1e-4)
    assert ratio < 10, ratio


def test_redacted_values_on_a_shared_container_that_holds_the_outermost() -> None:
    """Six more levels within 16 times the CPU time. With the innermost list
    holding the outermost, no level's result can be kept, so the rewrite follows
    every path until the line's value budget runs out; it once followed all of
    them: 0.025 s at 14 levels and 0.40 s at 18 (2026-11-04), doubling with each."""
    small = _rewrite_cost(_shared_tower(12, "back edge"))
    large = _rewrite_cost(_shared_tower(18, "back edge"))
    assert large / max(small, 1e-4) < 16, (small, large)
