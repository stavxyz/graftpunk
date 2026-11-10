"""Structured logging using structlog, without configuring structlog for the program.

graftpunk's modules log through :func:`get_logger`. Importing graftpunk changes
nothing in structlog: a program's own loggers keep structlog's defaults until the
program configures them. Only the ``gp`` CLI, which owns its process, calls
:func:`configure_logging`.
"""

from __future__ import annotations

import datetime
import decimal
import enum
import fractions
import logging as stdlib_logging
import os
import pathlib
import sys
import threading
import traceback
import uuid
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, TextIO

import structlog
import structlog._native as _structlog_native
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


# Values no log line may show while a command that holds them runs: the
# command's secret options (see :func:`log_secrets`). Process-wide rather than
# per context, so a line logged from any thread (a worker pool, the nodriver
# loop thread, an asyncio task created before the scope) is scrubbed too. A
# count per value, so nested and concurrent scopes each release only their own.
_active_secrets: Counter[str] = Counter()
_active_secrets_lock = threading.Lock()


def _reset_lock_in_child() -> None:
    """A forked child gets a fresh lock: another thread may have held the
    parent's at the fork, and nothing in the child would ever release it."""
    global _active_secrets_lock
    _active_secrets_lock = threading.Lock()


if hasattr(os, "register_at_fork"):  # POSIX only
    os.register_at_fork(after_in_child=_reset_lock_in_child)

# What structlog's filtering bound logger hands out for a level it drops.
# Looked up, not imported: should structlog rename them, every line is scrubbed.
_DROPPED_LINE_METHODS = tuple(
    method
    for method in (
        getattr(_structlog_native, "_nop", None),
        getattr(_structlog_native, "_anop", None),
    )
    if method is not None
)


@contextmanager
def log_secrets(secrets: Iterable[str]) -> Iterator[None]:
    """Scrub *secrets* from every line a :class:`GraftpunkLogger` writes while
    the block runs, from any thread, whatever structlog configuration renders
    it: every value of the line, in the forms the observe run's scrub covers,
    its traceback included. A value leaves the scrub when the last block holding
    it closes. Values shorter than the observe scrub's floor (three characters)
    are left alone, as there."""
    added = [s for s in secrets if s]
    with _active_secrets_lock:
        _active_secrets.update(added)
    try:
        yield
    finally:
        with _active_secrets_lock:
            _active_secrets.subtract(added)
            for value in added:
                if _active_secrets[value] <= 0:
                    del _active_secrets[value]


def _current_secrets() -> tuple[str, ...]:
    if not _active_secrets:  # the common case, read without the lock
        return ()
    with _active_secrets_lock:
        return tuple(_active_secrets)


# The logging methods of a structlog bound logger, and their async forms ("a" + name).
_LOG_METHODS = frozenset(
    {
        "debug",
        "info",
        "warning",
        "warn",
        "error",
        "err",
        "critical",
        "fatal",
        "failure",
        "exception",
        "msg",
        "log",
    }
)


# How deep the scrub and the redacted_values rewrite walk nested containers:
# past it the scrub writes the rest as its scrubbed repr, the rewrite as _TOO_DEEP.
_MAX_SCRUB_DEPTH = 64


def _unprintable(value: Any) -> str:
    """The fixed stand-in for a value that has no text at all: never the value
    itself, which a renderer could neither print nor be trusted to scrub."""
    return f"<unprintable {type(value).__name__}>"


def _texts(value: Any) -> list[str]:
    """``str(value)`` and ``repr(value)``, leaving out either one that raises."""
    texts = []
    for render in (str, repr):
        try:
            texts.append(render(value))
        except Exception:  # noqa: BLE001, S112 - inside the logger: logging it would recurse
            continue
    return texts


class _ReadText:
    """What a value with clean ``str`` and ``repr`` and no ``__structlog__``
    method is logged as inside a secret scope: its texts as the scrub read them.
    The value itself could change before the renderer reads it, for one through
    a later ``__structlog__`` call in the same line."""

    def __init__(self, text: str, representation: str) -> None:
        self._text = text
        self._representation = representation

    def __str__(self) -> str:
        return self._text

    def __repr__(self) -> str:
        return self._representation


class _FrozenInt(int):
    """An int subclass's number with the texts the scrub read (#372): ``%d``
    and JSON take the number, ``str`` and ``repr`` the texts, and neither can
    change before the renderer reads it."""

    _text: str
    _representation: str

    def __new__(cls, number: int, text: str, representation: str) -> _FrozenInt:
        made = super().__new__(cls, number)
        made._text = text
        made._representation = representation
        return made

    def __str__(self) -> str:
        return self._text

    def __repr__(self) -> str:
        return self._representation


