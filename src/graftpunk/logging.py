"""Structured logging using structlog, without configuring structlog for the program.

graftpunk's modules log through :func:`get_logger`. Importing graftpunk changes
nothing in structlog: a program's own loggers keep structlog's defaults until the
program configures them. Only the ``gp`` CLI, which owns its process, calls
:func:`configure_logging`.
"""

from __future__ import annotations

import logging as stdlib_logging
import os
import sys
import threading
from contextlib import contextmanager
from typing import Any, TextIO

import structlog
from structlog.typing import EventDict, WrappedLogger


def add_log_level(logger: WrappedLogger, method_name: str, event_dict: EventDict) -> EventDict:
    """Add the log level to the event dict."""
    if method_name == "warn":
        # Translate "warn" to "warning"
        event_dict["level"] = "warning"
    else:
        event_dict["level"] = method_name
    return event_dict


# The logger factory graftpunk last installed. Lets configured_by_consumer()
# tell "structlog is configured because graftpunk configured it" apart from
# "the host program configured structlog itself".
_owned_factory: object | None = None


def _stderr_wants_color() -> bool:
    """Mirror rich's rule: FORCE_COLOR (non-empty) wins, then NO_COLOR, then a TTY."""
    force = os.environ.get("FORCE_COLOR")
    if force is not None:
        return force != ""
    if os.environ.get("NO_COLOR"):
        return False
    # pythonw and some embedding hosts run with sys.stderr set to None.
    isatty = getattr(sys.stderr, "isatty", None)
    try:
        return bool(isatty and isatty())
    except (OSError, ValueError):
        return False


def configured_by_consumer() -> bool:
    """True when structlog carries a configuration graftpunk did not install.

    Importing graftpunk configures nothing; only :func:`configure_logging` (the
    ``gp`` CLI) does. So this is False while structlog is unconfigured or holds
    the CLI's configuration, and True once the host program configured it.
    """
    if not structlog.is_configured():
        return False
    return structlog.get_config().get("logger_factory") is not _owned_factory


def _processors(json_output: bool, colors: bool) -> list[Any]:
    # ConsoleRenderer formats exc_info itself and warns when format_exc_info
    # ran before it (structlog 21.2.0 and later), so only JSON output gets it.
    # plain_traceback keeps the console traceback as format_exc_info wrote it.
    tail: list[Any] = (
        [structlog.processors.format_exc_info, structlog.processors.JSONRenderer()]
        if json_output
        else [
            structlog.dev.ConsoleRenderer(
                colors=colors, exception_formatter=structlog.dev.plain_traceback
            )
        ]
    )
    return [
        structlog.contextvars.merge_contextvars,
        add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
        *tail,
    ]


def _stderr_or_devnull() -> TextIO:
    # With no stderr (pythonw), log lines go nowhere rather than to stdout,
    # which may carry a program's output.
    if sys.stderr is not None:
        return sys.stderr
    from graftpunk._stdout_fd import open_devnull

    return open_devnull()


def configure_logging(
    level: str = "WARNING",
    json_output: bool = False,
) -> None:
    """Configure structlog for the whole process, as the ``gp`` CLI does.

    This replaces any structlog configuration the process has, so only a
    program that owns its process should call it. A library consumer does not
    need to: :func:`get_logger` keeps graftpunk's lines on stderr at WARNING
    while structlog is unconfigured.

    Args:
        level: Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL).
        json_output: If True, output JSON format. If False, use console-friendly format.

    Console output is coloured only when stderr is a TTY (or FORCE_COLOR is set)
    and NO_COLOR is unset.
    """
    global _owned_factory
    _owned_factory = structlog.PrintLoggerFactory(file=_stderr_or_devnull())
    structlog.configure(
        processors=_processors(json_output, _stderr_wants_color()),
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(stdlib_logging, level.upper(), stdlib_logging.INFO)
        ),
        context_class=dict,
        logger_factory=_owned_factory,
        cache_logger_on_first_use=False,
    )


