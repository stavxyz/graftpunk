"""Pure path templating, no HAR involved.

The one rule for collapsing a dynamic path segment into a parameter, shared
by the digest's endpoint modelling and the fixtures/naming rule below it
(plugin tooling spec, 2026-09-11).
"""

from __future__ import annotations

import hashlib
import re
from urllib.parse import unquote, urlsplit, urlunsplit

# A segment collapses to a parameter when it is all digits or holds_an_id says
# it carries an account value (see there for every shape). The base64-like and
# long-hex lengths below are two of those shapes.
_MIN_HEX_LEN = 16
_MIN_BASE64_LEN = 20

# A trailing "s" after one of these is part of the word, not a plural: naive
# stripping turned status/address/analysis/bus into statu_id, addres_id,
# analysi_id, bu_id. Not a general inflector, just the three letters that
# cover the shapes a URL path actually carries (final fix wave, 2026-09-12).
_SINGULAR_BEFORE_FINAL_S = frozenset("sui")

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)
_HEX_RE = re.compile(r"^[0-9a-fA-F]+$")
# An email address, matched against the percent-decoded segment: account data,
# so it collapses like an id and is masked in every URL the digest keeps.
_EMAIL_RE = re.compile(r"^[^@\s/]+@[^@\s/]+\.[^@\s/.]+$")

__all__ = [
    "bare_host",
    "bare_path",
    "bare_url",
    "holds_an_id",
    "REDACTED_NAME_PREFIX",
    "is_placeholder",
    "looks_dynamic",
    "param_name_for_segment",
    "redacted_name",
    "template_path",
    "templated_url",
    "templates_a_segment",
]


_PATH_PARAMS_RE = re.compile(r";[^/]*")


def bare_path(path: str) -> str:
    """*path* with the ``;params`` of every segment removed.

    A matrix parameter (``;jsessionid=...``) can sit on any segment, not only
    the last, and it carries a session id or a token rather than a route.
    """
    return _PATH_PARAMS_RE.sub("", path)


def bare_host(netloc: str) -> str:
    """*netloc* without its ``user:password@``: a credential, not part of the host.
    The port stays."""
    return netloc.rpartition("@")[2]


# holds_an_id's shapes. The rule is lexical, splits a name on [_.-~] (a file
# extension included), and errs toward an id: a false positive costs a readable
# name, which a GP-FILL comment then counts; a false negative commits an account
# value.
_PART_SPLIT_RE = re.compile(r"[_.\-~]")
_DIGIT_RUN_RE = re.compile(r"\d{5,}")
_HEX_PART_RE = re.compile(r"[0-9a-fA-F]{8,}")
_BASE64_PART_RE = re.compile(rf"[A-Za-z0-9+/]{{{_MIN_BASE64_LEN},}}=*")
_PREFIXED_ID_RE = re.compile(r"[A-Za-z]{2,8}[_.\-]([A-Za-z0-9]{8,})")
_MIXED_TOKEN_RE = re.compile(r"[A-Za-z0-9]{8,}")
# Not ids: a lower-case word with trailing digits (address2, windows10), a
# camelCase word (orderId2), and a Kubernetes-style version (v1beta1).
_WORD_WITH_DIGITS_RE = re.compile(r"[a-z]+\d+")
_CAMEL_WORD_RE = re.compile(r"[a-z]+(?:[A-Z][a-z]+)+\d*")
_VERSION_RE = re.compile(r"v\d+(?:alpha|beta|rc)?\d*")
_PLACEHOLDER_SEGMENT_RE = re.compile(r"\{[A-Za-z0-9_]+\}")


def _has_digit(text: str) -> bool:
    return any(ch.isdigit() for ch in text)


# A part this long that switches between letters and digits 3 or more times is an
# interleaved random token (x7kq29lp); a word with digits switches once or twice
# (address2, added2cart). html5player1 switches 3 times and templates, which fails
# safe.
_MIN_INTERLEAVED_LEN = 8


def _letter_digit_transitions(part: str) -> int:
    """How many times *part* switches between a letter and a digit."""
    kinds = [ch.isdigit() for ch in part if ch.isalnum()]
    return sum(1 for before, after in zip(kinds, kinds[1:], strict=False) if before != after)