class _FrozenFloat(float):
    """:class:`_FrozenInt` for a float subclass."""

    _text: str
    _representation: str

    def __new__(cls, number: float, text: str, representation: str) -> _FrozenFloat:
        made = super().__new__(cls, number)
        made._text = text
        made._representation = representation
        return made

    def __str__(self) -> str:
        return self._text

    def __repr__(self) -> str:
        return self._representation


class _StructlogValue(_ReadText):
    """What a value with a ``__structlog__`` method and clean ``str`` and
    ``repr`` is logged as: the method's return, scrubbed, for a renderer that
    calls the method (structlog's ``JSONRenderer``), and the value's own texts
    for one that prints ``str`` or ``repr``. The renderer never calls the
    value's own method, whose next return could differ from the one scrubbed."""

    def __init__(self, structlog_value: Any, text: str, representation: str) -> None:
        super().__init__(text, representation)
        self._structlog_value = structlog_value

    def __structlog__(self) -> Any:
        return self._structlog_value


# Values whose texts cannot change, kept as they are when clean: a renderer
# writes an int or a float (an IntEnum too) natively, ``%d`` formats them, and a
# program's serializer may know the others.
_FIXED_TYPES = frozenset(
    {
        complex,
        decimal.Decimal,
        fractions.Fraction,
        datetime.date,
        datetime.datetime,
        datetime.time,
        datetime.timedelta,
        datetime.timezone,
        uuid.UUID,
        pathlib.PurePosixPath,
        pathlib.PureWindowsPath,
        pathlib.PosixPath,
        pathlib.WindowsPath,
    }
)


def _key(key: Any) -> Any:
    """A rebuilt dict key, or its text when the rebuild is not hashable: a
    hashable list subclass is rebuilt as a plain list."""
    try:
        hash(key)
    except Exception:  # noqa: BLE001 - unhashable, or a __hash__ that raises
        try:
            return repr(key)
        except Exception:  # noqa: BLE001 - inside the logger: logging it would recurse
            return _unprintable(key)
    return key


def _json_key(key: Any) -> Any:
    """A scrubbed dict key as JSON can write it: a string, number, bool or None
    as it is, and any other key (a tuple, an object with ``__structlog__``) as
    its text. structlog's JSONRenderer raises on such a key out of the log
    call, and calls no ``__structlog__`` or default for a key (#371)."""
    if key is None or isinstance(key, str | int | float | bool):
        return key
    texts = _texts(key)
    return texts[0] if texts else _unprintable(key)


# How many values one log line's scrub (or redacted_values rewrite) visits
# before it writes each container it has not yet entered as _TOO_MANY. A
# container shared at every level whose innermost list holds the outermost has
# a path count exponential in its depth, and no cache bounds that (#305).
_MAX_LINE_VALUES = 10_000
_TOO_MANY = "<too many values>"
_TOO_DEEP = "<nested too deep>"
_NO_LOOP = sys.maxsize

# What a container that holds itself is written as where the loop closes, as
# Python's repr writes it.
_LOOP_MARKS: tuple[tuple[type, str], ...] = ((dict, "{...}"), (list, "[...]"), (tuple, "(...)"))


def _loop_mark(value: Any) -> str:
    return next((mark for kind, mark in _LOOP_MARKS if isinstance(value, kind)), "...")


def _rebuilt(value: list[Any] | tuple[Any, ...], members: list[Any]) -> Any:
    """*members* in a plain list or tuple, or in *value*'s own type for a
    namedtuple whose constructor takes them."""
    if isinstance(value, list):
        return members
    try:
        if hasattr(value, "_fields"):
            return type(value)(*members)
    except Exception:  # noqa: BLE001, S110 - a namedtuple subclass with its own constructor
        pass
    return tuple(members)