# The configuration graftpunk's own lines use while structlog is unconfigured.
# It is never installed in structlog, so the program's loggers are untouched.
_quiet_wrapper = structlog.make_filtering_bound_logger(stdlib_logging.WARNING)
_quiet_processors: dict[bool, list[Any]] = {}


class _CurrentStderrLogger:
    """A stand-in for structlog's PrintLogger that writes to ``sys.stderr`` as of each write.

    CliRunner, pytest and ``contextlib.redirect_stderr`` swap ``sys.stderr``,
    and pythonw sets it to None. A PrintLogger per stream would also register
    each stream in structlog's process-wide lock table, which never lets go of
    it, so one instance serves every stream under one lock. A stream that is
    closed or fails to write loses the line instead of raising: a log call must
    not crash the program, and the stream that failed is where a report of the
    failure would go.
    """

    _lock = threading.Lock()

    def msg(self, message: str) -> None:
        with self._lock:
            stream = _stderr_or_devnull()
            try:
                stream.write(message + "\n")
                stream.flush()
            except (OSError, ValueError):
                pass

    log = debug = info = warn = warning = msg
    fatal = failure = err = error = critical = exception = msg


_current_stderr_logger = _CurrentStderrLogger()


def _quiet_logger(initial_values: dict[str, Any]) -> Any:
    colors = _stderr_wants_color()
    processors = _quiet_processors.get(colors)
    if processors is None:
        processors = _quiet_processors.setdefault(colors, _processors(False, colors))
    # The context is bound afterwards, never passed to wrap_logger, whose
    # keyword parameters (logger, processors, ...) a context key may share.
    return structlog.wrap_logger(
        _current_stderr_logger,
        processors=processors,
        wrapper_class=_quiet_wrapper,
        context_class=dict,
        cache_logger_on_first_use=False,
    ).bind(**initial_values)


class GraftpunkLogger:
    """A structlog logger that picks its configuration on every call.

    While structlog is configured (by the program, or by the ``gp`` CLI), each
    call goes through that configuration, as a logger from
    ``structlog.get_logger()`` would. While it is not, each call goes through a
    configuration graftpunk keeps to itself: stderr (devnull when
    ``sys.stderr`` is None), WARNING and above. So while structlog is
    unconfigured graftpunk never writes a log line to stdout (#163), and it
    never configures structlog for the program; once the program configures
    structlog, graftpunk's lines go wherever that configuration sends them.

    ``bind()``, ``new()``, ``unbind()`` and ``try_unbind()`` return another
    ``GraftpunkLogger`` holding the new context, so a logger bound before the
    program configures structlog follows that configuration afterwards. Every
    other attribute (``info``, ``warning``, ``ainfo``, ``is_enabled_for`` and so
    on) is looked up on a bound logger built for that call. A program that
    configures ``cache_logger_on_first_use=True`` gets no caching for these
    loggers: they rebuild on each call, as structlog does with caching off.
    """

    def __init__(self, factory_args: tuple[Any, ...], initial_values: dict[str, Any]) -> None:
        self._factory_args = factory_args
        self._initial_values = initial_values

    @property
    def _context(self) -> dict[str, Any]:
        return self._initial_values

    def __repr__(self) -> str:
        return (
            f"<GraftpunkLogger(logger_factory_args={self._factory_args!r}, "
            f"initial_values={self._initial_values!r})>"
        )

    def _resolve(self) -> Any:
        if structlog.is_configured():
            # As in _quiet_logger: get_logger's keyword parameters are not context.
            return structlog.get_logger(*self._factory_args).bind(**self._initial_values)
        return _quiet_logger(self._initial_values)

    def _with(self, values: dict[str, Any]) -> GraftpunkLogger:
        return GraftpunkLogger(self._factory_args, values)

    def bind(self, **new_values: Any) -> GraftpunkLogger:
        return self._with({**self._initial_values, **new_values})

    def new(self, **new_values: Any) -> GraftpunkLogger:
        return self._with(dict(new_values))

    def unbind(self, *keys: str) -> GraftpunkLogger:
        """Raises KeyError for a key that is not bound, as structlog's ``unbind`` does."""
        values = dict(self._initial_values)
        for key in keys:
            del values[key]
        return self._with(values)

    def try_unbind(self, *keys: str) -> GraftpunkLogger:
        values = dict(self._initial_values)
        for key in keys:
            values.pop(key, None)
        return self._with(values)

    def __getattr__(self, name: str) -> Any:
        # copy, pickle and abc probe dunders, and during unpickling the instance
        # has no attributes yet: answering must not build a logger.
        if name.startswith("__") or name in ("_factory_args", "_initial_values"):
            raise AttributeError(name)
        return getattr(self._resolve(), name)


