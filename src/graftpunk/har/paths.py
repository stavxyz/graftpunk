"""Pure path templating, no HAR involved.

The one rule for collapsing a dynamic path segment into a parameter, shared
by the digest's endpoint modelling and the fixtures/naming rule below it
(plugin tooling spec, 2026-09-11).
"""

from __future__ import annotations

import re
from collections.abc import Callable
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


# holds_an_id's shapes. The rule is lexical, reads the whole name first and then
# its parts (split on _ . - ~ $, so a file extension splits off), and errs toward
# an id: a false positive costs a readable name, which a GP-FILL comment counts;
# a false negative commits an account value.
_PART_SPLIT_RE = re.compile(r"[_.\-~$]")
_JOIN_SPLIT_RE = re.compile(r"[_\-]")
_WHOLE_BASE64_RE = re.compile(rf"[A-Za-z0-9_\-]{{{_MIN_BASE64_LEN},}}=*")
_DIGIT_RUN_RE = re.compile(r"\d{5,}")
_HEX_PART_RE = re.compile(r"[0-9a-fA-F]{8,}")
_BASE64_PART_RE = re.compile(rf"[A-Za-z0-9+/]{{{_MIN_BASE64_LEN},}}=*")
_PREFIXED_ID_RE = re.compile(r"[A-Za-z]{2,8}[_.\-]([A-Za-z0-9]{8,})")
_ALNUM_RE = re.compile(r"[A-Za-z0-9]+")
_LEADING_ZERO_RE = re.compile(r"0\d{2,}")
_VERSION_RE = re.compile(r"v\d+(?:alpha|beta|rc)?\d*")
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
# A card, national id, phone, or loyalty number written in digit groups: at least
# this many all-digit parts, holding at least this many digits together.
_MIN_DIGIT_GROUPS = 3
_MIN_GROUPED_DIGITS = 7
# A word's letter runs split at camel and Pascal boundaries (AddressLine, HTTPClient).
_WORD_RUN_RE = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")
_VOWELS = frozenset("aeiouyAEIOUY")
_PLACEHOLDER_SEGMENT_RE = re.compile(r"\{[A-Za-z0-9_]+\}")
# A part at least this long that mixes letters and digits is an id unless it
# reads as a word or a version.
_MIN_MIXED_LEN = 8
# A long joined name reads as words with at most this many 1- or 2-letter runs.
_MAX_JOINED_SHORT_RUNS = 1


def _has_digit(text: str) -> bool:
    return any(ch.isdigit() for ch in text)


def _has_letter(text: str) -> bool:
    return any(ch.isalpha() for ch in text)


def _letter_digit_transitions(part: str) -> int:
    """How many times *part* switches between a letter and a digit."""
    kinds = [ch.isdigit() for ch in part if ch.isalnum()]
    return sum(1 for before, after in zip(kinds, kinds[1:], strict=False) if before != after)


# The consonant pairs English field names are spelled with (y counts as a vowel),
# the ones where two words meet included (backpack, webhook, thumbnail). A letter
# run of 4 or more holding any other pair (fb, kq, zh, ...) reads as random.
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
_MIN_SPOKEN_RUN = 4


def _odd_consonant_pair(lowered: str) -> bool:
    return any(
        a not in _VOWELS and b not in _VOWELS and a + b not in _WORD_CONSONANT_PAIRS
        for a, b in zip(lowered, lowered[1:], strict=False)
    )


# Each check rejects a letter run of 4 or more as not spelled like a word. Every
# check here and in _WORD_REJECTS meets one criterion, automated in
# tests/unit/test_id_miss_rates.py: removing it breaches a miss-rate ceiling. The
# ones that did not were deleted (round 6: odd vowel pairs, q without u, a tripled
# letter, a run with no vowel; round 7: a word-initial pair, four consonants).
_SPELLING_REJECTS: tuple[tuple[str, Callable[[str], bool]], ...] = (
    ("consonant pair", _odd_consonant_pair),
)