def _repr_cost(value: Any, limit: int) -> int | None:
    """How many values ``repr(value)`` writes, following lists, tuples and dicts
    as repr does (a container it is already inside is written ``[...]``), or None
    when that is more than *limit* or the count fails."""
    count = 0
    inside: set[int] = set()
    stack: list[tuple[Any, bool]] = [(value, False)]
    try:
        while stack:
            item, leaving = stack.pop()
            if leaving:
                inside.discard(id(item))
                continue
            count += 1
            if not isinstance(item, dict | list | tuple) or id(item) in inside:
                continue
            members: list[Any] = (
                [part for pair in dict.items(item) for part in pair]
                if isinstance(item, dict)
                else list(list.__iter__(item) if isinstance(item, list) else tuple.__iter__(item))
            )
            if count + len(members) > limit:
                return None
            inside.add(id(item))
            stack.append((item, True))
            stack.extend((member, False) for member in members)
    except Exception:  # noqa: BLE001 - inside the logger: logging it would recurse
        return None
    return count if count <= limit else None


class _LineWalk:
    """The walk :class:`_LineScrub` and :class:`_LineRewrite` share over one
    log line's values: dicts, lists and tuples (their subclasses too) are
    rebuilt as plain ones, a namedtuple as its own type when its constructor
    takes its members; it never raises.

    A container is walked once for each depth it sits at, however many paths
    reach it there: ``l = [l, l]`` nested 22 levels has four million paths to
    its innermost list and 23 lists. Its result is kept with the container
    itself, so the ``id`` it is kept by cannot pass to another object during the
    line. A result is not kept when a loop closed inside it on a container
    above it, whose place depends on the path: a loop that closes on the
    container whose member it is (``l.append(l)``) closes there from any path,
    so it does not count.

    Past ``_MAX_SCRUB_DEPTH`` levels a container is written as the subclass's
    ``_too_deep``, and past ``_MAX_LINE_VALUES`` values each container not yet
    entered is written as ``_TOO_MANY``; the outermost one is always entered.
    """

    def __init__(self) -> None:
        # The depth of each container (or __structlog__ value) being walked.
        self._open: dict[int, int] = {}
        self._done: dict[tuple[int, int], tuple[Any, Any]] = {}
        # The shallowest open container a loop closed on, since the walk
        # entered the current container, not counting its own member loops.
        self._loop_depth = _NO_LOOP
        self._values = 0

    def _loop(self, value: Any) -> Any:
        raise NotImplementedError

    def _too_deep(self, value: Any) -> Any:
        raise NotImplementedError

    def _members(self, value: Any, depth: int) -> Any:
        raise NotImplementedError

    def _closes_loop(self, value: Any, depth: int) -> bool:
        """Whether *value*, met at *depth*, is being walked already."""
        opened = self._open.get(id(value))
        if opened is None:
            return False
        if opened != depth - 1:
            self._loop_depth = min(self._loop_depth, opened)
        return True

    def _container(self, value: Any, depth: int) -> Any:
        if self._closes_loop(value, depth):
            return self._loop(value)
        if depth >= _MAX_SCRUB_DEPTH:
            return self._too_deep(value)
        key = (id(value), depth)
        done = self._done.get(key)
        if done is not None:
            return done[1]
        if depth and self._values >= _MAX_LINE_VALUES:
            return _TOO_MANY
        outer, self._loop_depth = self._loop_depth, _NO_LOOP
        self._open[id(value)] = depth
        try:
            result = self._members(value, depth + 1)
        except Exception:  # noqa: BLE001 - a subclass whose iteration raises
            result = _unprintable(value)
        finally:
            del self._open[id(value)]
        if self._loop_depth > depth:
            self._done[key] = (value, result)
        self._loop_depth = min(outer, self._loop_depth)
        return result