def _is_word(part: str) -> bool:
    return bool(
        _WORD_WITH_DIGITS_RE.fullmatch(part)
        or _CAMEL_WORD_RE.fullmatch(part)
        or _VERSION_RE.fullmatch(part)
    )


def _part_holds_an_id(part: str) -> bool:
    if _DIGIT_RUN_RE.search(part):
        return True
    if _is_word(part):
        return False
    if _HEX_PART_RE.fullmatch(part) and _has_digit(part) and any(ch.isalpha() for ch in part):
        return True
    if len(part) >= _MIN_HEX_LEN and _HEX_RE.match(part):
        return True
    if _BASE64_PART_RE.fullmatch(part) and _has_digit(part):
        return True
    if len(part) >= _MIN_INTERLEAVED_LEN and _letter_digit_transitions(part) >= 3:
        return True
    return bool(
        _MIXED_TOKEN_RE.fullmatch(part)
        and _has_digit(part)
        and any(ch.isupper() for ch in part)
        and any(ch.islower() for ch in part)
    )


def holds_an_id(text: str) -> bool:
    """True when *text*, a name or a path segment, carries an account value.

    The one owner of that decision: a path segment (``looks_dynamic``), a query,
    body, form, or response key, a header name, and a cookie name all go through
    it. *text* is percent-decoded, then it holds an id when it is an email or a
    UUID, is a prefixed id (a short alphabetic prefix, a separator from ``_.-``,
    and a tail of 8 or more characters mixing letters and digits, as
    ``cus_NffrFeUfNV2Hib``), or when any of its parts, split on ``_ . - ~``, holds a
    run of 5 or more digits, is a hex token of 8 or more characters holding a digit
    and a letter, is 16 or more hex characters, is a base64-like token of 20 or more
    characters holding a digit, is a token of 8 or more characters mixing upper
    case, lower case, and digits, or is 8 or more characters switching between a
    letter and a digit 3 or more times (``x7kq29lp``). A lower-case word with trailing digits
    (``address2``), a camelCase word (``orderId2``), and a version (``v1beta1``) are
    words, not ids, unless they hold a run of 5 or more digits.

    The rule is lexical: an id in a shape it does not read (a short word-like
    value) is not caught.
    """
    text = unquote(text)
    if not text:
        return False
    if _EMAIL_RE.match(text) or _UUID_RE.match(text):
        return True
    prefixed = _PREFIXED_ID_RE.fullmatch(text)
    if prefixed:
        tail = prefixed.group(1)
        if _has_digit(tail) and any(ch.isalpha() for ch in tail) and not _is_word(tail):
            return True
    return any(_part_holds_an_id(part) for part in _PART_SPLIT_RE.split(text) if part)


REDACTED_NAME_PREFIX = "sha256:"


def redacted_name(name: str) -> str:
    """*name*, or ``sha256:<hex digest of its UTF-8 bytes>`` when it
    :func:`holds_an_id`: for a name that must still be matched later (a cookie or
    token name a fixture must not contain) but must not be written down."""
    if not holds_an_id(name):
        return name
    return REDACTED_NAME_PREFIX + hashlib.sha256(name.encode("utf-8")).hexdigest()


def is_placeholder(segment: str) -> bool:
    """True when *segment* is a ``{name}`` placeholder, as ``template_path`` and
    ``bare_url``'s email masking write one."""
    return bool(_PLACEHOLDER_SEGMENT_RE.fullmatch(segment))


def _is_email(segment: str) -> bool:
    return bool(_EMAIL_RE.match(unquote(segment)))


def _masked_emails(path: str) -> str:
    """*path* with each segment that holds an email address replaced by the
    placeholder :func:`template_path` would give it, every other segment as it was."""
    segments = path.split("/")
    for index, segment in enumerate(segments):
        if _is_email(segment):
            previous = segments[index - 1] if index else ""
            if looks_dynamic(previous):
                previous = "{}"
            segments[index] = f"{{{param_name_for_segment(previous)}}}"
    return "/".join(segments)


