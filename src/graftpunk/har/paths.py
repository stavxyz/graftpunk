"""Pure path templating, no HAR involved.

The one rule for collapsing a dynamic path segment into a parameter, shared
by the digest's endpoint modelling and the fixtures/naming rule below it
(plugin tooling spec, 2026-09-11).
"""

from __future__ import annotations

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
# its parts (split on _ . - ~, so a file extension splits off), and errs toward
# an id: a false positive costs a readable name, which a GP-FILL comment counts;
# a false negative commits an account value.
_PART_SPLIT_RE = re.compile(r"[_.\-~]")
_JOIN_SPLIT_RE = re.compile(r"[_\-]")
_WHOLE_BASE64_RE = re.compile(rf"[A-Za-z0-9_\-]{{{_MIN_BASE64_LEN},}}=*")
_DIGIT_RUN_RE = re.compile(r"\d{5,}")
_HEX_PART_RE = re.compile(r"[0-9a-fA-F]{8,}")
_BASE64_PART_RE = re.compile(rf"[A-Za-z0-9+/]{{{_MIN_BASE64_LEN},}}=*")
_PREFIXED_ID_RE = re.compile(r"[A-Za-z]{2,8}[_.\-]([A-Za-z0-9]{8,})")
_ALNUM_RE = re.compile(r"[A-Za-z0-9]+")
_LEADING_ZERO_RE = re.compile(r"0\d+")
_VERSION_RE = re.compile(r"v\d+(?:alpha|beta|rc)?\d*")
# A word's letter runs split at camel and Pascal boundaries (AddressLine, HTTPClient).
_WORD_RUN_RE = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")
_VOWELS = frozenset("aeiouyAEIOUY")
_PLACEHOLDER_SEGMENT_RE = re.compile(r"\{[A-Za-z0-9_]+\}")
# A part at least this long that mixes letters and digits is an id unless it
# reads as a word or a version.
_MIN_MIXED_LEN = 8
_MAX_WORD_DIGIT_RUN = 4
_MAX_WORD_SWITCHES = 2
_MIN_INTERLEAVED_LEN = 8
_INTERLEAVED_SWITCHES = 3


def _has_digit(text: str) -> bool:
    return any(ch.isdigit() for ch in text)


def _has_letter(text: str) -> bool:
    return any(ch.isalpha() for ch in text)


def _letter_digit_transitions(part: str) -> int:
    """How many times *part* switches between a letter and a digit."""
    kinds = [ch.isdigit() for ch in part if ch.isalnum()]
    return sum(1 for before, after in zip(kinds, kinds[1:], strict=False) if before != after)