class _LineScrub(_LineWalk):
    """Every secret replaced in one log line's values, whatever their types; it
    never raises.

    Strings are scrubbed; dicts (keys too, two that collide both kept), lists
    and tuples are rebuilt with scrubbed members (see :class:`_LineWalk`). A
    container that holds itself, where the loop closes, and one nested past
    ``_MAX_SCRUB_DEPTH``, are logged as their scrubbed ``repr`` when that costs
    no more than the line's remaining values, else as a fixed placeholder. Any
    other value (an exception, a ``Path``, ``bytes`` decoded with replacement, a
    set, an object) is logged as its text, scrubbed, when that text holds a
    secret. One whose texts are clean is logged as what the scrub read: an int,
    a float, ``bytes`` and the types in ``_FIXED_TYPES`` as they are, one whose
    ``__structlog__`` method returns as :class:`_StructlogValue`, one whose
    method raises as a :class:`_StructlogValue` carrying a fixed placeholder,
    and any other as :class:`_ReadText`. A value whose ``str`` and ``repr``
    both raise, or whose type cannot be checked, is logged as a fixed
    placeholder. A dict key rebuilt as an unhashable value is logged as its
    text.
    """

    def __init__(self, redact: Callable[[str], str], *, plain_numbers: bool = False) -> None:
        super().__init__()
        self._redact = redact
        # Every int or float subclass as its plain number, as JSON writes it.
        self._plain_numbers = plain_numbers
        self._reprs: dict[int, tuple[Any, str]] = {}

    def text(self, text: str) -> str:
        return self._redact(text)

    def _as_text(self, value: Any, placeholder: str) -> str:
        """*value*'s ``repr``, scrubbed, for a container the walk does not enter,
        or *placeholder* when that repr writes more values than the line has
        left. Python's repr follows every path, so a shared container's repr can
        cost far more than its walk."""
        known = self._reprs.get(id(value))
        if known is not None:
            return known[1]
        cost = _repr_cost(value, _MAX_LINE_VALUES - self._values)
        if cost is None:
            self._values = _MAX_LINE_VALUES
            text = placeholder
        else:
            self._values += cost
            try:
                text = self.text(repr(value))
            except Exception:  # noqa: BLE001 - too deep for repr too
                text = _unprintable(value)
        self._reprs[id(value)] = (value, text)
        return text

    def _loop(self, value: Any) -> str:
        return self._as_text(value, _loop_mark(value))

    def _too_deep(self, value: Any) -> str:
        return self._as_text(value, _TOO_MANY)

    def _members(self, value: Any, depth: int) -> Any:
        if isinstance(value, dict):
            result: dict[Any, Any] = {}
            for key, item in value.items():
                scrubbed_key = _json_key(self(key, depth))
                # Two keys that scrub to the same text both stay, the later one numbered.
                candidate, number = scrubbed_key, 1
                while candidate in result:
                    number += 1
                    candidate = f"{scrubbed_key} ({number})"
                result[candidate] = self(item, depth)
            return result
        return _rebuilt(value, [self(member, depth) for member in value])

    def _other(self, value: Any, depth: int) -> Any:
        if isinstance(value, bytes | bytearray):
            text = bytes(value).decode("utf-8", errors="replace")
            scrubbed = self.text(text)
            if scrubbed != text:
                return scrubbed
            # A copy of a bytearray, as read: a later member could change it.
            return bytearray(value) if isinstance(value, bytearray) else value
        texts = _texts(value)
        if not texts:
            return _unprintable(value)
        if len(texts) == 1:
            # One of str and repr raises; the renderer would hit it (it prints a
            # repr), so the line carries the text that works.
            return self.text(texts[0])
        for text in texts:
            scrubbed = self.text(text)
            if scrubbed != text:
                return scrubbed
        if type(value) in (int, float) or type(value) in _FIXED_TYPES:
            return value
        if isinstance(value, enum.Enum):
            # A member's texts read its fixed name and value.
            return value
        if isinstance(value, int | float):
            # A subclass's str or repr may read state a later __structlog__ in
            # this line changes (#372). The plain number keeps %d and a native
            # JSON number; one whose str is not the plain number's keeps them
            # too, printing the texts the scrub read.
            if isinstance(value, int):
                whole = int.__int__(value)
                if self._plain_numbers or texts[0] == str(whole):
                    return whole
                return _FrozenInt(whole, texts[0], texts[1])
            real = float.__float__(value)
            if self._plain_numbers or texts[0] == str(real):
                return real
            return _FrozenFloat(real, texts[0], texts[1])
        if self._closes_loop(value, depth):
            # Its own __structlog__ return led back to it: its repr ends the loop.
            return texts[1]
        try:
            returned = value.__structlog__()
        except AttributeError:
            # No such method, as structlog's JSONRenderer reads it.
            return _ReadText(texts[0], texts[1])
        except Exception:  # noqa: BLE001 - the renderer's call could raise, or return a secret
            return _StructlogValue(_unprintable(value), texts[0], texts[1])
        if depth >= _MAX_SCRUB_DEPTH:
            structlog_value: Any = self._as_text(returned, _TOO_MANY)
        else:
            self._open[id(value)] = depth
            try:
                structlog_value = self(returned, depth + 1)
            finally:
                del self._open[id(value)]
        return _StructlogValue(structlog_value, texts[0], texts[1])

    def __call__(self, value: Any, depth: int = 0) -> Any:
        self._values += 1
        try:
            if value is None or isinstance(value, bool):
                return value
            if isinstance(value, str):
                return self.text(value)
            if isinstance(value, dict | list | tuple):
                return self._container(value, depth)
            return self._other(value, depth)
        except Exception:  # noqa: BLE001 - a type check that raises (a __class__ property)
            return _unprintable(value)


