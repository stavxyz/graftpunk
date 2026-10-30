"""Pure path templating, no HAR involved.

The one rule for collapsing a dynamic path segment into a parameter, shared
by the digest's endpoint modelling and the fixtures/naming rule below it
(plugin tooling spec, 2026-09-11).
"""

from __future__ import annotations

import functools
import re
from collections.abc import Callable, Iterable
from urllib.parse import unquote, unquote_plus, urlsplit, urlunsplit

# The name rule's length thresholds (see holds_an_id): mixed hex, a prefixed id's
# tail, and a base64-like token.
_MIN_HEX_LEN = 12
_MIN_PREFIXED_TAIL = 12
_MIN_BASE64_LEN = 24

# A trailing "s" after one of these is part of the word, not a plural: naive
# stripping turned status/address/analysis/bus into statu_id, addres_id,
# analysi_id, bu_id. Not a general inflector, just the three letters that
# cover the shapes a URL path actually carries.
_SINGULAR_BEFORE_FINAL_S = frozenset("sui")

# An email address, matched against the percent-decoded segment: account data,
# so it collapses like an id and is masked in every URL the digest keeps. One
# shape, searched for rather than matched whole, so a segment or a name holding
# an email anywhere in it (a display-name form, two emails joined by a
# separator) is caught the same as a segment that is only an email.
_EMAIL_SHAPE = r"[^@\s/]+@[^@\s/]+\.[^@\s/.]+"
_EMAIL_SEARCH_RE = re.compile(_EMAIL_SHAPE)

