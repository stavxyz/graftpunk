"""Pure path templating, no HAR involved.

The one rule for collapsing a dynamic path segment into a parameter, shared
by the digest's endpoint modelling and the fixtures/naming rule below it
(plugin tooling spec, 2026-09-11).
"""

from __future__ import annotations

import re
from urllib.parse import unquote, urlsplit, urlunsplit

# A segment collapses to a parameter when it looks like an opaque
# identifier rather than a word: all digits, a UUID, 16+ hex characters, or
# 20+ URL-safe-base64-like characters; one of the short and embedded shapes
# _looks_like_short_id names; or when it holds an email address. The
# base64-like check additionally requires at least one digit, so an ordinary
# long slug ("administrator-dashboard") is not mistaken for an encoded token.
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
_BASE64_RE = re.compile(r"^[A-Za-z0-9_-]+=*$")
# An email address, matched against the percent-decoded segment: account data,
# so it collapses like an id and is masked in every URL the digest keeps.
_EMAIL_RE = re.compile(r"^[^@\s/]+@[^@\s/]+\.[^@\s/.]+$")

__all__ = [
    "bare_host",
    "bare_path",
    "bare_url",
    "looks_dynamic",
    "param_name_for_segment",
    "template_path",
    "templated_url",
    "templates_a_segment",
]


_PATH_PARAMS_RE = re.compile(r";[^/]*")
_PLACEHOLDER_SEGMENT_RE = re.compile(r"\{[A-Za-z0-9_]+\}")


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


# The short and embedded id shapes an account carries through a whole recording
# (one account, one value, so no high-cardinality collapse ever sees them). The
# rule is lexical and errs toward templating: a version or asset segment that
# mixes letters and digits (html5player1) templates too, which costs a readable
# route name and never commits an id.
_DIGIT_RUN_RE = re.compile(r"\d{5,}")
_SHORT_HEX_RE = re.compile(r"[0-9a-fA-F]{8,}")
_PREFIXED_ID_RE = re.compile(r"[A-Za-z]{2,8}_[A-Za-z0-9]{8,}")
_ALNUM_TOKEN_RE = re.compile(r"[A-Za-z0-9]{8,}")
_HEX_LETTER_RE = re.compile(r"[a-fA-F]")


def _mixes_letters_and_digits(text: str) -> bool:
    return any(ch.isdigit() for ch in text) and any(ch.isalpha() for ch in text)


def _looks_like_short_id(segment: str) -> bool:
    """True when *segment* holds a run of 5 or more digits, is 8 or more hex
    characters holding a digit and a letter a-f, is a prefixed id (2 to 8 letters,
    an underscore, and 8 or more letters and digits mixed, as ``cus_NffrFeUfNV2Hib``),
    or is a token of 8 or more letters and digits mixed with no separator."""
    if _DIGIT_RUN_RE.search(segment):
        return True
    if (
        _SHORT_HEX_RE.fullmatch(segment)
        and any(ch.isdigit() for ch in segment)
        and _HEX_LETTER_RE.search(segment)
    ):
        return True
    if _PREFIXED_ID_RE.fullmatch(segment) and _mixes_letters_and_digits(segment.partition("_")[2]):
        return True
    return bool(_ALNUM_TOKEN_RE.fullmatch(segment)) and _mixes_letters_and_digits(segment)


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
    """True when *segment* reads as an opaque identifier rather than a word, or holds
    an email address (percent-decoded first), which is account data.

    The one owner of that judgement: ``template_path`` collapses on it, and the
    digest's high-cardinality collapse gates on it (in a relaxed form) so a run
    of distinct word-like sibling routes is not mistaken for one parameterised
    family.
    """
    if not segment:
        return False
    if segment.isdigit() or _is_email(segment):
        return True
    if _looks_like_short_id(segment):
        return True
    if _UUID_RE.match(segment):
        return True
    if len(segment) >= _MIN_HEX_LEN and _HEX_RE.match(segment):
        return True
    return (
        len(segment) >= _MIN_BASE64_LEN
        and bool(_BASE64_RE.match(segment))
        and any(ch.isdigit() for ch in segment)
    )


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
    if any(_PLACEHOLDER_SEGMENT_RE.fullmatch(segment) for segment in path.split("/")):
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