def _scrubbing_call(logger: GraftpunkLogger, name: str, secrets: tuple[str, ...]) -> Any:
    """*logger*'s log method *name*, scrubbing its bound values and every
    argument of *secrets*.

    A traceback (``exception()``, or ``exc_info``) is formatted here into the
    ``exception`` value, as structlog's ``format_exc_info`` would, so it is
    scrubbed before any renderer sees it; the line goes out at error level.
    """
    from graftpunk.observe.run import text_redactor

    is_async = name.startswith("a") and name[1:] in _LOG_METHODS
    base = name[1:] if is_async else name

    def call(*args: Any, **kwargs: Any) -> Any:
        target = ("aerror" if is_async else "error") if base == "exception" else name
        plain = logger._resolve()
        method = getattr(plain, target)
        if getattr(method, "__func__", method) in _DROPPED_LINE_METHODS:
            # structlog's level filter drops this line: nothing is rendered, so
            # nothing needs scrubbing.
            return getattr(plain, name)(*args, **kwargs)
        scrub = _LineScrub(text_redactor(secrets))
        # A fresh logger with scrubbed bound values: a module's LOG is shared.
        bound = logger._with(scrub(logger._initial_values))._resolve()
        exc_info = kwargs.pop("exc_info", base == "exception" or None)
        target = name
        if exc_info:
            if isinstance(exc_info, BaseException):
                exc_info = (type(exc_info), exc_info, exc_info.__traceback__)
            elif not isinstance(exc_info, tuple):
                exc_info = sys.exc_info()
            if exc_info[0] is not None:
                kwargs["exception"] = "".join(traceback.format_exception(*exc_info)).rstrip("\n")
            if base == "exception":
                target = "aerror" if is_async else "error"
        return getattr(bound, target)(*scrub(_interpolated(args)), **scrub(kwargs))

    return call


def _interpolated(args: tuple[Any, ...]) -> tuple[Any, ...]:
    """``(event, *positional)`` as the one formatted message, as structlog's
    bound loggers interpolate it (a single non-empty mapping by name), so the
    scrub reads the message as it will print.

    Scrubbing the arguments one by one turned a number whose digits hold a
    secret into a string, and ``%d`` then raised ``TypeError`` out of the log
    call. A format that fails here is left to the logger, as before.
    """
    if len(args) < 2 or not isinstance(args[0], str):
        return args
    event, positional = args[0], args[1:]
    try:
        if len(positional) == 1 and isinstance(positional[0], Mapping) and positional[0]:
            return (event % positional[0],)
        return (event % positional,)
    except Exception:  # noqa: BLE001 - the logger reports or raises as it did
        return args


def _unchanged(text: str) -> str:
    return text


class _SafeRenderer:
    """A structlog renderer that never raises out of the log call: gp's JSON
    and console formats each wrap theirs in one.

    A line the renderer cannot write is written again after the scrub's walk
    with nothing to redact. The walk makes a key that is not a string, number,
    bool or None its text (#371), a container that holds itself its repr, and
    a value whose ``str`` and ``repr`` both raise (one whose ``__repr__``
    raises, an int past Python's 4300-digit limit) a fixed placeholder. If that
    fails too, the line is a fixed event carrying the level and timestamp. A
    line the renderer can write is written as before.
    """

    def __init__(
        self, render: Callable[[WrappedLogger, str, EventDict], Any], *, json: bool
    ) -> None:
        self._render = render
        # JSON writes an int or float subclass as its number; the walk does too.
        self._plain_numbers = json

    def __call__(self, logger: WrappedLogger, method_name: str, event_dict: EventDict) -> Any:
        try:
            return self._render(logger, method_name, dict(event_dict))
        except Exception:  # noqa: BLE001, S110 - a log call must not raise
            pass
        try:
            walked = _LineScrub(_unchanged, plain_numbers=self._plain_numbers)(event_dict)
            return self._render(logger, method_name, walked)
        except Exception:  # noqa: BLE001 - a log call must not raise
            return self._render(logger, method_name, _last_resort(event_dict))


_UNRENDERABLE = "<unrenderable log line>"


