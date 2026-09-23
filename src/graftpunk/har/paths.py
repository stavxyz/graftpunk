"""Pure path templating, no HAR involved.

The one rule for collapsing a dynamic path segment into a parameter, shared
by the digest's endpoint modelling and the fixtures/naming rule below it
(plugin tooling spec, 2026-09-11).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from urllib.parse import unquote, urlsplit, urlunsplit

# The name rule's length thresholds (see holds_an_id): mixed hex, a prefixed id's
# tail, and a base64-like token.
_MIN_HEX_LEN = 12
_MIN_PREFIXED_TAIL = 12
_MIN_BASE64_LEN = 24

# A trailing "s" after one of these is part of the word, not a plural: naive
# stripping turned status/address/analysis/bus into statu_id, addres_id,
# analysi_id, bu_id. Not a general inflector, just the three letters that
# cover the shapes a URL path actually carries (final fix wave, 2026-09-12).
_SINGULAR_BEFORE_FINAL_S = frozenset("sui")

# An email address, matched against the percent-decoded segment: account data,
# so it collapses like an id and is masked in every URL the digest keeps.
_EMAIL_RE = re.compile(r"^[^@\s/]+@[^@\s/]+\.[^@\s/.]+$")

__all__ = [
    "bare_host",
    "bare_path",
    "bare_url",
    "holds_an_id",
    "is_placeholder",
    "looks_dynamic",
    "param_name_for_segment",
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


# holds_an_id's shapes: strong evidence only. A name read as an id is dropped from
# every generated command, so the rule catches only the shapes an account value
# takes as a key, header, input, cookie, or token name. A path segment fails closed
# on its own rule (looks_dynamic), so short random tokens are covered there.
_PART_SPLIT_RE = re.compile(r"[_.\-~$]")
_JOIN_SPLIT_RE = re.compile(r"[_\-]")
_MIN_DIGIT_RUN = 6
_DIGIT_RUN_RE = re.compile(rf"\d{{{_MIN_DIGIT_RUN},}}")
_MIXED_HEX_RE = re.compile(rf"[0-9a-fA-F]{{{_MIN_HEX_LEN},}}")
_PREFIXED_ID_RE = re.compile(rf"[A-Za-z]{{2,8}}[_.\-]([A-Za-z0-9]{{{_MIN_PREFIXED_TAIL},}})")
_BASE64_RE = re.compile(rf"[A-Za-z0-9+/_\-]{{{_MIN_BASE64_LEN},}}=*")
_MIN_BASE64_SWITCHES = 5
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
# A card, national id, phone, or loyalty number written in digit groups: at least
# this many all-digit parts, holding at least this many digits together.
_MIN_DIGIT_GROUPS = 3
_MIN_GROUPED_DIGITS = 7
_PLACEHOLDER_SEGMENT_RE = re.compile(r"\{[A-Za-z0-9_]+\}")


def _has_digit(text: str) -> bool:
    return any(ch.isdigit() for ch in text)


def _has_letter(text: str) -> bool:
    return any(ch.isalpha() for ch in text)


def _letter_digit_switches(text: str) -> int:
    """How many times *text* switches between a letter and a digit."""
    kinds = [ch.isdigit() for ch in text if ch.isalnum()]
    return sum(1 for before, after in zip(kinds, kinds[1:], strict=False) if before != after)


def _parts(text: str) -> list[str]:
    return [part for part in _PART_SPLIT_RE.split(text) if part]


def _email(text: str) -> bool:
    return bool(_EMAIL_RE.match(text))


def _long_digit_run(text: str) -> bool:
    return bool(_DIGIT_RUN_RE.search(text))


def _mixed_hex(text: str) -> bool:
    return any(
        _MIXED_HEX_RE.fullmatch(part) and _has_digit(part) and _has_letter(part)
        for part in _parts(text)
    )


def _digit_groups(text: str) -> bool:
    """True when *text* holds 3 or more all-digit parts totalling 7 or more digits
    (``4111-1111-1111-1111``, ``123-45-6789``, ``1-800-555-0199``); a date
    (``2024-01-15``) as a whole is not."""
    groups = [part for part in _parts(text) if part.isascii() and part.isdigit()]
    return (
        len(groups) >= _MIN_DIGIT_GROUPS
        and sum(len(group) for group in groups) >= _MIN_GROUPED_DIGITS
        and not _DATE_RE.fullmatch(text)
    )


def _prefixed_id(text: str) -> bool:
    """A prefix of 2 to 8 letters, a separator, and a tail of 12 or more characters
    mixing upper case, lower case, and digits (``cus_NffrFeUfNV2Hib``)."""
    match = _PREFIXED_ID_RE.fullmatch(text)
    if match is None:
        return False
    tail = match.group(1)
    return (
        any(ch.isupper() for ch in tail) and any(ch.islower() for ch in tail) and _has_digit(tail)
    )


def _base64_token(text: str) -> bool:
    """The whole or a part is a base64-like token of 24 or more characters switching
    between letters and digits at least 5 times. The ruling's floor was 3; at 3 a
    long WebForms name (``ctl00_ContentPlaceHolder1_txtUserName``, 4 switches)
    reads as an id, and 5 changed no measured random-token rate (round 7b)."""
    return any(
        _BASE64_RE.fullmatch(candidate)
        and _letter_digit_switches(candidate) >= _MIN_BASE64_SWITCHES
        for candidate in (text, *_parts(text))
    )


# The name rule's sub-rules, each the only catch of at least one entry of the
# key-position id table (tests/unit/test_id_miss_rates.py drops each in turn). A
# UUID needs no rule of its own: its last group is 12 hex characters, caught as
# mixed hex, or as a digit run when it is all digits (round 7b).
_NAME_ID_RULES: tuple[tuple[str, Callable[[str], bool]], ...] = (
    ("email", _email),
    ("digit run", _long_digit_run),
    ("mixed hex", _mixed_hex),
    ("digit groups", _digit_groups),
    ("prefixed id", _prefixed_id),
    ("base64 token", _base64_token),
)


def holds_an_id(text: str) -> bool:
    """True when *text*, a name, carries an account value on strong evidence.

    The owner of that decision for every name position: query, JSON, and form keys,
    response keys, header names, cookie and token names, and a login form's element
    ids and input names. A name read as an id is dropped from every generated
    command, so only strong evidence counts. *text* is percent-decoded; it holds an
    id when it, or a part of it (split on ``_ . - ~ $``, so a file extension splits
    off), is:

    - an email, or a UUID (by its 12-character hex group);
    - a run of 6 or more digits (``user_40912873``);
    - hex of 12 or more characters mixing digits and letters (``a3f9c2d1e0b4``);
    - 3 or more all-digit parts totalling 7 or more digits (``4111-1111-1111-1111``,
      ``123-45-6789``; a date such as ``2024-01-15`` as a whole is not);
    - a prefixed id: 2 to 8 letters, a separator, and a tail of 12 or more
      characters mixing upper case, lower case, and digits (``cus_NffrFeUfNV2Hib``);
    - a base64-like token of 24 or more characters switching between letters and
      digits at least 5 times.

    A path segment is judged by :func:`looks_dynamic`, which fails closed and
    consults this rule too. The known limit, measured in
    ``tests/unit/test_id_miss_rates.py``: a short random token used as a name
    (``kqzpwmab47``, ``x7Kq29Lp``) is kept.
    """
    text = unquote(text)
    if not text:
        return False
    return any(rule(text) for _name, rule in _NAME_ID_RULES)


# A path segment's own checks, on top of the name rule: fail closed on any digit
# outside the literal shapes, and a few shapes a short route part can hide.
# The consonant pairs English words are spelled with (y counts as a vowel), the
# ones where two words meet included (backpack, webhook). A letter run holding any
# other pair (fb, kq, zh, ...) before a trailing digit reads as random.
_WORD_CONSONANT_PAIRS = frozenset(
    (  # noqa: SIM905 - one compact string on purpose: the set is data
        "bb bd bh bk bl bm bn bp br bs bt bv bw cc ch ck cl cm cn cr cs ct db dd dg dh "
        "dl dm dn dp dr ds dt dv dw ff fl fn fr fs ft gg gh gl gm gn gr gs gt hb hd hf "
        "hl hm hn hp hr hs ht hw kb kd kf kg kh kl km kn kp kr ks kt kw lb lc ld lf lg "
        "lh lk ll lm ln lp lr ls lt lv lw mb md mf mh ml mm mn mp mr ms mt mw nb nc nd "
        "nf ng nh nk nl nm nn np nr ns nt nv nw nx pb pc pd pf ph pl pm pn pp pr ps pt "
        "pw rb rc rd rf rg rh rk rl rm rn rp rr rs rt rv rw sb sc sd sf sg sh sk sl sm "
        "sn sp sq sr ss st sv sw tb tc td tf tg th tl tm tn tp tr ts tt tv tw wb wc wd "
        "wf wh wk wl wm wn wp wr ws wt xc xp xs xt zz"
    ).split()
)
_VOWELS = frozenset("aeiouyAEIOUY")
# A part of a literal segment that holds a digit: a word with a digit run of at
# most 2 after it (address2, windows10, ec2), a version, or a lone digit run of at
# most 2 (/page/2).
_WORD_WITH_DIGITS_RE = re.compile(r"([A-Za-z]+)\d{1,2}")
_VERSION_RE = re.compile(r"v\d+(?:alpha|beta|rc)?\d*")
# A run of capitals this long is an acronym no longer: MAPLETON7 reads as a code.
_MAX_CAPITALS_RUN = 3


def _spelled(letters: str) -> bool:
    """True when *letters* hold only consonant pairs English words use and no run of
    more than 3 capitals."""
    if len(letters) > _MAX_CAPITALS_RUN and letters.isupper():
        return False
    lowered = letters.lower()
    return all(
        a in _VOWELS or b in _VOWELS or a + b in _WORD_CONSONANT_PAIRS
        for a, b in zip(lowered, lowered[1:], strict=False)
    )


def _literal_part(part: str) -> bool:
    if not _has_digit(part):
        return True
    if _VERSION_RE.fullmatch(part) or (part.isdigit() and len(part) <= 2):
        return True
    match = _WORD_WITH_DIGITS_RE.fullmatch(part)
    return match is not None and _spelled(match.group(1))


def _joined_token(text: str) -> bool:
    """A segment of 3 or more parts joined by ``-`` or ``_``, every part 2 to 6
    characters mixing letters and digits (``ab12-cd34-ef56``)."""
    parts = _JOIN_SPLIT_RE.split(text)
    return len(parts) >= 3 and all(
        2 <= len(part) <= 6 and _has_digit(part) and _has_letter(part) and part.isalnum()
        for part in parts
    )


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
    """True when *segment* is a path segment that collapses into a named parameter.

    Fails closed: a segment is dynamic when :func:`holds_an_id` says so (an email, a
    UUID, a long number, ...) or it is 3 or more short letter-and-digit parts joined
    by ``-`` or ``_`` (``ab12-cd34-ef56``), and a segment holding a digit is literal
    only when every part of it (split on ``_ . - ~ $``) holds no digit, is a word
    spelled with the consonant pairs English uses and no run of 4 or more capitals,
    followed by a digit run of at most 2 (``address2``, ``windows10``, ``ec2``), is
    a version (``v2``, ``v1beta1``), or is a digit run of at most 2 standing alone
    (``/page/2``). Every other segment holding a digit is dynamic: a date, a card,
    phone, or national id number in digit groups, a long number, a mixed token
    (``kqzpwmab47``, ``MAPLETON7``). A letters-only segment that holds no id stays
    literal; the digest's high-cardinality collapse templates a family of many
    digit-bearing siblings.

    The one owner of that judgement: ``template_path`` collapses on it, and the
    digest's high-cardinality collapse gates on it. Names (keys, headers, input
    names) are judged by :func:`holds_an_id` instead.
    """
    if not segment:
        return False
    if holds_an_id(segment):
        return True
    text = unquote(segment)
    if not _has_digit(text):
        return False
    return _joined_token(text) or not all(_literal_part(part) for part in _parts(text))


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
