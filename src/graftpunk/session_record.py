"""The session record: a saved session's state dict as versioned JSON.

Every class graftpunk saves reduces itself to a state dict (``__getstate__``)
and restores from one (``__setstate__``). :data:`FIELDS` is the one table of
what a record stores from that dict: each row names a state key, where the
record keeps it, the session kinds it applies to, and its codec.
:data:`PICKLE_ONLY` names the state keys a record leaves out because they hold
arbitrary objects, each with a test that its value is the library default
(which :func:`record_to_state` puts back) and a factory for that default.

:func:`state_to_record` refuses a state it cannot store whole, by naming the
key, so the caller keeps writing the session as a pickle. :func:`classify` is
the one entry point for stored bytes: it tells a record from a legacy pickle
by the first byte, and validates a record completely before anything reads it.

Nothing here imports a session class, selenium, requestium, nodriver or httpie.
"""

from __future__ import annotations

import copy
import json
import math
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from http import cookiejar
from typing import Any

import requests.adapters
from requests.cookies import RequestsCookieJar
from requests.structures import CaseInsensitiveDict

from graftpunk.cookies import _STDLIB_DEFAULT_POLICY, BrowserCookiePolicy, _policy_is_default
from graftpunk.exceptions import InvalidSessionRecord, NewerSessionRecord
from graftpunk.session_riders import (
    _CACHE_ATTR,
    _HEADER_ROLE_ORIGINS_ATTR,
    _HEADER_ROLES_ATTR,
    GP_ACCOUNT_ATTR,
    SESSION_CLASS_PATHS,
    CachedToken,
)

RECORD_VERSION = 1

_VERSION_KEY = "graftpunk_session"
_KIND_KEY = "kind"
_KINDS: frozenset[str] = frozenset(SESSION_CLASS_PATHS.values())
_BROWSER: frozenset[str] = frozenset({"browser"})
_HTTP: frozenset[str] = frozenset({"api", "requests"})

_SELENIUM_REASON = "selenium sessions are stored as pickles in this release"

# How many objects and arrays deep a record may nest. Readers walk a record
# recursively (copy.deepcopy, comparisons), and a fixed limit far below the
# interpreter's recursion limit makes "too deep" the same answer wherever the
# call is made from.
MAX_DEPTH = 100


@dataclass(frozen=True)
class Field:
    """One state key a record stores.

    ``encode`` turns the state value into JSON data and may raise
    ``ValueError`` or ``TypeError`` to refuse it; ``decode`` turns the JSON
    data back into the state value and raises either for data that is not a
    valid value. A row that is not ``required`` may be absent from a state (a
    rider an older release did not have) and is then absent from the record.
    """

    state_key: str
    record_path: tuple[str, ...]
    kinds: frozenset[str]
    encode: Callable[[Any], Any]
    decode: Callable[[Any], Any]
    required: bool = True


@dataclass(frozen=True)
class PickleOnly:
    """One state key a record leaves out: only its default value can be dropped.

    ``at_default`` is a structural test on the value itself; ``default`` makes
    the value :func:`record_to_state` puts back.
    """

    state_key: str
    kinds: frozenset[str]
    at_default: Callable[[Any], bool]
    default: Callable[[], Any]


@dataclass(frozen=True)
class Record:
    """A validated session record.

    ``fields`` maps each stored state key to its JSON data as the record holds
    it (decoded by :func:`record_to_state`); ``extra`` holds the record's
    unknown top-level keys, untouched, so a write-back can keep them.
    """

    version: int
    kind: str
    fields: dict[str, Any]
    extra: dict[str, Any]


@dataclass(frozen=True)
class LegacyBytes:
    """Stored bytes that are not a record: a legacy pickle, not yet unpickled."""

    data: bytes


@dataclass(frozen=True)
class Unrecordable:
    """Why a state cannot be stored as a record: the key, and the reason."""

    key: str
    reason: str


# (encode, decode)
_Codec = tuple[Callable[[Any], Any], Callable[[Any], Any]]


# Plain JSON values


def _plain(check: Callable[[Any], bool], what: str) -> _Codec:
    """A codec for a value JSON holds as it is, of the shape *check* accepts."""

    def decode(data: Any) -> Any:
        if not check(data):
            raise ValueError(f"not {what}")
        return data

    def encode(value: Any) -> Any:
        # A tuple, a non-string key or a NaN would come back from JSON as
        # something else; the session keeps its pickle instead.
        if json.loads(json.dumps(value)) != value:
            raise ValueError("JSON would not give the value back unchanged")
        return decode(value)

    return encode, decode


