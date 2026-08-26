"""Structured logging configuration using structlog."""

import os
import sys
from contextlib import contextmanager
from typing import Any

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
    return sys.stderr.isatty()


def configured_by_consumer() -> bool:
    """True when structlog carries a configuration graftpunk did not install."""
    if not structlog.is_configured():
        return False
    return structlog.get_config().get("logger_factory") is not _owned_factory


def configure_logging(
    level: str = "WARNING",
    json_output: bool = False,
) -> None:
    """Configure structlog for graftpunk.

    Args:
        level: Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL).
        json_output: If True, output JSON format. If False, use console-friendly format.

    Console output is coloured only when stderr is a TTY (or FORCE_COLOR is set)
    and NO_COLOR is unset.
    """
    global _owned_factory
    processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
    ]

    if json_output:
        processors.append(structlog.processors.JSONRenderer())
    else:
        processors.append(structlog.dev.ConsoleRenderer(colors=_stderr_wants_color()))

    import logging as stdlib_logging

    _owned_factory = structlog.PrintLoggerFactory(file=sys.stderr)
    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(stdlib_logging, level.upper(), stdlib_logging.INFO)
        ),
        context_class=dict,
        logger_factory=_owned_factory,
        cache_logger_on_first_use=False,
    )


def ensure_library_defaults() -> None:
    """Give library consumers a quiet, stderr-only logger without touching theirs.

    Only the ``gp`` CLI calls :func:`configure_logging`. A program that merely
    imports graftpunk would otherwise get structlog's built-in defaults — a
    ``PrintLogger`` on **stdout** with no level filter — so a stray debug line could
    corrupt machine-readable output (#163). If the consumer has already configured
    structlog, their configuration is left exactly as it is.

    Note: this marks structlog as configured, so a consumer's later
    ``structlog.configure_once()`` is a no-op; call ``structlog.configure()`` to
    replace the default, or configure before importing graftpunk. It runs once,
    at import: a later ``structlog.reset_defaults()`` restores structlog's stdout
    builtins and graftpunk does not re-arm the default.
    """
    if not structlog.is_configured():
        configure_logging()


ensure_library_defaults()


@contextmanager
def suppress_asyncio_noise():
    """Suppress asyncio 'Loop is closed' warnings during event loop shutdown.

    nodriver's subprocess handlers fire asyncio WARNING/ERROR messages when
    the event loop closes. This context manager temporarily raises the asyncio
    logger level to CRITICAL to suppress this harmless cleanup noise.

    Note: This suppresses ALL asyncio log messages below CRITICAL for the
    duration of the context. The suppression window should be kept as small
    as possible (just the asyncio.run() call).
    """
    import logging

    asyncio_logger = logging.getLogger("asyncio")
    prev_level = asyncio_logger.level
    asyncio_logger.setLevel(logging.CRITICAL)
    try:
        yield
    finally:
        asyncio_logger.setLevel(prev_level)


def enable_network_debug() -> None:
    """Enable deep network-level debug logging.

    Turns on wire-level HTTP tracing for debugging request/response cycles:
    - http.client: HTTPConnection.debuglevel = 1 (prints raw HTTP traffic)
    - urllib3: DEBUG level (connection pool lifecycle, retries)
    - httpx: DEBUG level (request/response flow)
    - httpcore: DEBUG level (low-level HTTP transport)
    """
    import http.client
    import logging

    http.client.HTTPConnection.debuglevel = 1

    # Ensure the root stdlib logger can emit DEBUG messages from these
    # network libraries. Without a handler, propagated messages are dropped.
    root = logging.getLogger()
    if not root.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("%(name)s %(levelname)s: %(message)s"))
        root.addHandler(handler)
    root.setLevel(logging.DEBUG)

    network_loggers = ("urllib3", "httpx", "httpcore")
    for logger_name in network_loggers:
        log = logging.getLogger(logger_name)
        log.setLevel(logging.DEBUG)
        log.propagate = True

    structlog.get_logger().info(
        "network_debug_enabled",
        loggers=network_loggers,
        http_client_debuglevel=1,
    )


def get_logger(name: str | None = None) -> structlog.BoundLogger:
    """Get a configured logger instance.

    Args:
        name: Optional logger name. If not provided, uses the calling module's name.

    Returns:
        Configured structlog logger.
    """
    return structlog.get_logger(name)