__all__ = [
    "bare_host",
    "bare_path",
    "bare_url",
    "family_param_name",
    "holds_an_id",
    "is_placeholder",
    "keys_are_ids",
    "looks_dynamic",
    "normal_host",
    "param_name_for_segment",
    "reads_as_cased_words",
    "reads_as_words",
    "split_parts",
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


_DEFAULT_PORTS = {"http": "80", "https": "443"}


def normal_host(scheme: str, netloc: str) -> str:
    """*netloc* as :func:`bare_host` spells it, in lower case, without the default
    port of *scheme*: two spellings of one host compare equal."""
    host = bare_host(netloc).lower()
    port = _DEFAULT_PORTS.get(scheme.lower())
    if port and host.endswith(f":{port}"):
        host = host[: -len(port) - 1]
    return host


# holds_an_id's shapes: strong evidence only. A name read as an id is dropped from
# every generated command, so the rule catches only the shapes an account value
# takes as a key, header, input, cookie, or token name. A path segment fails closed
# on its own rule (looks_dynamic), so short random tokens are covered there.
_PART_SPLIT_RE = re.compile(r"[_.\-~$]")
_JOIN_SPLIT_RE = re.compile(r"[_\-]")
_MIN_DIGIT_RUN = 6
_DIGIT_RUN_RE = re.compile(rf"\d{{{_MIN_DIGIT_RUN},}}")
_MIXED_HEX_RE = re.compile(rf"[0-9a-fA-F]{{{_MIN_HEX_LEN},}}")
# A prefix of 2 to 8 letters starting the text or a _/- part, a separator, and a
# tail running to the end of the text or of a part.
_PREFIXED_ID_RE = re.compile(
    rf"(?:^|(?<=[_\-]))[A-Za-z]{{2,8}}[_.\-]([A-Za-z0-9]{{{_MIN_PREFIXED_TAIL},}})(?=$|[_.\-~$])"
)
_BASE64_RE = re.compile(rf"[A-Za-z0-9+/_\-]{{{_MIN_BASE64_LEN},}}=*")
_MIN_BASE64_SWITCHES = 5
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
# A card, national id, phone, or loyalty number written in digit groups: at least
# this many all-digit parts, holding at least this many digits together.
_MIN_DIGIT_GROUPS = 3
_MIN_GROUPED_DIGITS = 7
_PLACEHOLDER_SEGMENT_RE = re.compile(r"\{[A-Za-z0-9_]+\}")
# A phone number with its area code in parentheses: (555)123-4567, (512)555.0100.
_PARENTHESISED_PHONE_RE = re.compile(r"\(\d{3}\)\s?\d{3}[-.\s]?\d{4}")
# 0x and 12 or more hex digits: a wallet address, a hash, or a key id.
_PREFIXED_HEX_RE = re.compile(rf"0[xX][0-9a-fA-F]{{{_MIN_HEX_LEN},}}")


def _has_digit(text: str) -> bool:
    return any(ch.isdigit() for ch in text)


def _has_letter(text: str) -> bool:
    return any(ch.isalpha() for ch in text)


def _letter_digit_switches(text: str) -> int:
    """How many times *text* switches between a letter and a digit."""
    kinds = [ch.isdigit() for ch in text if ch.isalnum()]
    return sum(1 for before, after in zip(kinds, kinds[1:], strict=False) if before != after)


def split_parts(text: str) -> list[str]:
    """*text* split on ``_ . - ~ $``, the separators the path rule reads a segment's
    parts by, empty parts kept (``north--region`` gives an empty middle part)."""
    return _PART_SPLIT_RE.split(text)


def _parts(text: str) -> list[str]:
    return [part for part in split_parts(text) if part]


def _email(text: str) -> bool:
    return bool(_EMAIL_SEARCH_RE.search(text))


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


def _parenthesised_phone(text: str) -> bool:
    return bool(_PARENTHESISED_PHONE_RE.search(text))


def _prefixed_hex(text: str) -> bool:
    return any(_PREFIXED_HEX_RE.fullmatch(part) for part in _parts(text))


def _prefixed_id(text: str) -> bool:
    """A prefix of 2 to 8 letters, a separator, and a tail of 12 or more characters
    mixing upper case, lower case, and digits (``cus_NffrFeUfNV2Hib``), at the start
    of *text* or of any ``_``/``-`` part (``otp_cus_NffrFeUfNV2Hib``)."""
    return any(
        any(ch.isupper() for ch in tail) and any(ch.islower() for ch in tail) and _has_digit(tail)
        for tail in (match.group(1) for match in _PREFIXED_ID_RE.finditer(text))
    )


def _base64_token(text: str) -> bool:
    """The whole or a part is a base64-like token of 24 or more characters switching
    between letters and digits at least 5 times. The ruling's floor was 3; at 3 a
    long WebForms name (``ctl00_ContentPlaceHolder1_txtUserName``, 4 switches)
    reads as an id, and 5 changed no measured random-token rate."""
    return any(
        _BASE64_RE.fullmatch(candidate)
        and _letter_digit_switches(candidate) >= _MIN_BASE64_SWITCHES
        for candidate in (text, *_parts(text))
    )


# The name rule's sub-rules, each the only catch of at least one entry of the
# key-position id table (tests/unit/test_id_miss_rates.py drops each in turn). A
# UUID needs no rule of its own: its last group is 12 hex characters, caught as
# mixed hex, or as a digit run when it is all digits.
_NAME_ID_RULES: tuple[tuple[str, Callable[[str], bool]], ...] = (
    ("email", _email),
    ("digit run", _long_digit_run),
    ("mixed hex", _mixed_hex),
    ("digit groups", _digit_groups),
    ("parenthesised phone", _parenthesised_phone),
    ("0x hex", _prefixed_hex),
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

    - an email anywhere in the text (searched, not matched whole: a display-name
      form or two emails joined by a separator both count), or a UUID (by its
      12-character hex group);
    - a run of 6 or more digits (``user_40912873``);
    - hex of 12 or more characters mixing digits and letters (``a3f9c2d1e0b4``);
    - 3 or more all-digit parts totalling 7 or more digits (``4111-1111-1111-1111``,
      ``123-45-6789``; a date such as ``2024-01-15`` as a whole is not), or a phone
      number with its area code in parentheses (``(555)123-4567``);
    - ``0x`` and 12 or more hex digits (``0xdeadbeefcafe12``);
    - a prefixed id: 2 to 8 letters, a separator, and a tail of 12 or more
      characters mixing upper case, lower case, and digits (``cus_NffrFeUfNV2Hib``),
      at the start of the text or of any ``_``/``-`` part (``otp_cus_...``);
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


# A response object whose keys are all ids the name rule keeps (push ids, record
# ids): at least this many keys, all of one length at least this long, each an
# alphanumeric run (a leading "-" allowed) mixing letters and digits.
_MIN_ID_MAP_KEYS = 3
_MIN_ID_MAP_KEY_LEN = 12
_ID_MAP_KEY_RE = re.compile(r"-?[A-Za-z0-9]+")


def keys_are_ids(keys: Iterable[str]) -> bool:
    """True when *keys*, one response object's keys, are ids as a group: 3 or more,
    all one length of 12 or more, each an alphanumeric run (a leading ``-``
    allowed) mixing letters and digits (``-NqF7xYz3abcDEFghiJK``,
    ``recA1b2C3d4E5f6G7``), each reading as random (not a word with a short digit
    run, by the path rule's literal-part test), and not all one name with a
    different trailing number (``addressLine1``, ``addressLine2``, ...). A map-level
    rule for response keys only: one such name alone is a field name. Keys with an
    inner separator are names (``ctl00_MainContent_LoginUser_Password``)."""
    keys = list(keys)
    lengths = {len(key) for key in keys}
    if not (
        len(keys) >= _MIN_ID_MAP_KEYS
        and len(lengths) == 1
        and next(iter(lengths)) >= _MIN_ID_MAP_KEY_LEN
    ):
        return False
    # A numbered field group (addressLine1..3, streetLine01..03) differs only in a
    # trailing digit run: one field, numbered, not ids.
    if len({key.rstrip("0123456789") for key in keys}) == 1:
        return False
    return all(
        _ID_MAP_KEY_RE.fullmatch(key)
        and _has_digit(key)
        and _has_letter(key)
        and not _literal_part(key.lstrip("-"))
        for key in keys
    )


# A path segment's own checks, on top of the name rule: fail closed on any digit
# outside the literal shapes, and a few shapes a short route part can hide.
# The consonant pairs English words are spelled with (y counts as a vowel), the
# ones where two words meet included (backpack, webhook). A letter run holding any
# other pair (fb, kq, zh, ...) before a trailing digit reads as random.
_WORD_CONSONANT_PAIRS = frozenset(
    (  # noqa: SIM905 - one compact string on purpose: the set is data
        "bb bd bh bk bl bm bn bp br bs bt bv bw cb cc ch ck cl cm cn cr cs ct db dd dg dh "
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
_VERSION_RE = re.compile(r"v\d{1,2}(?:alpha|beta|rc)?\d{0,2}")
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
    """A part of a literal segment: no digit, a version, a lone digit run of at most
    2, or a spelled word with a digit run of at most 2 after it."""
    if not _has_digit(part):
        return True
    if _VERSION_RE.fullmatch(part) or (part.isdigit() and len(part) <= 2):
        return True
    match = _WORD_WITH_DIGITS_RE.fullmatch(part)
    return match is not None and _spelled(match.group(1))


def _literal_segment(text: str) -> bool:
    """True when every part of *text* is literal (:func:`_literal_part`) and a lone
    short digit run is the only part holding a digit: ``/page/2`` and ``step-2``
    stay literal, ``03-14-87``, ``10.0.0.1``, and ``acct-12-34`` do not."""
    parts = _parts(text)
    if not all(_literal_part(part) for part in parts):
        return False
    digit_parts = [part for part in parts if _has_digit(part)]
    return not (len(digit_parts) > 1 and any(part.isdigit() for part in digit_parts))


def _joined_token(text: str) -> bool:
    """A segment of 3 or more parts joined by ``-`` or ``_``, every part 2 to 6
    characters mixing letters and digits (``ab12-cd34-ef56``)."""
    parts = _JOIN_SPLIT_RE.split(text)
    return len(parts) >= 3 and all(
        2 <= len(part) <= 6 and _has_digit(part) and _has_letter(part) and part.isalnum()
        for part in parts
    )


_VALUE_PART_RE = re.compile(r"[^\W_]+")
# A run of letters longer than this is not read as a word in a cookie value: few
# words a form keeps are longer, and without the cap 1.8% of random 16-letter
# lowercase values read as words (measured with reads_as_words on 2026-10-19).
_MAX_VALUE_WORD_LEN = 15


def _word_cased(letters: str) -> bool:
    return letters.islower() or letters.isupper() or letters.istitle()


def _plain_value_part(part: str) -> bool:
    if not part.isascii():
        return False
    if part.isdigit():
        return True
    if part.isalpha():
        return len(part) <= _MAX_VALUE_WORD_LEN and _word_cased(part) and _spelled(part)
    if _VERSION_RE.fullmatch(part):
        return True
    match = _WORD_WITH_DIGITS_RE.fullmatch(part)
    return match is not None and _word_cased(match.group(1)) and _spelled(match.group(1))


def reads_as_words(value: str) -> bool:
    """True when *value*, a cookie value, is plain words, short numbers, or a date:
    the state a form or a preference keeps, not a credential.

    *value* is percent- and plus-decoded. It reads as words when it has at least
    one run of letters or digits and :func:`holds_an_id` does not hold it (so no
    run of 6 or more digits, no long hex, no base64-like token), and either

    - it has more than one whitespace-separated word, or a non-ASCII letter,
      :func:`holds_an_id` holds none of its words, and every word looks typed
      (:func:`_typed_word`: ``iPhone 15``, ``4K TV``, ``café``), or
    - every run of letters and digits in it is ASCII and is a number, a version
      (``v2``), or a word of at most 15 letters in one case shape (``shoes``,
      ``USD``, ``Monday``) spelled with the consonant pairs English uses,
      optionally followed by a digit run of at most 2.

    ``running shoes``, ``12``, ``2026-10-01``, ``10/01/2026``, and ``en-US`` read
    as words; a session id (``Zq9XkLmPwR4t7Bv2Hn8Ks3Jd``), a mixed-case run
    (``kQzPwM``), ``lot 123456``, and an empty value do not.

    The known limits: a number of 5 digits or fewer reads as words, so do groups
    of digits separated by spaces or ``+`` (``4111 1111 1111 1111``), so does any
    value made of space- or ``+``-separated pieces of 3 or fewer letters and
    digits with a letter among them (``aB3+xY7+qW2``), since each piece passes as
    a short typed word, and so can a short random value. Measured on 100,000
    random values per row (seed 273, 2026-10-19): of lowercase letters alone,
    14.8% at 8 letters, 5.3% at 12 and 2.4% at 15 read as words, and none at 16
    or more; of standard base64 (``+`` and ``/`` included, ``+`` decoded as a
    space), 0.77% at 8 characters, 0.04% at 12, about 0.001% at 16
    (``Wmx+akr4/CSG++n7``) and none at 22 or more; of base64url, 0.30% at 8 and
    none at 16 or more; of ``[a-z0-9]``, 1.96% at 8, 0.008% at 16 and none at 26.
    """
    text = unquote_plus(value)
    parts = _VALUE_PART_RE.findall(text)
    if not parts or holds_an_id(text):
        return False
    words = text.split()
    if len(words) > 1 or any(ch.isalpha() and not ch.isascii() for ch in text):
        # Several words or a non-ASCII letter mark typed text (``iPhone 15``,
        # ``café``), but plus-decoding also splits a standard-base64 id at each
        # ``+``, so every word must look typed on its own. A word of punctuation
        # alone counts as typed (``salt & pepper``) only beside a letter, so
        # digit pieces split by punctuation (``12+/+34``) are not words, while
        # numbers alone (``10 19 2026``, ``12 34``) are.
        has_punctuation_word = any(not any(ch.isalnum() for ch in word) for word in words)
        return (
            not any(holds_an_id(word) for word in words)
            and (not has_punctuation_word or any(ch.isalpha() for ch in text))
            and all(_typed_word(word) for word in words)
        )
    return all(_plain_value_part(part) for part in parts)


_TYPED_SHORT_WORD_LEN = 3
_BRAND_CASED_RE = re.compile(r"[a-z]?[A-Z]?[a-z]+")
# A quantity typed into a search box: a number and a unit from this list (512gb,
# 500ml, 12pk, 55inch), or dimensions (10x10). A fixed list, not any 1 to 4
# lowercase letters: that open shape read 0.85% of random 8-character
# standard-base64 ids as words against 0.77% without it; the list reads 0.77%
# (100,000 values, seed 273, 2026-10-19).
_QUANTITY_UNITS = (  # noqa: SIM905 - one compact string on purpose: the set is data
    "kb mb gb tb mah mm cm km in inch ft yd ml cl oz lb lbs kg mg g l pk ct pc pcs "
    "pack w kw v hz ghz mhz"
).split()
_QUANTITY_RE = re.compile(rf"\d{{1,4}}(?:{'|'.join(_QUANTITY_UNITS)})|\d{{1,3}}x\d{{1,3}}")


def _typed_word(word: str) -> bool:
    """True when *word*, one whitespace-separated word of a cookie value, looks
    typed: it holds no letter or digit (``&`` in ``salt & pepper``), every run of
    letters and digits in it passes the plain-value rule (``shoes``, ``USB-C``), it
    is at most 3 letters and digits (``4K``, ``TV``, ``2x4``, ``m3``), it is a
    quantity (``512gb``, ``55inch``, ``10x10``), it is spelled letters in one brand
    casing followed by at most 2 digits (``iPhone``, ``iPhone15``), or it is at
    most 15 characters holding a non-ASCII letter (``café``)."""
    if not any(ch.isalnum() for ch in word):
        return True
    runs = _VALUE_PART_RE.findall(word)
    if runs and all(_plain_value_part(run) for run in runs):
        return True
    if len(word) <= _TYPED_SHORT_WORD_LEN and word.isalnum():
        return True
    if _QUANTITY_RE.fullmatch(word):
        return True
    letters = word.rstrip("0123456789")
    if (
        len(word) - len(letters) <= 2
        and len(letters) > _TYPED_SHORT_WORD_LEN
        and _BRAND_CASED_RE.fullmatch(letters)
        and _spelled(letters)
    ):
        return True
    return len(word) <= _MAX_VALUE_WORD_LEN and any(
        ch.isalpha() and not ch.isascii() for ch in word
    )


# A camelCase value's case runs: a run of capitals or a run of lower case.
_CASE_RUN_RE = re.compile(r"[A-Z]+|[a-z]+")
_MIN_CAMEL_WORD_LEN = 3
_MIN_CAMEL_TITLE_LEN = 3
# The capitalised 2-letter parts accepted below that length: joining words a
# field name is built with (searchByNAME, sortOnDate).
_CAMEL_JOINING_WORDS = frozenset(("At", "By", "In", "Is", "Of", "On", "Or", "To", "Up"))
_MIN_CAMEL_CAPS_LEN = 2
# A run of capitals this short that ends a lower camelCase value is an acronym
# (viewPDF), accepted however it is spelled; any other run (NAME, LIST, HTML in
# parseHTMLString) must be spelled.
_MAX_UNSPELLED_CAPS = 3
# Fewer parts read fewer random mixed-case values as words; 3 holds every tab and
# field name shape seen (tab, LIST, results).
_MAX_CAMEL_PARTS = 3
_MIN_UPPER_CAMEL_LEN = 12
# A part spans at most 2 case runs (a capital and its lower case), so a value
# with more runs, or longer than its parts can hold, cannot read as camelCase.
_MAX_CAMEL_RUNS = 2 * _MAX_CAMEL_PARTS
_MAX_CAMEL_LEN = _MAX_CAMEL_PARTS * _MAX_VALUE_WORD_LEN
# A name in a records search: 2 to 4 words of letters, joined by spaces and the
# punctuation names carry (Smith, John; O'Brien Mary; ST. JOHN; Smith-Jones Ann).
_NAME_RE = re.compile(r"[A-Za-z]+(?:(?:, |\. |[ ,.'-])[A-Za-z]+){1,3}\.?")
_NAME_WORD_RE = re.compile(r"[A-Za-z]+")


def _camel_part(part: str, *, strict: bool = False, last: bool = False) -> bool:
    """One part of a camelCase reading: lower case or capitalised (at least 3
    letters), spelled and holding a vowel, a capitalised joining word (By, Of),
    or capitals (at least 2, spelled unless it is a trailing acronym of at most
    3, *last*, as in viewPDF). *strict*, for a value that starts with a capital,
    takes no joining word and spells every run of capitals."""
    if len(part) > _MAX_VALUE_WORD_LEN:
        return False
    if part.islower():
        return len(part) >= _MIN_CAMEL_WORD_LEN and _a_word(part)
    if part.isupper():
        return len(part) >= _MIN_CAMEL_CAPS_LEN and (
            (not strict and last and len(part) <= _MAX_UNSPELLED_CAPS) or _spelled(part.lower())
        )
    if not strict and part in _CAMEL_JOINING_WORDS:
        return True
    return part[1:].islower() and len(part) >= _MIN_CAMEL_TITLE_LEN and _a_word(part.lower())


def _a_word(letters: str) -> bool:
    """Lower-case *letters* spelled with the consonant pairs English uses and
    holding a vowel (y counts): a camelCase word part, where a random run of
    consonants (mdhsr) would otherwise pass."""
    return _spelled(letters) and any(ch in _VOWELS for ch in letters)


def _reads_as_camel_case(text: str) -> bool:
    """True when *text*, ASCII letters, splits into at most 3 camelCase parts
    (:func:`_camel_part`). A run of capitals followed by lower case is either its
    own part (tab, LIST, results) or gives its last capital to the next part
    (parse, HTML, String; HTML, Parser).

    The search is over run positions with a part budget, memoised on (position,
    parts left), so it takes time polynomial in the number of runs; the guards
    before it bound that number at 6 and the length at 45 letters. A value that
    starts with a capital (HTMLParserView, LISTviewResults) reads so only at 12
    letters or more and by the strict part rules: accepted at any length, that
    shape reads several times more random 8-letter mixed-case values as words."""
    strict = text[0].isupper()
    if strict and len(text) < _MIN_UPPER_CAMEL_LEN:
        return False
    if len(text) > _MAX_CAMEL_LEN:
        return False
    runs = _CASE_RUN_RE.findall(text)
    if len(runs) > _MAX_CAMEL_RUNS:
        return False

    def part(piece: str, *, last: bool = False) -> bool:
        return _camel_part(piece, strict=strict, last=last)

    @functools.cache
    def fits(index: int, budget: int) -> bool:
        if index == len(runs):
            return True
        if budget == 0:
            return False
        head = runs[index]
        if not (head.isupper() and index + 1 < len(runs)):
            return part(head, last=index + 1 == len(runs)) and fits(index + 1, budget - 1)
        lower = runs[index + 1]
        own_part = budget >= 2 and part(head) and part(lower) and fits(index + 2, budget - 2)
        if own_part:
            return True
        given, joined = head[:-1], head[-1] + lower
        if not given:
            return part(joined) and fits(index + 2, budget - 1)
        return budget >= 2 and part(given) and part(joined) and fits(index + 2, budget - 2)

    return fits(0, _MAX_CAMEL_PARTS)


def _reads_as_name(text: str) -> bool:
    """2 to 4 words joined by a space or a ``, ' . -`` (at least one space), each
    a capital initial or a word of at most 15 letters, capitalised or in
    capitals, spelled with the consonant pairs English uses."""
    if " " not in text or not _NAME_RE.fullmatch(text):
        return False
    return all(_name_word(word) for word in _NAME_WORD_RE.findall(text))


def _name_word(word: str) -> bool:
    if len(word) == 1:
        return word.isupper()
    return (
        len(word) <= _MAX_VALUE_WORD_LEN
        and (word.isupper() or word.istitle())
        and _spelled(word.lower())
    )


def reads_as_cased_words(value: str) -> bool:
    """True when *value*, a cookie value, is one ASCII word in capitals, a
    camelCase run of words, or a name of a few words: a party or a code a form
    keeps in capitals (``LANDOWNER``, ``GRANTEE``, ``BRAL``), a tab or field name
    (``currentTab``, ``panelGRIDview``, ``viewPDF``), or a name as a records
    search keeps it (``Smith, John``, ``ST. JOHN``, ``O'Brien Mary``).
    :func:`reads_as_words` rejects some of each (a run of 4 or more capitals, a
    word in brand casing); this test is for a caller that accepts them as form
    state.

    *value* is plus- and percent-decoded and ASCII. In capitals alone it is at
    most 15 letters spelled with the consonant pairs English uses. In camelCase
    it is letters alone, at most 45 of them in at most 6 case runs, and splits
    into at most 3 parts of at most 15 letters: lower-case or capitalised words
    of at least 3 letters, spelled so and holding a vowel (y counts), a
    capitalised joining word (``By``, ``Of``, ``To`` and a few more), or a run of
    at least 2 capitals, spelled so unless it ends the value and has at most 3
    letters (``viewPDF``). A run of capitals before lower case is read both as
    its own part (``tab``, ``LIST``, ``results``) and as giving its last capital
    to the next part (``parse``, ``HTML``, ``String``). A value that starts with
    a capital reads so only at 12 letters or more, with no joining word and every
    run of capitals spelled (``LISTviewResults``, ``HTMLParserView``). A name is
    2 to 4 words with a space among the joins, joined by a space, a comma, an
    apostrophe, a full stop or a hyphen, each word a capital initial or a word of
    at most 15 letters capitalised or in capitals and spelled so (``Smith, JOHN
    Q.``). ``iPhone`` (a first part of 1 letter), ``CurrentTab`` and
    ``HTMLParser`` (a capital first, under 12 letters), ``tabXQZresults`` (an
    unspelled run inside), ``currentTab2`` (a digit) and ``kQzPwMxT`` do not
    read so.

    ``scripts/cookie_value_rates.py`` prints, per shape of random value, the
    share a cookie rule using this test (``flagged_names_of``) flags, against
    :func:`reads_as_words` alone. On its 100,000 values per shape (seed 273,
    2026-10-24) the rule no longer flags 0.67% of 8 mixed-case letters, 0.20% of
    12 and 0.011% of 16, 0.13% of 8 standard-base64 characters and 0.016% of 12,
    and none of hex, lowercase letters, UUIDs, JWTs or numbers. Shorter and
    biased shapes lose more: 1.9% of 6 mixed-case letters. Random capitals are
    its known limit, since only their letter pairs tell them from a word: it
    accepts 42% of 4 capitals, 19% of 7, 11% of 9 and 5.2% of 12, the rates
    :func:`reads_as_words` has accepted for lowercase letters of those lengths
    since 1.23.0, and 15% of 8 characters of capitals and spaces.
    An id of capitals and digits is accepted only when a draw holds no
    digit: 1.1% at 8 characters, 0.09% at 12 and none at 16.
    """
    text = unquote_plus(value)
    if not text.isascii() or not text:
        return False
    if not text.isalpha():
        return _reads_as_name(text)
    if text.isupper():
        return len(text) <= _MAX_VALUE_WORD_LEN and _spelled(text.lower())
    return _reads_as_camel_case(text)


def is_placeholder(segment: str) -> bool:
    """True when *segment* is a ``{name}`` placeholder, as ``template_path`` and
    ``bare_url``'s email masking write one."""
    return bool(_PLACEHOLDER_SEGMENT_RE.fullmatch(segment))


def _is_email(segment: str) -> bool:
    return bool(_EMAIL_SEARCH_RE.search(unquote(segment)))


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
    if _other_scheme(parts.scheme) and _opaque_holds_an_email(url):
        # A URL of another scheme has no segments to mask one by one, and an email
        # anywhere in it (one argument of several) would be printed whole.
        return f"{parts.scheme}:{{id}}"
    path = _masked_emails(bare_path(parts.path))
    return urlunsplit((parts.scheme, bare_host(parts.netloc), path, "", ""))


def looks_dynamic(segment: str) -> bool:
    """True when *segment* is a path segment that collapses into a named parameter.

    Fails closed: a segment is dynamic when :func:`holds_an_id` says so (an email, a
    long number, ...) or it is 3 or more short letter-and-digit parts joined by
    ``-`` or ``_`` (``ab12-cd34-ef56``), and a segment holding a digit is literal
    only when every part of it (split on ``_ . - ~ $``) holds no digit, is a word
    spelled with the consonant pairs English uses and no run of 4 or more capitals,
    followed by a digit run of at most 2 (``address2``, ``windows10``, ``ec2``), is
    a version of at most 2 digits (``v2``, ``v1beta1``; not ``v40912``), or is a
    digit run of at most 2 standing alone, and a lone digit run is the only part
    holding a digit (``/page/2`` and ``step-2`` stay literal; ``03-14-87``,
    ``10.0.0.1``, and ``acct-12-34`` do not). Every other segment holding a digit is
    dynamic: a date, a card, phone, or national id number in digit groups, a long
    number, a mixed token (``kqzpwmab47``, ``MAPLETON7``). The known limits: a
    letters-only segment that holds no id stays literal, and so does a word with one
    or two digits after it (``smith42``, ``mary12``); the digest's high-cardinality
    collapse templates a family of many digit-bearing siblings.

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
    return _joined_token(text) or not _literal_segment(text)


def param_name_for_segment(prev_segment: str) -> str:
    """The parameter name for a segment collapsed at ``prev_segment``'s position.

    The singular of the preceding literal segment (``orders`` -> ``order_id``),
    or the bare ``id`` when there is no preceding segment or it is itself a
    collapsed parameter (``{order_id}``).
    """
    if not prev_segment or prev_segment.startswith("{"):
        return "id"
    return f"{_singular(prev_segment)}_id"


def _singular(word: str) -> str:
    """*word* with a plural's final ``s`` taken off (``orders`` -> ``order``), kept
    after an ``s``, ``u``, or ``i`` (``status``, ``bus``, ``analysis``)."""
    is_plural = word.endswith("s") and len(word) > 1 and word[-2] not in _SINGULAR_BEFORE_FINAL_S
    return word[:-1] if is_plural else word


_PLAIN_WORD_RE = re.compile(r"[A-Za-z]+")

# Plurals ending in "ies" whose singular is not "y": the same word in the plural,
# and words whose singular ends in "ie" (the final "s" alone comes off).
_SAME_IN_THE_PLURAL = frozenset({"series", "species"})
_IE_PLURALS = frozenset(
    {
        "brownies",
        "calories",
        "cookies",
        "freebies",
        "goalies",
        "lies",
        "movies",
        "newbies",
        "pies",
        "rookies",
        "selfies",
        "smoothies",
        "ties",
        "zombies",
    }
)


def family_param_name(prev_segment: str, taken: Iterable[str] = ()) -> str:
    """The placeholder name for a family's varying segment, the one at
    *prev_segment*'s right (graftpunk.har.digest's family rule).

    A recording cannot know what the segment names, so the name is neutral and
    stable, for the author to rename: the singular of *prev_segment* plus
    ``_slug`` when *prev_segment* is a plain word of ASCII letters
    (``categories`` -> ``category_slug``, ``regions`` -> ``region_slug``; a final
    ``ies`` becomes ``y``, which ``param_name_for_segment`` keeps as it always has,
    except in ``series`` and ``species``, which keep their spelling, and in a few
    words whose singular ends in ``ie``, such as ``movies``), else ``slug`` (no
    segment before it, a placeholder, or a segment with a digit or a separator). When
    that name is in *taken* (the template's other placeholder names), a number from 2
    up is appended (``slug2``).
    """
    lowered = prev_segment.lower()
    if not _PLAIN_WORD_RE.fullmatch(prev_segment):
        base = "slug"
    elif lowered in _SAME_IN_THE_PLURAL:
        base = f"{prev_segment}_slug"
    elif lowered.endswith("ies") and len(prev_segment) > 3 and lowered not in _IE_PLURALS:
        base = f"{prev_segment[:-3]}y_slug"
    else:
        base = f"{_singular(prev_segment)}_slug"
    taken = set(taken)
    name, suffix = base, 2
    while name in taken:
        name, suffix = f"{base}{suffix}", suffix + 1
    return name


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


def _other_scheme(scheme: str) -> bool:
    """True when *scheme* (lower case, as urlsplit gives it) is present and names
    neither ``http`` nor ``https``."""
    return bool(scheme) and scheme not in ("http", "https")


_OPAQUE_RUN_RE = re.compile(r"[\w.-]+")


def _opaque_holds_an_id(url: str) -> bool:
    """True when the text after the scheme of *url*, a URL of another scheme than
    ``http`` or ``https``, holds an id or a token: some ``[\\w.-]+`` run of it is
    dynamic under the path rule (:func:`looks_dynamic`). It fails closed: a search
    of 200,000 random runs found none the name rule (:func:`holds_an_id`) reads as
    an id that it does not, so the name rule is not consulted.
    ``javascript:login('f3a9c2e1b7d4a6f0e2c8b1d9')`` and ``javascript:go(12345678)``
    do; ``javascript:void(0)`` does not. The runs split an email at its ``@``, so the
    whole text is also searched for one (:func:`_opaque_holds_an_email`)."""
    opaque = url.partition(":")[2]
    return _opaque_holds_an_email(url) or any(
        looks_dynamic(run) for run in _OPAQUE_RUN_RE.findall(opaque)
    )


def _opaque_holds_an_email(url: str) -> bool:
    """True when the text after the scheme of *url*, percent-decoded, holds an email
    anywhere (``javascript:go('alice@example.com', 'ref')``), by the one email shape
    a path segment is judged by."""
    return bool(_EMAIL_SEARCH_RE.search(unquote(url.partition(":")[2])))


def templates_a_segment(url: str) -> bool:
    """True when :func:`template_path` turns a segment of *url*'s path into a
    parameter, so the path holds an id or a token.

    Compared path to path (templated against :func:`bare_path`), never whole
    strings: :func:`templated_url` gives a URL with no path a ``/``, which is not an
    account value. A segment :func:`bare_url` already masked (an email, now
    ``{user_id}``) counts too. A URL of another scheme than ``http`` or ``https``
    has no path to template, so it counts only when its text holds an id or a
    token (:func:`_opaque_holds_an_id`): ``javascript:go(12345678)`` does, and
    ``javascript:void(0)`` does not.
    """
    parts = urlsplit(url)
    if _other_scheme(parts.scheme):
        return _opaque_holds_an_id(url)
    path = bare_path(parts.path)
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
    to the page itself). A URL of another scheme than ``http`` or ``https`` has no
    path to template: it is returned unchanged (``javascript:void(0)``), or as its
    scheme and ``{id}`` when its text holds an id or a token
    (:func:`_opaque_holds_an_id`), which is never printed.
    """
    parts = urlsplit(url)
    if _other_scheme(parts.scheme):
        return f"{parts.scheme}:{{id}}" if _opaque_holds_an_id(url) else url
    path = parts.path or ("/" if parts.netloc else "")
    template = template_path(path)[0] if path else ""
    return urlunsplit((parts.scheme, bare_host(parts.netloc), template, "", ""))