def _is_bool(v: Any) -> bool:
    return type(v) is bool


def _is_int(v: Any) -> bool:
    return type(v) is int


def _is_str(v: Any) -> bool:
    return type(v) is str


def _is_optional_str(v: Any) -> bool:
    return v is None or type(v) is str


def _is_number(v: Any) -> bool:
    if type(v) not in (int, float):
        return False
    try:
        return math.isfinite(v)
    except OverflowError:  # an int too large for a float, which JSON can hold
        return False


def _is_str_map(v: Any, values: Callable[[Any], bool] = _is_str) -> bool:
    return type(v) is dict and all(type(k) is str and values(x) for k, x in v.items())


def _pair_codec(*, allow_str: bool, what: str) -> _Codec:
    """``None``, a ``(str, str)`` tuple (a JSON list) and, if *allow_str*, a str."""

    def encode(value: Any) -> Any:
        if value is None or (allow_str and type(value) is str):
            return value
        if type(value) is tuple and len(value) == 2 and all(type(v) is str for v in value):
            return list(value)
        raise ValueError(f"not {what}")

    def decode(data: Any) -> Any:
        if data is None or (allow_str and type(data) is str):
            return data
        if type(data) is list and len(data) == 2 and all(type(v) is str for v in data):
            return tuple(data)
        raise ValueError(f"not {what}")

    return encode, decode


# Headers: a list of [name, value] pairs, so order and case survive.


def _encode_headers(headers: Any) -> list[list[str]]:
    # A plain dict on nodriver, requests' CaseInsensitiveDict on the others.
    if not isinstance(headers, Mapping):
        raise TypeError(f"headers are a {type(headers).__name__}, not a mapping")
    pairs = [[name, value] for name, value in headers.items()]
    for name, value in pairs:
        if type(name) is not str or type(value) is not str:
            raise ValueError(f"header {name!r} is not a pair of strings")
    return pairs


def _header_pairs(data: Any) -> list[tuple[str, str]]:
    if type(data) is not list:
        raise ValueError("headers are not a list of pairs")
    pairs = []
    for pair in data:
        if type(pair) is not list or len(pair) != 2 or not all(type(s) is str for s in pair):
            raise ValueError(f"header {pair!r} is not a pair of strings")
        pairs.append((pair[0], pair[1]))
    return pairs


def _decode_headers(make: Callable[[list[tuple[str, str]]], Any]) -> Callable[[Any], Any]:
    def decode(data: Any) -> Any:
        pairs = _header_pairs(data)
        headers = make(pairs)
        if len(headers) != len(pairs):
            raise ValueError("a header name appears twice")
        return headers

    return decode


# Cookies: every http.cookiejar.Cookie constructor argument of each cookie.

_COOKIE_ARGS: dict[str, Callable[[Any], bool]] = {
    "version": lambda v: v is None or _is_int(v),
    "name": _is_str,
    "value": _is_optional_str,
    "port": _is_optional_str,
    "port_specified": _is_bool,
    "domain": _is_str,
    "domain_specified": _is_bool,
    "domain_initial_dot": _is_bool,
    "path": _is_str,
    "path_specified": _is_bool,
    "secure": _is_bool,
    "expires": lambda v: v is None or _is_number(v),
    "discard": _is_bool,
    "comment": _is_optional_str,
    # requests.cookies.morsel_to_cookie (what RequestsCookieJar.set does with
    # a Morsel) sets comment_url to a bool and HttpOnly in rest to a bool or
    # "", and a response or plugin can add such a cookie before a write-back.
    "comment_url": lambda v: _is_optional_str(v) or _is_bool(v),
    "rest": lambda v: _is_str_map(v, lambda x: _is_optional_str(x) or _is_bool(x)),
    "rfc2109": _is_bool,
}


def _encode_cookies(jar: Any) -> list[dict[str, Any]]:
    if not isinstance(jar, cookiejar.CookieJar):
        raise TypeError(f"cookies are a {type(jar).__name__}, not a cookie jar")
    # The record does not store the policy; each loader gives one by kind.
    policy = getattr(jar, "_policy", None)
    if not _policy_is_default(policy):
        raise ValueError(
            f"the jar's cookie policy ({type(policy).__qualname__}) is not the default"
        )
    return [
        {arg: dict(cookie._rest) if arg == "rest" else getattr(cookie, arg) for arg in _COOKIE_ARGS}
        for cookie in jar
    ]