def _last_resort(event_dict: EventDict) -> EventDict:
    """The line when nothing else renders: a fixed event, and the level and
    timestamp when they are strings (add_log_level and TimeStamper write
    them so)."""
    line: dict[str, Any] = {"event": _UNRENDERABLE}
    for key in ("level", "timestamp"):
        value = event_dict.get(key)
        if type(value) is str:
            line[key] = value
    return line


def _processors(json_output: bool, colors: bool) -> list[Any]:
    # ConsoleRenderer formats exc_info itself and warns when format_exc_info
    # ran before it (structlog 21.2.0 and later), so only JSON output gets it.
    # plain_traceback keeps the console traceback as format_exc_info wrote it.
    tail: list[Any] = (
        [
            structlog.processors.format_exc_info,
            _SafeRenderer(structlog.processors.JSONRenderer(), json=True),
        ]
        if json_output
        else [
            _SafeRenderer(
                structlog.dev.ConsoleRenderer(
                    colors=colors, exception_formatter=structlog.dev.plain_traceback
                ),
                json=False,
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
        values = _redact(self._initial_values) if _redactions.get() else self._initial_values
        if structlog.is_configured():
            # As in _quiet_logger: get_logger's keyword parameters are not context.
            return structlog.get_logger(*self._factory_args).bind(**values)
        return _quiet_logger(values)

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
        is_log_method = name in _LOG_METHODS or name[1:] in _LOG_METHODS
        secrets = _current_secrets()
        if secrets and is_log_method:
            method = _scrubbing_call(self, name, secrets)
        else:
            method = getattr(self._resolve(), name)
        # redacted_values() rewrites its pairs first; log_secrets() then
        # scrubs its values from what is left. Either may be active alone.
        if _redactions.get() and is_log_method:
            return _redacting_call(method)
        return method


# Values that must not reach a log line, each with what to write instead. Set
# only inside redacted_values(); a contextvar, so another thread's or task's
# lines are untouched.
_redactions: ContextVar[tuple[tuple[str, str], ...]] = ContextVar(
    "graftpunk_log_redactions", default=()
)


class _LineRewrite(_LineWalk):
    """One log line's values with each :func:`redacted_values` pair applied; it
    never raises.

    Strings are rewritten, inside dicts (keys too), lists and tuples (see
    :class:`_LineWalk`); any other value is kept as it is, and a dict key
    rebuilt as an unhashable value is written as its text. A container that
    holds itself is written where the loop closes as Python's repr writes it
    (``[...]``), and one nested past ``_MAX_SCRUB_DEPTH`` as ``_TOO_DEEP``:
    neither holds a value.
    """

    def __init__(self, pairs: tuple[tuple[str, str], ...]) -> None:
        super().__init__()
        self._pairs = pairs

    def _loop(self, value: Any) -> str:
        return _loop_mark(value)

    def _too_deep(self, value: Any) -> str:
        return _TOO_DEEP

    def _members(self, value: Any, depth: int) -> Any:
        if isinstance(value, dict):
            return {_key(self(key, depth)): self(item, depth) for key, item in value.items()}
        return _rebuilt(value, [self(item, depth) for item in value])

    def __call__(self, value: Any, depth: int = 0) -> Any:
        self._values += 1
        try:
            if isinstance(value, str):
                for secret, placeholder in self._pairs:
                    value = value.replace(secret, placeholder)
                return value
            if isinstance(value, dict | list | tuple):
                return self._container(value, depth)
        except Exception:  # noqa: BLE001 - a type check that raises (a __class__ property)
            return _unprintable(value)
        return value


def _redact(value: Any) -> Any:
    return _LineRewrite(_redactions.get())(value)


def _redacting_call(method: Any) -> Any:
    def call(*args: Any, **kwargs: Any) -> Any:
        rewrite = _LineRewrite(_redactions.get())
        return method(*rewrite(args), **rewrite(kwargs))

    return call


@contextmanager
def redacted_values(*pairs: tuple[str, str]) -> Iterator[None]:
    """Within the block, write each pair's first string as its second in graftpunk's log lines.

    Every string in a line's event, positional arguments, keyword values and
    bound context is rewritten, including inside lists, tuples and dicts (keys
    and values), so a path or an error message that holds the value is
    covered too. Matching is by substring: a longer string that contains the
    value has that part replaced. Only
    graftpunk's own loggers (:func:`get_logger`) are affected, and only in the
    current thread or task. An empty value is ignored.
    """
    kept = tuple((secret, placeholder) for secret, placeholder in pairs if secret)
    token = _redactions.set(_redactions.get() + kept)
    try:
        yield
    finally:
        _redactions.reset(token)


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