def _spoken_run(run: str) -> bool:
    """True when the letter run *run* (4 or more letters) is spelled like a word: no
    check in ``_SPELLING_REJECTS`` rejects it."""
    lowered = run.lower()
    return not any(reject(lowered) for _name, reject in _SPELLING_REJECTS)


def _starts_with_a_digit(part: str, runs: list[str]) -> bool:
    return part[0].isdigit()


def _capitals_run(part: str, runs: list[str]) -> bool:
    return any(run.isalpha() and len(run) >= _MIN_SPOKEN_RUN and run.isupper() for run in runs)


def _unspelled_run(part: str, runs: list[str]) -> bool:
    return any(
        run.isalpha() and len(run) >= _MIN_SPOKEN_RUN and not _spoken_run(run) for run in runs
    )


def _no_word_run(part: str, runs: list[str]) -> bool:
    return not any(run.isalpha() and len(run) >= _MIN_SPOKEN_RUN for run in runs)


def _short_lower_run_after_digits(part: str, runs: list[str]) -> bool:
    return any(
        previous.isdigit() and run.isalpha() and run[0].islower() and len(run) < _MIN_SPOKEN_RUN
        for previous, run in zip(runs, runs[1:], strict=False)
    )


def _lower_run_after_acronym_digits(part: str, runs: list[str]) -> bool:
    return any(
        acronym.isalpha()
        and len(acronym) < _MIN_SPOKEN_RUN
        and digits.isdigit()
        and run.isalpha()
        and run[0].islower()
        for acronym, digits, run in zip(runs, runs[1:], runs[2:], strict=False)
    )


# Each check rejects a part of letters and digits mixed as not a word, held to the
# same criterion as _SPELLING_REJECTS; the ones that bought nothing were deleted
# (round 6: at most two letter/digit switches, a digit run of at most 4, a vowel in
# every 3-letter run, and the separate 3-or-more-switches rule; round 7: more than
# one run of 1 or 2 letters).
_WORD_REJECTS: tuple[tuple[str, Callable[[str, list[str]], bool]], ...] = (
    ("starts with a digit", _starts_with_a_digit),
    ("capitals run", _capitals_run),
    ("unspelled run", _unspelled_run),
    ("no word run", _no_word_run),
    ("short lower run after digits", _short_lower_run_after_digits),
    ("lower run after acronym digits", _lower_run_after_acronym_digits),
)


def _reads_as_a_word(part: str) -> bool:
    """True when *part*, letters and digits mixed, reads as a field name: a version
    (``v1beta1``), or a word no check in ``_WORD_REJECTS`` rejects. Its letter runs
    split at camel and Pascal boundaries. A word starts with a letter, holds a run
    of 4 or more letters, spells every such run with only the consonant pairs
    English words use and never in capitals alone, and starts a letter run after
    digits as a new word: capitalised (``oauth2Token``), or lower-case of 4 or more
    letters (``added2cart``) when the letters before the digits are a word, not an
    acronym (``ipv4Address``)."""
    if _VERSION_RE.fullmatch(part):
        return True
    runs = _WORD_RUN_RE.findall(part)
    return not any(reject(part, runs) for _name, reject in _WORD_REJECTS)


def _digit_groups(text: str) -> bool:
    """True when *text* holds 3 or more all-digit parts totalling 7 or more digits
    (``4111-1111-1111-1111``, ``123-45-6789``, ``1-800-555-0199``); a date
    (``2024-01-15``) as a whole is not."""
    groups = [part for part in _PART_SPLIT_RE.split(text) if part.isascii() and part.isdigit()]
    return (
        len(groups) >= _MIN_DIGIT_GROUPS
        and sum(len(group) for group in groups) >= _MIN_GROUPED_DIGITS
        and not _DATE_RE.fullmatch(text)
    )