def _decode_cookies(policy: Callable[[], cookiejar.CookiePolicy]) -> Callable[[Any], Any]:
    def decode(data: Any) -> RequestsCookieJar:
        if type(data) is not list:
            raise ValueError("cookies are not a list")
        jar = RequestsCookieJar(policy=policy())
        for entry in data:
            if type(entry) is not dict or set(entry) != set(_COOKIE_ARGS):
                raise ValueError(f"a cookie entry does not have exactly {sorted(_COOKIE_ARGS)}")
            for arg, check in _COOKIE_ARGS.items():
                if not check(entry[arg]):
                    raise ValueError(f"cookie {entry['name']!r} has an invalid {arg}")
            jar.set_cookie(cookiejar.Cookie(**entry))
        if len(jar) != len(data):
            raise ValueError("a cookie appears twice")
        return jar

    return decode


# The cached-token rider: token name -> CachedToken.

_TOKEN_ARGS: dict[str, Callable[[Any], bool]] = {
    "name": _is_str,
    "value": _is_str,
    "extracted_at": _is_number,
    "ttl": _is_number,
}


def _encode_tokens(tokens: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(tokens, Mapping):
        raise TypeError(f"cached tokens are a {type(tokens).__name__}, not a mapping")
    out = {}
    for key, token in tokens.items():
        if type(token) is not CachedToken:
            raise TypeError(f"cached token {key!r} is a {type(token).__name__}")
        out[key] = {arg: getattr(token, arg) for arg in _TOKEN_ARGS}
    return out


def _decode_tokens(data: Any) -> dict[str, CachedToken]:
    if type(data) is not dict:
        raise ValueError("cached tokens are not an object")
    tokens = {}
    for key, entry in data.items():
        if type(entry) is not dict or set(entry) != set(_TOKEN_ARGS):
            raise ValueError(f"cached token {key!r} does not have exactly {sorted(_TOKEN_ARGS)}")
        if not all(check(entry[arg]) for arg, check in _TOKEN_ARGS.items()):
            raise ValueError(f"cached token {key!r} has a value of the wrong type")
        tokens[key] = CachedToken(**entry)
    return tokens


FIELDS: tuple[Field, ...] = (
    Field(
        "_backend_type",
        ("backend_type",),
        _BROWSER,
        *_plain(lambda v: v == "nodriver", "the nodriver backend"),
    ),
    Field("_use_stealth", ("use_stealth",), _BROWSER, *_plain(_is_bool, "a bool")),
    Field("session_name", ("session_name",), _BROWSER, *_plain(_is_optional_str, "a string")),
    Field("current_url", ("current_url",), _BROWSER, *_plain(_is_str, "a string")),
    # The header shape each kind's __getstate__ writes and __setstate__ takes.
    Field("headers", ("headers",), _BROWSER, _encode_headers, _decode_headers(dict)),
    Field("headers", ("headers",), _HTTP, _encode_headers, _decode_headers(CaseInsensitiveDict)),
    # The policy each kind's loaders give a jar. The browser policy pickles as
    # the standard library's, so the api state a pickle holds carries that.
    Field(
        "cookies",
        ("cookies",),
        frozenset({"browser", "api"}),
        _encode_cookies,
        _decode_cookies(BrowserCookiePolicy),
    ),
    Field(
        "cookies",
        ("cookies",),
        frozenset({"requests"}),
        _encode_cookies,
        _decode_cookies(_STDLIB_DEFAULT_POLICY),
    ),
    # The pickled riders (session_riders.PICKLED_SESSION_RIDER_ATTRS). A state
    # written by an older release may lack a rider it did not have yet.
    Field(
        _CACHE_ATTR,
        ("riders", "cached_tokens"),
        _BROWSER,
        _encode_tokens,
        _decode_tokens,
        required=False,
    ),
    Field(
        _HEADER_ROLES_ATTR,
        ("riders", "header_roles"),
        _BROWSER,
        *_plain(lambda v: _is_str_map(v, _is_str_map), "a map of header maps"),
        required=False,
    ),
    Field(
        _HEADER_ROLE_ORIGINS_ATTR,
        ("riders", "header_role_origins"),
        _BROWSER,
        *_plain(_is_str_map, "a map of strings"),
        required=False,
    ),
    Field(
        GP_ACCOUNT_ATTR,
        ("riders", "account"),
        _BROWSER,
        *_plain(_is_optional_str, "a string"),
        required=False,
    ),
    Field("auth", ("auth",), _HTTP, *_pair_codec(allow_str=False, what="a (user, password) pair")),
    Field("params", ("requests", "params"), _HTTP, *_plain(lambda v: type(v) is dict, "an object")),
    Field(
        "verify",
        ("requests", "verify"),
        _HTTP,
        *_plain(lambda v: _is_bool(v) or _is_str(v), "a bool or a path"),
    ),
    Field(
        "proxies",
        ("requests", "proxies"),
        _HTTP,
        *_plain(lambda v: _is_str_map(v, _is_optional_str), "a map of strings"),
    ),
    Field(
        "cert",
        ("requests", "cert"),
        _HTTP,
        *_pair_codec(allow_str=True, what="a path or a (cert, key) pair"),
    ),
    Field("max_redirects", ("requests", "max_redirects"), _HTTP, *_plain(_is_int, "an int")),
    Field("trust_env", ("requests", "trust_env"), _HTTP, *_plain(_is_bool, "a bool")),
    Field("stream", ("requests", "stream"), _HTTP, *_plain(_is_bool, "a bool")),
)


# The pickle-only predicates read the value's structure and construct nothing
# to compare with: HTTPAdapter has no __eq__. A value they cannot inspect (a
# placeholder for a class that could not be imported, an attribute read that
# raises) is not at its default, so the session keeps its pickle.


def _hooks_at_default(hooks: Any) -> bool:
    try:
        return isinstance(hooks, Mapping) and list(hooks) == ["response"] and not hooks["response"]
    except Exception:  # noqa: BLE001 - uninspectable means not at default
        return False


_ADAPTER_PREFIXES = ("https://", "http://")


def _adapter_at_default(adapter: Any) -> bool:
    cls = type(adapter)
    if (cls.__module__, cls.__qualname__) != ("requests.adapters", "HTTPAdapter"):
        return False
    retries = adapter.max_retries
    # HTTPAdapter's default, Retry(0, read=False): no retry of any kind.
    zero_retry = (
        type(retries).__name__ == "Retry"
        and retries.total == 0
        and retries.read is False
        and retries.connect is None
        and retries.redirect is None
        and retries.status is None
        # Retry.other is new in urllib3 1.26; requests 2.28.0 allows 1.21.1
        # and later, and urllib3 1.25.11 has no such attribute.
        and getattr(retries, "other", None) is None
        and retries.backoff_factor == 0
        and not retries.status_forcelist
    )
    return (
        zero_retry
        and not adapter.config
        and adapter._pool_connections == requests.adapters.DEFAULT_POOLSIZE
        and adapter._pool_maxsize == requests.adapters.DEFAULT_POOLSIZE
        and adapter._pool_block == requests.adapters.DEFAULT_POOLBLOCK
    )


def _adapters_at_default(adapters: Any) -> bool:
    try:
        return (
            isinstance(adapters, Mapping)
            and sorted(adapters) == sorted(_ADAPTER_PREFIXES)
            and all(_adapter_at_default(adapters[prefix]) for prefix in _ADAPTER_PREFIXES)
        )
    except Exception:  # noqa: BLE001 - uninspectable means not at default
        return False


def _default_adapters() -> OrderedDict[str, Any]:
    # As requests.Session.__init__ mounts them.
    return OrderedDict((prefix, requests.adapters.HTTPAdapter()) for prefix in _ADAPTER_PREFIXES)


PICKLE_ONLY: tuple[PickleOnly, ...] = (
    PickleOnly("hooks", _HTTP, _hooks_at_default, lambda: {"response": []}),
    PickleOnly("adapters", _HTTP, _adapters_at_default, _default_adapters),
)


def _deeper_than(data: Any, limit: int) -> bool:
    """Whether JSON *data* nests objects and arrays more than *limit* deep.

    Iterative, so it answers for data too deep to walk recursively.
    """
    stack = [(data, 1)]
    while stack:
        node, depth = stack.pop()
        if type(node) is dict:
            children: Any = node.values()
        elif type(node) is list:
            children = node
        else:
            continue
        if depth > limit:
            return True
        stack.extend((child, depth + 1) for child in children)
    return False


def _refuse_constant(name: str) -> Any:
    raise ValueError(f"{name} is not a JSON number")


def _fields_of(kind: str) -> tuple[Field, ...]:
    return tuple(f for f in FIELDS if kind in f.kinds)


# Every top-level key the table owns, for any kind.
_TABLE_KEYS: frozenset[str] = frozenset(
    {_VERSION_KEY, _KIND_KEY} | {f.record_path[0] for f in FIELDS}
)


def _lookup(doc: Mapping[str, Any], path: tuple[str, ...]) -> tuple[bool, Any]:
    node: Any = doc
    for key in path:
        if key not in node:
            return False, None
        node = node[key]
    return True, node


def _check_layout(doc: Mapping[str, Any], kind: str) -> None:
    # A record of one version holds no table key it does not use: an unknown
    # key inside a block, or a key of another kind, means it is not a record
    # this release wrote or can read whole.
    rows = _fields_of(kind)
    own = {_VERSION_KEY, _KIND_KEY} | {f.record_path[0] for f in rows}
    for key in doc:
        if key in _TABLE_KEYS and key not in own:
            raise InvalidSessionRecord(f"{key!r} is not a key of a {kind} record")
    for block in {f.record_path[0] for f in rows if len(f.record_path) > 1}:
        if block not in doc:
            continue
        if type(doc[block]) is not dict:
            raise InvalidSessionRecord(f"{block!r} is not an object")
        known = {f.record_path[1] for f in rows if f.record_path[0] == block}
        if unknown := sorted(set(doc[block]) - known):
            raise InvalidSessionRecord(f"{block!r} holds unknown keys {unknown}")


def classify(decrypted: bytes) -> Record | LegacyBytes:
    """Tell a stored record from a legacy pickle, and validate a record whole.

    A payload whose first byte is ``{`` is a record (``[``, a JSON array, is
    an invalid one); anything else is returned as :class:`LegacyBytes` and
    never unpickled here. Raises
    :class:`NewerSessionRecord` for a record version above
    :data:`RECORD_VERSION`, and :class:`InvalidSessionRecord` for a payload
    that starts as a record and is not a valid one, including one holding
    ``NaN`` or ``Infinity`` or nested more than :data:`MAX_DEPTH` deep.
    """
    # Neither byte is a pickle opcode, so neither can start a pickle; JSON
    # that starts with "[" is refused as a record rather than unpickled.
    if decrypted[:1] not in (b"{", b"["):
        return LegacyBytes(decrypted)
    try:
        # NaN and Infinity are not JSON, and the writer never stores them.
        doc = json.loads(decrypted, parse_constant=_refuse_constant)
    except (ValueError, RecursionError) as exc:
        raise InvalidSessionRecord(f"not JSON ({exc})") from exc
    if type(doc) is not dict:
        raise InvalidSessionRecord("not a JSON object")
    if _deeper_than(doc, MAX_DEPTH):
        raise InvalidSessionRecord(f"nested more than {MAX_DEPTH} deep")
    version = doc.get(_VERSION_KEY)
    if type(version) is not int:
        raise InvalidSessionRecord(f"{_VERSION_KEY!r} is not an integer version")
    if version > RECORD_VERSION:
        raise NewerSessionRecord(version)
    if version < 1:
        raise InvalidSessionRecord(f"version {version} is not a record version")
    kind = doc.get(_KIND_KEY)
    if type(kind) is not str or kind not in _KINDS:
        raise InvalidSessionRecord(f"{kind!r} is not a session kind")
    _check_layout(doc, kind)
    fields = {}
    for row in _fields_of(kind):
        present, data = _lookup(doc, row.record_path)
        where = ".".join(row.record_path)
        if not present:
            if row.required:
                raise InvalidSessionRecord(f"{where!r} is missing")
            continue
        try:
            row.decode(data)
        except (ValueError, TypeError, OverflowError) as exc:
            raise InvalidSessionRecord(f"{where!r}: {exc}") from exc
        fields[row.state_key] = data
    extra = {key: value for key, value in doc.items() if key not in _TABLE_KEYS}
    return Record(version=version, kind=kind, fields=fields, extra=extra)


def state_to_record(
    kind: str, state: Mapping[str, Any], extra: Mapping[str, Any] | None = None
) -> bytes | Unrecordable:
    """Encode a session's state dict as a record, or say which key prevents it.

    *extra* is written back as top-level keys unchanged (a write-back's
    :attr:`Record.extra`). A value :func:`classify` would refuse (a
    non-finite number, nesting more than :data:`MAX_DEPTH` deep) is
    unrecordable. Raises ``ValueError`` for an unknown *kind* or an *extra*
    key the table owns, both caller errors.
    """
    if kind not in _KINDS:
        raise ValueError(f"unknown session kind {kind!r}")
    if extra is not None and (owned := sorted(set(extra) & _TABLE_KEYS)):
        raise ValueError(f"extra keys {owned} are keys the field table owns")
    if kind == "browser" and state.get("_backend_type") == "selenium":
        return Unrecordable("_backend_type", _SELENIUM_REASON)
    rows = {row.state_key: row for row in _fields_of(kind)}
    pickle_only = {row.state_key: row for row in PICKLE_ONLY if kind in row.kinds}
    encoded: dict[str, Any] = {}
    for key, value in state.items():
        if key in rows:
            try:
                data = rows[key].encode(value)
                # A field sits at most two levels below the record's top.
                if _deeper_than(data, MAX_DEPTH - 2):
                    raise ValueError(f"it nests more than {MAX_DEPTH} deep")
                # What a reader will get: refuses NaN, infinity and non-JSON
                # values, and anything classify would reject.
                rows[key].decode(json.loads(json.dumps(data, allow_nan=False)))
            except (ValueError, TypeError, OverflowError, RecursionError) as exc:
                return Unrecordable(key, f"a record cannot hold it: {exc}")
            encoded[key] = data
        elif key in pickle_only:
            if not pickle_only[key].at_default(value):
                return Unrecordable(key, "it is not at its default, which only a pickle can hold")
        else:
            return Unrecordable(key, "the session record has no field for it")
    doc: dict[str, Any] = {_VERSION_KEY: RECORD_VERSION, _KIND_KEY: kind}
    for row in rows.values():
        if row.state_key not in encoded:
            if row.required:
                return Unrecordable(row.state_key, "the state has no value for it")
            continue
        *blocks, leaf = row.record_path
        node = doc
        for block in blocks:
            node = node.setdefault(block, {})
        node[leaf] = encoded[row.state_key]
    doc |= extra or {}
    if _deeper_than(doc, MAX_DEPTH):
        return Unrecordable("extra", f"a record cannot hold it: it nests over {MAX_DEPTH} deep")
    try:
        return json.dumps(doc, allow_nan=False).encode()
    except ValueError as exc:
        # Only an extra key can hold a non-finite number here: every field
        # was read back above.
        return Unrecordable("extra", f"a record cannot hold it: {exc}")


def refusal_before_state(kind: str, session: object) -> Unrecordable | None:
    """The refusal :func:`state_to_record` would give *session*, when its
    attributes decide it, so the caller need not run ``__getstate__``.

    A selenium session's ``__getstate__`` asks the driver for its URL, a
    WebDriver round trip with a live browser. ``BrowserSession`` reads a
    missing ``_backend_type`` as selenium, and so does this.
    """
    if kind == "browser" and getattr(session, "_backend_type", "selenium") == "selenium":
        return Unrecordable("_backend_type", _SELENIUM_REASON)
    return None


def record_to_state(record: Record) -> dict[str, Any]:
    """The state dict ``__setstate__`` takes for *record*'s kind.

    The stored keys, decoded into new objects on each call, plus each
    pickle-only key of the kind at its default, because
    ``requests.Session.__setstate__`` sets only the keys it is given.
    """
    data = copy.deepcopy(record.fields)
    state = {
        row.state_key: row.decode(data[row.state_key])
        for row in _fields_of(record.kind)
        if row.state_key in data
    }
    for row in PICKLE_ONLY:
        if record.kind in row.kinds:
            state[row.state_key] = row.default()
    return state


def kind_of(session: object) -> str | None:
    """The kind *session*'s exact class is stored as, or None; imports no class."""
    cls = type(session)
    return SESSION_CLASS_PATHS.get((cls.__module__, cls.__qualname__))