# suppress_asyncio_noise() windows can overlap (two backends stopping at once,
# on one loop or on two threads) and end in any order, so the level is saved
# by the first one in and restored by the last one out.
_asyncio_noise_lock = threading.Lock()
_asyncio_noise_depth = 0
_asyncio_noise_saved_level = 0


@contextmanager
def suppress_asyncio_noise():
    """Suppress asyncio 'Loop is closed' warnings during event loop shutdown.

    nodriver's subprocess handlers fire asyncio WARNING/ERROR messages when
    the event loop closes. This context manager temporarily raises the asyncio
    logger level to CRITICAL to suppress this harmless cleanup noise.

    Overlapping uses, from coroutines or threads, share one suppression: the
    first saves the logger's level and the last restores it, whatever order
    they end in.

    Note: This suppresses ALL asyncio log messages below CRITICAL for the
    duration of the context, process-wide. The suppression window should be
    kept as small as possible (just the asyncio.run() call).
    """
    global _asyncio_noise_depth, _asyncio_noise_saved_level  # noqa: PLW0603
    asyncio_logger = stdlib_logging.getLogger("asyncio")
    with _asyncio_noise_lock:
        if _asyncio_noise_depth == 0:
            _asyncio_noise_saved_level = asyncio_logger.level
            asyncio_logger.setLevel(stdlib_logging.CRITICAL)
        _asyncio_noise_depth += 1
    try:
        yield
    finally:
        with _asyncio_noise_lock:
            _asyncio_noise_depth -= 1
            if _asyncio_noise_depth == 0:
                asyncio_logger.setLevel(_asyncio_noise_saved_level)


def enable_network_debug() -> None:
    """Enable deep network-level debug logging.

    Turns on wire-level HTTP tracing for debugging request/response cycles:
    - http.client: HTTPConnection.debuglevel = 1 (prints raw HTTP traffic)
    - urllib3: DEBUG level (connection pool lifecycle, retries)
    - httpx: DEBUG level (request/response flow)
    - httpcore: DEBUG level (low-level HTTP transport)
    """
    import http.client

    http.client.HTTPConnection.debuglevel = 1

    # Ensure the root stdlib logger can emit DEBUG messages from these
    # network libraries. Without a handler, propagated messages are dropped.
    root = stdlib_logging.getLogger()
    if not root.handlers:
        handler = stdlib_logging.StreamHandler(sys.stderr)
        handler.setFormatter(stdlib_logging.Formatter("%(name)s %(levelname)s: %(message)s"))
        root.addHandler(handler)
    root.setLevel(stdlib_logging.DEBUG)

    network_loggers = ("urllib3", "httpx", "httpcore")
    for logger_name in network_loggers:
        log = stdlib_logging.getLogger(logger_name)
        log.setLevel(stdlib_logging.DEBUG)
        log.propagate = True

    get_logger(__name__).info(
        "network_debug_enabled",
        loggers=network_loggers,
        http_client_debuglevel=1,
    )


def get_logger(name: str | None = None) -> GraftpunkLogger:
    """A logger for graftpunk's own lines; see :class:`GraftpunkLogger`.

    Args:
        name: Passed to the logger factory, as ``structlog.get_logger(name)`` does.
            Defaults to the calling module's ``__name__``: the factory runs at
            log time, when the nearest frame outside structlog is graftpunk's,
            so a factory that names loggers after their caller (the stdlib
            ``LoggerFactory``) cannot find the module itself.
    """
    if name is None:
        name = sys._getframe(1).f_globals.get("__name__")
    return GraftpunkLogger(() if name is None else (name,), {})