# The consonant pairs English field names are spelled with (y counts as a vowel).
# A letter run of 4 or more holding any other pair (fb, kq, zh, ...) reads as
# random, not as a word.
_WORD_CONSONANT_PAIRS = frozenset(
    {
        "bb",
        "bl",
        "br",
        "bs",
        "cc",
        "ch",
        "ck",
        "cl",
        "cr",
        "cs",
        "ct",
        "dd",
        "dg",
        "dl",
        "dm",
        "dn",
        "dr",
        "ds",
        "dt",
        "dw",
        "ff",
        "fl",
        "fr",
        "fs",
        "ft",
        "gg",
        "gh",
        "gl",
        "gm",
        "gn",
        "gr",
        "gs",
        "hl",
        "hm",
        "hn",
        "hr",
        "ht",
        "kl",
        "kn",
        "kr",
        "ks",
        "kw",
        "lb",
        "lc",
        "ld",
        "lf",
        "lg",
        "lk",
        "ll",
        "lm",
        "ln",
        "lp",
        "lr",
        "ls",
        "lt",
        "lv",
        "lw",
        "mb",
        "ml",
        "mm",
        "mn",
        "mp",
        "mr",
        "ms",
        "nb",
        "nc",
        "nd",
        "nf",
        "ng",
        "nk",
        "nl",
        "nm",
        "nn",
        "np",
        "ns",
        "nt",
        "nv",
        "nx",
        "ph",
        "pl",
        "pm",
        "pn",
        "pp",
        "pr",
        "ps",
        "pt",
        "rb",
        "rc",
        "rd",
        "rf",
        "rg",
        "rh",
        "rk",
        "rl",
        "rm",
        "rn",
        "rp",
        "rr",
        "rs",
        "rt",
        "rv",
        "rw",
        "sc",
        "sf",
        "sh",
        "sk",
        "sl",
        "sm",
        "sn",
        "sp",
        "sq",
        "ss",
        "st",
        "sw",
        "tb",
        "tc",
        "tf",
        "th",
        "tl",
        "tm",
        "tn",
        "tp",
        "tr",
        "ts",
        "tt",
        "tw",
        "wb",
        "wd",
        "wf",
        "wk",
        "wl",
        "wm",
        "wn",
        "wp",
        "wr",
        "ws",
        "wt",
        "xc",
        "xp",
        "xs",
        "xt",
        "zz",
    }
)
# The consonant pairs a word starts with.
_WORD_INITIAL_PAIRS = frozenset(
    {
        "bl",
        "br",
        "ch",
        "cl",
        "cr",
        "dr",
        "fl",
        "fr",
        "gl",
        "gn",
        "gr",
        "kl",
        "kn",
        "kr",
        "ph",
        "pl",
        "pr",
        "sc",
        "sh",
        "sk",
        "sl",
        "sm",
        "sn",
        "sp",
        "sq",
        "st",
        "sw",
        "th",
        "tr",
        "tw",
        "wh",
        "wr",
    }
)
# Vowel pairs English field names do not spell (a random token often does).
_ODD_VOWEL_PAIRS = frozenset({"aa", "ii", "uu", "yy", "iy", "yi", "uy", "yu"})
_MIN_SPOKEN_RUN = 4


def _spoken_run(run: str) -> bool:
    """True when the letter run *run* (4 or more letters) is spelled like a word:
    it holds a vowel, starts with a vowel or a consonant pair words start with,
    holds no 4 consonants in a row, no letter three times running, no ``q`` without
    a ``u``, no vowel pair English does not spell (``ii``, ``yy``), and every pair
    of adjacent consonants is one English spells words with."""
    lowered = run.lower()
    if not any(ch in _VOWELS for ch in lowered):
        return False
    starts_with_a_cluster = lowered[0] not in _VOWELS and lowered[1] not in _VOWELS
    if starts_with_a_cluster and lowered[:2] not in _WORD_INITIAL_PAIRS:
        return False
    if any(lowered[i : i + 2] in _ODD_VOWEL_PAIRS for i in range(len(lowered) - 1)):
        return False
    if "q" in lowered.replace("qu", ""):
        return False
    consonants = 0
    for index, ch in enumerate(lowered):
        if index >= 2 and ch == lowered[index - 1] == lowered[index - 2]:
            return False
        if ch in _VOWELS:
            consonants = 0
            continue
        consonants += 1
        if consonants >= 4:
            return False
        if consonants >= 2 and lowered[index - 1 : index + 1] not in _WORD_CONSONANT_PAIRS:
            return False
    return True