def _joined_token(text: str) -> bool:
    """A segment of 3 or more parts joined by ``-`` or ``_`` that is an id as a
    whole: every part 2 to 6 characters mixing letters and digits
    (``ab12-cd34-ef56``), or one part of 3 or more digits with a leading zero beside
    a part holding a letter (``ORD-2024-0001``). A date (``2024-01-15``) and a
    two-digit step (``step_01_done``) are not."""
    parts = _JOIN_SPLIT_RE.split(text)
    if len(parts) < 3:
        return False
    if any(_LEADING_ZERO_RE.fullmatch(part) for part in parts) and any(
        _has_letter(part) for part in parts
    ):
        return True
    return all(
        2 <= len(part) <= 6 and _has_digit(part) and _has_letter(part) and part.isalnum()
        for part in parts
    )


_SHORT_WORD_WITH_DIGITS_RE = re.compile(r"[a-z]{2,}\d{1,2}")


def _joined_part_reads_as_a_word(part: str) -> bool:
    """True when one ``_``/``-`` part of a long name reads as a word: a digit run (one
    of 5 or more digits is an id by the part rule after this one), a lower-case word
    with 1 or 2 digits (``ctl00``, ``line2``), a version, a letter run spelled like a
    word, or letters and digits that read as a word."""
    if not part or part.isdigit():
        return True
    if _SHORT_WORD_WITH_DIGITS_RE.fullmatch(part) or _VERSION_RE.fullmatch(part):
        return True
    if part.isalpha():
        return _letters_read_as_words(part)
    return bool(_ALNUM_RE.fullmatch(part)) and _reads_as_a_word(part)


def _joined_name_reads_as_words(text: str) -> bool:
    """True when a long ``_``/``-`` joined *text* reads as words, not as a random
    token: every part reads as a word (:func:`_joined_part_reads_as_a_word`), and the
    whole holds at most one run of 1 or 2 letters outside a version or a short word
    with digits (``X-Goog-Upload-Protocol-v2-Status``)."""
    parts = _JOIN_SPLIT_RE.split(text)
    short = sum(
        1
        for part in parts
        if not (_VERSION_RE.fullmatch(part) or _SHORT_WORD_WITH_DIGITS_RE.fullmatch(part))
        for run in _WORD_RUN_RE.findall(part)
        if run.isalpha() and len(run) <= 2
    )
    return short <= _MAX_JOINED_SHORT_RUNS and all(
        _joined_part_reads_as_a_word(part) for part in parts
    )


def _letters_read_as_words(part: str) -> bool:
    """True when the letters-only *part* reads as words: every camel or Pascal run of
    4 or more letters is spelled like a word (``txtUserName``: ``User``, ``Name``)."""
    return all(len(run) < _MIN_SPOKEN_RUN or _spoken_run(run) for run in _WORD_RUN_RE.findall(part))


def _part_holds_an_id(part: str) -> bool:
    if _DIGIT_RUN_RE.search(part):
        return True
    if len(part) >= _MIN_HEX_LEN and _HEX_RE.match(part):
        return True
    if _HEX_PART_RE.fullmatch(part) and _has_digit(part) and _has_letter(part):
        return True
    if (
        _BASE64_PART_RE.fullmatch(part)
        and _has_digit(part)
        and not (_ALNUM_RE.fullmatch(part) and _reads_as_a_word(part))
    ):
        return True
    return bool(
        len(part) >= _MIN_MIXED_LEN
        and _ALNUM_RE.fullmatch(part)
        and _has_digit(part)
        and _has_letter(part)
        and not _reads_as_a_word(part)
    )


