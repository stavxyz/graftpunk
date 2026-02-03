"""Structured logging configuration using structlog."""

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


def configure_logging(
    level: str = "WARNING",
    json_output: bool = False,
) -> None:
    """Configure structlog for graftpunk.

    Args:
        level: Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL).
        json_output: If True, output JSON format. If False, use console-friendly format.
    """
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
        processors.append(structlog.dev.ConsoleRenderer(colors=True))

    import logging as stdlib_logging

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(stdlib_logging, level.upper(), stdlib_logging.INFO)
        ),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=False,
    )


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


def get_logger(name: str | None = None) -> structlog.BoundLogger:
    """Get a configured logger instance.

    Args:
        name: Optional logger name. If not provided, uses the calling module's name.

    Returns:
        Configured structlog logger.
    """
    return structlog.get_logger(name)