def _reads_as_a_word(part: str) -> bool:
    """True when *part*, letters and digits mixed, reads as a field name: a version
    (``v1beta1``), or a word. A word starts with a letter, switches between letters
    and digits at most twice, holds no digit run longer than 4, and holds at least
    one letter run (split at camel and Pascal boundaries) of 4 or more letters;
    every such run is spelled like a word (:func:`_spoken_run`) and is not 4 or more
    capitals, and every run of 3 letters holds a vowel. Shorter runs stand as
    acronyms (``md5Checksum``, ``ipv4Address``, ``x509Certificate``). A letter run
    after digits starts a new word: it is capitalised (``oauth2Token``) or a
    lower-case run of 4 or more letters (``added2cart``)."""
    if _VERSION_RE.fullmatch(part):
        return True
    if part[0].isdigit() or _letter_digit_transitions(part) > _MAX_WORD_SWITCHES:
        return False
    spoken = False
    after_digits = False
    after_acronym = False
    previous_letters = ""
    for run in _WORD_RUN_RE.findall(part):
        if run.isdigit():
            if len(run) > _MAX_WORD_DIGIT_RUN:
                return False
            after_digits = True
            after_acronym = 0 < len(previous_letters) < _MIN_SPOKEN_RUN
            continue
        if after_digits and run[0].islower() and (after_acronym or len(run) < _MIN_SPOKEN_RUN):
            return False
        after_digits = False
        previous_letters = run
        if len(run) >= _MIN_SPOKEN_RUN:
            if run.isupper() or not _spoken_run(run):
                return False
            spoken = True
        elif len(run) == 3 and not any(ch in _VOWELS for ch in run):
            return False
    return spoken


def _joined_token(text: str) -> bool:
    """A segment of 3 or more parts joined by ``-`` or ``_`` that is an id as a
    whole: every part 2 to 6 characters mixing letters and digits
    (``ab12-cd34-ef56``), or one part all digits with a leading zero
    (``ORD-2024-0001``)."""
    parts = _JOIN_SPLIT_RE.split(text)
    if len(parts) < 3:
        return False
    if any(_LEADING_ZERO_RE.fullmatch(part) for part in parts):
        return True
    return all(
        2 <= len(part) <= 6 and _has_digit(part) and _has_letter(part) and part.isalnum()
        for part in parts
    )


def _part_holds_an_id(part: str) -> bool:
    if _DIGIT_RUN_RE.search(part):
        return True
    if len(part) >= _MIN_HEX_LEN and _HEX_RE.match(part):
        return True
    if _HEX_PART_RE.fullmatch(part) and _has_digit(part) and _has_letter(part):
        return True
    if _BASE64_PART_RE.fullmatch(part) and _has_digit(part):
        return True
    if (
        len(part) >= _MIN_INTERLEAVED_LEN
        and _letter_digit_transitions(part) >= _INTERLEAVED_SWITCHES
        and not _VERSION_RE.fullmatch(part)
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
    """True when *text*, a name or a path segment, carries an account value.

    The one owner of that decision: every position the digest reads a name or a
    segment from goes through it. *text* is percent-decoded, then read in order:

    1. The whole text is an email or a UUID; or a URL-safe base64-like token of 20
       or more characters holding a digit and switching between letters and digits
       at least twice (a random token; a long snake_case name with one trailing
       digit is not one).
    2. The whole text is 3 or more parts joined by ``-`` or ``_``, every part 2 to 6
       characters mixing letters and digits, or one part all digits with a leading
       zero (``ORD-2024-0001``).
    3. The whole text is a prefixed id: 2 to 8 letters, a separator, and a tail of 8
       or more characters mixing letters and digits that does not read as a word
       (``cus_NffrFeUfNV2Hib``).
    4. A part (split on ``_ . - ~``) holds a run of 5 or more digits, is 16 or more
       hex characters or 8 or more mixing hex digits and letters, is a base64-like
       token of 20 or more characters holding a digit, or is 8 or more characters
       switching between letters and digits 3 or more times (``x7kq29lp``).
    5. A part of 8 or more letters and digits mixed does not read as a word
       (:func:`_reads_as_a_word`): ``kqzpwmab47``, ``XKQ29LPZ``, ``Zq9XkLmPwR``.

    The rule is lexical. A random token with no digit reads as a word and is not
    caught, nor is a short word-like value; a camel name with a digit inside a word
    part (``apiV2Client``, ``getUser2FA``) may read as an id.
    """
    text = unquote(text)
    if not text:
        return False
    if _EMAIL_RE.match(text) or _UUID_RE.match(text):
        return True
    if (
        _WHOLE_BASE64_RE.fullmatch(text)
        and _has_digit(text)
        and _letter_digit_transitions(text) >= 2
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