def holds_an_id(text: str) -> bool:
    """True when *text*, a name, carries an account value.

    The one owner of that decision for every name position: query, JSON, and form
    keys, response keys, header names, cookie and token names, and a login form's
    element ids and input names. A path segment is judged by :func:`looks_dynamic`,
    which fails closed and consults this rule too. *text* is percent-decoded, then
    read in order:

    1. The whole text is an email or a UUID; or holds 3 or more all-digit parts
       totalling 7 or more digits (``4111-1111-1111-1111``, ``123-45-6789``; a date
       such as ``2024-01-15`` as a whole is not); or is a URL-safe base64-like token
       of 20 or more characters holding a digit and switching between letters and
       digits at least twice, unless it is a long ``_``/``-`` joined name whose parts
       all read as words (``line_item_2_unit_price``,
       ``ctl00_MainContent_LoginUser_Password``).
    2. The whole text is 3 or more parts joined by ``-`` or ``_``, every part 2 to 6
       characters mixing letters and digits, or one part of 3 or more digits with a
       leading zero beside a part holding a letter (``ORD-2024-0001``; a step such
       as ``step_01_done`` is not).
    3. The whole text is a prefixed id: 2 to 8 letters, a separator, and a tail of 8
       or more characters mixing letters and digits that does not read as a word
       (``cus_4fK2x9QaZ1``).
    4. A part (split on ``_ . - ~ $``) holds a run of 5 or more digits, is 16 or more
       hex characters or 8 or more mixing hex digits and letters, or is a
       base64-like token of 20 or more characters holding a digit that does not read
       as a word (``shippingAddressLine2`` does).
    5. A part of 8 or more letters and digits mixed does not read as a word
       (:func:`_reads_as_a_word`): ``kqzpwmab47``, ``XKQ29LPZ``, ``Zq9XkLmPwR``.

    The rule is lexical and measured both ways (``tests/unit/test_id_miss_rates.py``:
    random-token miss rates per shape, and false-positive rates over a regression
    corpus and a held-out corpus of public SDK names). It misses a random token
    with no digit, a short word-like value, and a random token whose letter runs are
    each spelled like words (``cus_NffrFeUfNV2Hib``). It reads as an id a name with
    lower-case letters right after a digit (``add2cart``, ``retina2x``,
    ``k8sNamespace``), a run of 5 or more digits (``ed25519``), no run of 4 or more
    letters (``sha256Key``), a letter run with a consonant pair words do not use
    (``pbkdf2Iterations``), or 20 or more characters read as a base64 token
    (``Md5OfMessageAttributes``).
    """
    text = unquote(text)
    if not text:
        return False
    if _EMAIL_RE.match(text) or _UUID_RE.match(text) or _digit_groups(text):
        return True
    if (
        _WHOLE_BASE64_RE.fullmatch(text)
        and _has_digit(text)
        and _letter_digit_transitions(text) >= 2
        and not _joined_name_reads_as_words(text)
    ):
        return True
    if _joined_token(text):
        return True
    prefixed = _PREFIXED_ID_RE.fullmatch(text)
    if prefixed:
        tail = prefixed.group(1)
        if _has_digit(tail) and _has_letter(tail) and not _reads_as_a_word(tail):
            return True
    return any(_part_holds_an_id(part) for part in _PART_SPLIT_RE.split(text) if part)


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


# The only parts a path segment holding a digit may have and stay literal: letters
# alone, a word with a digit run of at most 2 after it (address2, windows10, ec2), a
# version, or a lone digit run of at most 2 (/page/2).
_LITERAL_PART_RE = re.compile(r"[A-Za-z]*\d{0,2}|v\d+(?:alpha|beta|rc)?\d*")


def looks_dynamic(segment: str) -> bool:
    """True when *segment* is a path segment that collapses into a named parameter.

    Fails closed: a segment is dynamic when :func:`holds_an_id` says so (an email, a
    UUID, a hex or random token, ...), and a segment holding a digit is literal only
    when every part of it (split on ``_ . - ~ $``) is letters alone, a word of
    letters with a digit run of at most 2 after it (``address2``, ``windows10``,
    ``ec2``), a version (``v2``, ``v1beta1``), or a digit run of at most 2 standing
    alone (``/page/2``). Every other segment holding a digit is dynamic: a date, a
    card, phone, or national id number in digit groups, a long number, a mixed
    token. A letters-only segment that holds no id stays literal; the digest's
    high-cardinality collapse templates a family of many digit-bearing siblings.

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
    return not all(_LITERAL_PART_RE.fullmatch(part) for part in _PART_SPLIT_RE.split(text))


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