def bare_url(url: str) -> str:
    """*url* reduced to its scheme, host, and :func:`bare_path` path, with any
    segment that holds an email address masked as its placeholder.

    The one rule for every URL the digest keeps: the query string, the fragment,
    every segment's ``;params``, any ``user:password@``, and an email in the path
    are account values, not parts of the route. A relative URL stays relative, and
    one that was only a query or ``;params`` comes back empty.
    """
    parts = urlsplit(url)
    path = _masked_emails(bare_path(parts.path))
    return urlunsplit((parts.scheme, bare_host(parts.netloc), path, "", ""))


def looks_dynamic(segment: str) -> bool:
    """True when *segment* is all digits or :func:`holds_an_id`: a path segment
    that collapses into a named parameter.

    The one owner of that judgement: ``template_path`` collapses on it, and the
    digest's high-cardinality collapse gates on it (in a relaxed form) so a run
    of distinct word-like sibling routes is not mistaken for one parameterised
    family.
    """
    if not segment:
        return False
    return segment.isdigit() or holds_an_id(segment)


def param_name_for_segment(prev_segment: str) -> str:
    """The parameter name for a segment collapsed at ``prev_segment``'s position.

    The singular of the preceding literal segment (``orders`` -> ``order_id``),
    or the bare ``id`` when there is no preceding segment or it is itself a
    collapsed parameter (``{order_id}``).
    """
    if not prev_segment or prev_segment.startswith("{"):
        return "id"
    is_plural = (
        prev_segment.endswith("s")
        and len(prev_segment) > 1
        and prev_segment[-2] not in _SINGULAR_BEFORE_FINAL_S
    )
    singular = prev_segment[:-1] if is_plural else prev_segment
    return f"{singular}_id"


def template_path(path: str) -> tuple[str, dict[str, str]]:
    """Collapse a URL path's dynamic segments into named parameters.

    ``/orders/123`` becomes ``/orders/{order_id}``. A leading and/or
    trailing slash is preserved exactly as given, and every segment's
    ``;params`` are dropped first (:func:`bare_path`).

    Returns:
        The templated path, and a dict mapping each collapsed parameter
        name to the literal segment value it replaced.
    """
    path = bare_path(path)
    leading = "/" if path.startswith("/") else ""
    trailing = "/" if len(path) > 1 and path.endswith("/") else ""
    segments = path.strip("/").split("/") if path.strip("/") else []

    params: dict[str, str] = {}
    result_segments: list[str] = []
    for segment in segments:
        if looks_dynamic(segment):
            prev = result_segments[-1] if result_segments else ""
            name = param_name_for_segment(prev)
            params[name] = segment
            result_segments.append(f"{{{name}}}")
        else:
            result_segments.append(segment)

    body = "/".join(result_segments)
    return f"{leading}{body}{trailing}" if body else (leading or "/"), params


def templates_a_segment(url: str) -> bool:
    """True when :func:`template_path` turns a segment of *url*'s path into a
    parameter, so the path holds an id or a token.

    Compared path to path (templated against :func:`bare_path`), never whole
    strings: :func:`templated_url` gives a URL with no path a ``/``, which is not an
    account value. A segment :func:`bare_url` already masked (an email, now
    ``{user_id}``) counts too.
    """
    path = bare_path(urlsplit(url).path)
    if any(is_placeholder(segment) for segment in path.split("/")):
        return True
    return bool(path) and template_path(path)[0] != path


def templated_url(url: str) -> str:
    """*url* reduced to its scheme, host, and path, the path templated the way an
    endpoint's is (:func:`template_path`).

    For a URL a digest records as observed (a login observation, a form action)
    and a projection or generated file then prints: its path can hold an account
    id or a one-time token. An absolute URL with no path gets ``/``; a relative
    one stays relative, and an empty one stays empty (an empty form action posts
    to the page itself).
    """
    parts = urlsplit(url)
    path = parts.path or ("/" if parts.netloc else "")
    template = template_path(path)[0] if path else ""
    return urlunsplit((parts.scheme, bare_host(parts.netloc), template, "", ""))
