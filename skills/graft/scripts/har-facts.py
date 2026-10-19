"""Answer a question about a recording's HAR with names, attributes, shapes, and counts.

Usage:
  har-facts.py cookies HAR
  har-facts.py request HAR URL_PART [--method METHOD]
  har-facts.py response HAR URL_PART [--method METHOD]

A HAR holds the account's data and, from a recorded login, the password the user
typed. The graft skill reads one through this script so that no value from it
reaches the skill's context. The script prints recorded text only when it passes
one allowlist gate, ``printable_name``: a field name of at most 48 characters made
of words joined by ``_``, ``-``, ``.``, brackets, or camel case, each word spelled
with the consonant pairs English uses (graftpunk.har.paths's spelling rule,
copied here and held equal by a test), a short or capitalised word holding a
vowel, capitals only as a known acronym or a name all in capitals, digits only as
a run of at most 2 ending a word, and nothing graftpunk's own id rule
(``graftpunk.har.paths.holds_an_id``, copied here as ``holds_an_id`` and held
equal by a test) reads as an id. A path segment must also hold no run of more
than three capitals. Every other piece of recorded text is replaced or counted,
never printed:

- a URL prints as its scheme, a host of hostname characters, and its path with
  each segment that fails the gate written ``{id}`` (digits or an id shape),
  ``{token}`` (16 characters or more), or ``{}``; a URL of a scheme other than
  http, https, ws, or wss prints as that scheme alone;
- a query string prints its keys that pass the gate, never a value, and counts
  the rest; a query or form body with no ``=`` is an unnamed blob of N bytes;
- a JSON key, a form, multipart, or ``params`` field name, an HTML control's
  name or id, a header name, and a cookie name print only when they pass, and
  are otherwise counted as names that read as values;
- an HTML page's title prints as its length class only, never its text, and a
  control's type prints only when it is a standard input type;
- a cookie's ``Path`` prints templated like a URL path, its ``Domain`` only when
  it is the setting response's host or a parent of it, and its ``SameSite`` only
  as ``Lax``, ``Strict``, or ``None``;
- a value's type and a string's length class (empty, short up to 16 characters,
  medium up to 64, long) print, never the value.

The gate cannot tell a word that is also a value from a field name: a username
used as a JSON key or a path segment (``jsmith``), or a name all in capitals
used as a key, reads as a name and prints. Random tokens of 12 characters or
more, hex, UUIDs, emails, JWT pieces, and prefixed ids fail it at least 99.9% of
the time (the test suite measures 10,000 samples of each); random tokens of 8
characters fail it 99.69% (base64url) to 99.98% (hex) of the time (10,000 samples
each, seed 277, 2026-10-19).

``cookies`` prints one block per cookie a response sets: each response that sets
it (method and templated URL) with a count, and each setting's ``HttpOnly``,
``Secure``, ``SameSite``, ``Path``, ``Domain``, and whether it expires, read from
the raw ``Set-Cookie`` header, every line of a newline-joined value, parsed as
graftpunk's own HAR parser does (``set_cookie_lines``, held equal by a test), and
from the entry's ``response.cookies`` array, paired by position within a name; a
line says when the two disagree. Then it names the cookies the recording only
sends.

``request`` and ``response`` match URL_PART against each entry's own request URL
path, as recorded or templated (never a ``Referer`` or ``Location`` header), and
``--method`` narrows by method. ``request`` prints the method, the URL with its
query keys, the request header and cookie names, and the body's content type and
field names with each value's type. ``response`` prints the status, the content
type, the response header names, the names of the cookies it sets, and the
body's shape: JSON keys and types, or an HTML page's title length and its forms,
each with its method, templated action, and controls by name and type (repeats
collapsed with a count, an unnamed control by type and position). A field whose
name reads as a password, a secret, a token, a card, or a key is listed by name
alone. A response body streamed to ``bodies/`` is read only from the HAR's own
``bodies/`` directory.

At most 20 matching entries are described; past that, a count prints. Exit 0 when
something was described, 1 when nothing matched, 2 on a usage error, a HAR that
is missing or is not a HAR, or an entry that could not be read; no error message
quotes the HAR. Standard library only, so it runs under any Python 3.9 or later.
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import sys
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

_MAX_ENTRIES = 20
_MAX_KEYS = 40
_MAX_DEPTH = 4
_MAX_NAME_LEN = 48
_LINE_BREAK_RE = re.compile(r"\r\n|\r|\n")

# graftpunk.har.paths.holds_an_id, copied: the script cannot import graftpunk, and
# a test holds the two equal.
_MIN_HEX_LEN = 12
_MIN_PREFIXED_TAIL = 12
_MIN_BASE64_LEN = 24
_EMAIL_SEARCH_RE = re.compile(r"[^@\s/]+@[^@\s/]+\.[^@\s/.]+")
_PART_SPLIT_RE = re.compile(r"[_.\-~$]")
_DIGIT_RUN_RE = re.compile(r"\d{6,}")
_MIXED_HEX_RE = re.compile(rf"[0-9a-fA-F]{{{_MIN_HEX_LEN},}}")
_PREFIXED_ID_RE = re.compile(
    rf"(?:^|(?<=[_\-]))[A-Za-z]{{2,8}}[_.\-]([A-Za-z0-9]{{{_MIN_PREFIXED_TAIL},}})(?=$|[_.\-~$])"
)
_BASE64_RE = re.compile(rf"[A-Za-z0-9+/_\-]{{{_MIN_BASE64_LEN},}}=*")
_MIN_BASE64_SWITCHES = 5
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_PARENTHESISED_PHONE_RE = re.compile(r"\(\d{3}\)\s?\d{3}[-.\s]?\d{4}")
_PREFIXED_HEX_RE = re.compile(rf"0[xX][0-9a-fA-F]{{{_MIN_HEX_LEN},}}")


def _has_digit(text: str) -> bool:
    return any(ch.isdigit() for ch in text)


def _has_letter(text: str) -> bool:
    return any(ch.isalpha() for ch in text)


def _parts(text: str) -> list[str]:
    return [part for part in _PART_SPLIT_RE.split(text) if part]


def _letter_digit_switches(text: str) -> int:
    kinds = [ch.isdigit() for ch in text if ch.isalnum()]
    # No strict=: the script runs on Python 3.9, which lacks it.
    return sum(1 for before, after in zip(kinds, kinds[1:]) if before != after)  # noqa: B905


def _mixed_hex(text: str) -> bool:
    return any(
        _MIXED_HEX_RE.fullmatch(part) and _has_digit(part) and _has_letter(part)
        for part in _parts(text)
    )


def _digit_groups(text: str) -> bool:
    groups = [part for part in _parts(text) if part.isascii() and part.isdigit()]
    return len(groups) >= 3 and sum(len(g) for g in groups) >= 7 and not _DATE_RE.fullmatch(text)


def _prefixed_id(text: str) -> bool:
    return any(
        any(ch.isupper() for ch in tail) and any(ch.islower() for ch in tail) and _has_digit(tail)
        for tail in (match.group(1) for match in _PREFIXED_ID_RE.finditer(text))
    )


def _base64_token(text: str) -> bool:
    return any(
        _BASE64_RE.fullmatch(candidate)
        and _letter_digit_switches(candidate) >= _MIN_BASE64_SWITCHES
        for candidate in (text, *_parts(text))
    )


def holds_an_id(text: str) -> bool:
    """Whether *text*, a name, carries an account value: graftpunk's rule."""
    text = unquote(text)
    if not text:
        return False
    return (
        bool(_EMAIL_SEARCH_RE.search(text))
        or bool(_DIGIT_RUN_RE.search(text))
        or _mixed_hex(text)
        or _digit_groups(text)
        or bool(_PARENTHESISED_PHONE_RE.search(text))
        or any(_PREFIXED_HEX_RE.fullmatch(part) for part in _parts(text))
        or _prefixed_id(text)
        or _base64_token(text)
    )


# graftpunk.har.paths's spelling rule, copied (a test holds the pair set equal):
# the consonant pairs English words are spelled with, y counting as a vowel.
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
_VOWELS = frozenset("aeiouy")


def _spelled(letters: str) -> bool:
    """Whether lower-case *letters* use only consonant pairs English words use."""
    return all(
        a in _VOWELS or b in _VOWELS or a + b in _WORD_CONSONANT_PAIRS
        for a, b in zip(letters, letters[1:])  # noqa: B905 - Python 3.9 has no strict=
    )


# The gate's own shape. A name is parts joined by _ - . [ ]; each part is a short
# number, capitals (PHPSESSID), or words in lower or camel case (startDate,
# XMLHttpRequest) with a digit run of at most 2 at its end (name1). Every word is
# spelled; a short lower-case word, and every capitalised word, also holds a
# vowel; a capitals run inside camel case is a known acronym. Random tokens break
# into short unspelled or vowel-less words and fail.
_NAME_CHARS_RE = re.compile(r"[A-Za-z0-9_.\-\[\]]+")
_NAME_SPLIT_RE = re.compile(r"[_.\-\[\]]+")
_CAMEL_WORD_RE = re.compile(r"[A-Z]{2,5}(?=[A-Z][a-z]|$)|[A-Z]?[a-z]+")
_ACRONYMS = frozenset(
    (  # noqa: SIM905 - one compact string on purpose: the set is data
        "ID IDS URL URI API XML HTML JSON CSRF XSRF UUID IP UI SSO MFA OTP SMS PDF CSV UTC "
        "OS DOB SSN ZIP CVV CVC TOS GDPR HTTP HTTPS JWT SAML AJAX DOM CSS JS ISO VAT SKU"
    ).split()
)
# A capitalised word of two letters in camel case must be one of these (userId).
_SHORT_WORDS = frozenset(
    "Id By At To Of On In Is Up No Or Ok Me My Do Go If As Us Tv Pm Am".split()  # noqa: SIM905
)
_MAX_PARTS = 8
_MAX_CAPITALS = 16
_LONG_LOWER = 9


def _lower_word(word: str) -> bool:
    if word.upper() in _ACRONYMS:
        return True
    if len(word) >= _LONG_LOWER:
        return any(ch in _VOWELS for ch in word)
    return _spelled(word) and (len(word) <= 3 or any(ch in _VOWELS for ch in word))


def _part_reads_as_words(part: str) -> bool:
    letters = part.rstrip("0123456789")
    digits = len(part) - len(letters)
    if not letters:
        return 1 <= digits <= 2
    if digits > 2 or not letters.isalpha():
        return False
    if letters.isupper():
        if letters in _ACRONYMS or len(letters) == 1:
            return True
        lowered = letters.lower()
        if len(letters) <= _MAX_CAPITALS and len(letters) >= _LONG_LOWER:
            return _lower_word(lowered)
        return (
            len(letters) <= _MAX_CAPITALS
            and _spelled(lowered)
            and (len(letters) <= 2 or any(ch in _VOWELS for ch in lowered))
        )
    words = _CAMEL_WORD_RE.findall(letters)
    if "".join(words) != letters:
        return False
    if len(words) == 1 and words[0].islower():
        return _lower_word(words[0])
    # Random text splits into many short words; a camel name holds at most one
    # word of three letters or fewer that is not a listed short word or acronym.
    short = [w for w in words if len(w) <= 3 and w not in _SHORT_WORDS and w not in _ACRONYMS]
    if len(short) > 1:
        return False
    for word in words:
        lowered = word.lower()
        if word.isupper():
            if word not in _ACRONYMS:
                return False
        elif len(word) == 2 and word[0].isupper():
            if word not in _SHORT_WORDS:
                return False
        elif len(word) < 2 or not _spelled(lowered) or not any(ch in _VOWELS for ch in lowered):
            return False
    return True


def printable_name(text: str) -> bool:
    """The one output gate: whether recorded *text* may be printed as a name."""
    if not text or len(text) > _MAX_NAME_LEN or not _NAME_CHARS_RE.fullmatch(text):
        return False
    parts = [part for part in _NAME_SPLIT_RE.split(text) if part]
    if not parts or len(parts) > _MAX_PARTS or not any(_has_letter(part) for part in parts):
        return False
    if not all(_part_reads_as_words(part) for part in parts):
        return False
    # Capitals beside lower case (uid_SAMCOOKIE, addr[ADDRKEY]) read as a value
    # dropped into a name; a name all in capitals (PHPSESSID, XSRF-TOKEN) does not.
    if any(_long_capitals(part) for part in parts) and not all(
        part.rstrip("0123456789").isupper() or part.isdigit() for part in parts
    ):
        return False
    return not holds_an_id(text)


def _long_capitals(part: str) -> bool:
    letters = part.rstrip("0123456789")
    return letters.isupper() and len(letters) > 3 and letters not in _ACRONYMS


def _printable_segment(segment: str) -> bool:
    """A path segment prints when it passes the gate and holds no run of capitals
    longer than an acronym: routes are words, and capitals in one are a code."""
    text = unquote(segment)
    parts = [part for part in _NAME_SPLIT_RE.split(text) if part]
    return printable_name(text) and not any(_long_capitals(part) for part in parts)


def _placeholder(segment: str) -> str:
    text = unquote(segment)
    if text.isdigit() or holds_an_id(text):
        return "{id}"
    return "{token}" if len(text) >= 16 else "{}"


def templated_path(path: str) -> str:
    """*path* with each segment that fails the gate replaced by a placeholder."""
    segments = path.split("/")
    out = [seg if not seg or _printable_segment(seg) else _placeholder(seg) for seg in segments]
    return "/".join(out)


_HOST_RE = re.compile(r"[a-z0-9.\-]{1,253}|\[[0-9a-f:.]{2,45}\]")
_WEB_SCHEMES = ("http", "https", "ws", "wss")
_SCHEME_RE = re.compile(r"[a-z][a-z0-9+.\-]{0,15}")


def _scheme(scheme: str) -> str:
    return f"{scheme}:" if _SCHEME_RE.fullmatch(scheme) else "a URL of another scheme"


def templated_url(url: str) -> str:
    """*url* as scheme, host, and templated path; never a query, fragment, or userinfo."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return "a URL that does not parse"
    scheme = parts.scheme.lower()
    if scheme and scheme not in _WEB_SCHEMES:
        return _scheme(scheme)
    path = templated_path(parts.path)
    if not scheme:
        return path or "(this page)"
    host = (parts.hostname or "").lower()
    host = host if _HOST_RE.fullmatch(host) else "{host}"
    try:
        port = parts.port
    except ValueError:
        port = None
    return f"{scheme}://{host}{f':{port}' if port else ''}{path}"


def _query_text(url: str) -> str:
    try:
        return urlsplit(url).query
    except ValueError:
        return ""


def _pairs(text: str) -> tuple[list[tuple[str, str]], int]:
    """``name=value`` pairs of an ``&``-joined text, and how many pieces held no ``=``."""
    pairs: list[tuple[str, str]] = []
    unnamed = 0
    for piece in text.split("&"):
        if not piece:
            continue
        name, sep, value = piece.partition("=")
        if not sep:
            unnamed += 1
            continue
        pairs.append((unquote(name.replace("+", " ")), unquote(value.replace("+", " "))))
    return pairs, unnamed


def _blob(text: str) -> str:
    size = len(text.encode("utf-8", "replace"))
    return f"an unnamed blob of {size} byte{'' if size == 1 else 's'}"


def query_keys(url: str) -> str:
    query = _query_text(url)
    if not query:
        return ""
    if "=" not in query:
        return f" (query: {_blob(query)})"
    pairs, unnamed = _pairs(query)
    names = list(dict.fromkeys(name for name, _v in pairs))
    shown = [name for name in names if printable_name(name)]
    notes = []
    if len(names) > len(shown):
        notes.append(_value_names(len(names) - len(shown)))
    if unnamed:
        notes.append(f"{unnamed} piece{'' if unnamed == 1 else 's'} with no name")
    tail = f" ({'; '.join(notes)})" if notes else ""
    return f"?{'&'.join(shown)}{tail}" if shown else f" (query: {'; '.join(notes)})"


def _value_names(count: int, noun: str = "name") -> str:
    if count == 1:
        return f"1 {noun} that reads as a value, not shown"
    return f"{count} {noun}s that read as values, not shown"


# Set-Cookie


def set_cookie_lines(headers: list[Any]) -> list[tuple[str, dict[str, str]]]:
    """``(name, attributes)`` for every cookie a ``Set-Cookie`` header in *headers*
    sets, in order, by graftpunk.har.parser._set_cookie_lines's parse (a test holds
    the two equal): each line of a header value is one cookie, its name is before
    the first ``=`` of the part before the first ``;``, and its attributes are the
    ``;``-separated parts after it, keyed by lower-cased attribute name. The
    cookie's value is dropped here and never returned."""
    cookies: list[tuple[str, dict[str, str]]] = []
    for header in headers:
        if not isinstance(header, dict) or str(header.get("name", "")).lower() != "set-cookie":
            continue
        for line in _LINE_BREAK_RE.split(str(header.get("value", ""))):
            pair, *parts = line.split(";")
            if "=" not in pair:
                continue
            name = pair.split("=", 1)[0].strip()
            if not name:
                continue
            attributes: dict[str, str] = {}
            for part in parts:
                key, _sep, value = part.partition("=")
                key = key.strip().lower()
                if key:
                    attributes[key] = value.strip()
            cookies.append((name, attributes))
    return cookies


# Secret-looking names


_WORD_RE = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|[0-9]+")
_SECRET_WORDS = frozenset(
    (  # noqa: SIM905 - one compact string on purpose: the set is data
        "pass passwd password passcode passphrase pw pwd pin otp mfa totp secret token "
        "auth authorization credential credentials key apikey sig signature session sid csrf xsrf "
        "card cc cvv cvc csc pan iban ssn"
    ).split()
)
_SECRET_SUBSTRINGS = ("password", "passwd", "secret", "token", "apikey", "cardnumber", "credential")


def looks_secret(name: str) -> bool:
    """Whether a field *name* reads as a password, secret, token, card, or key."""
    lowered = name.lower()
    if any(part in lowered for part in _SECRET_SUBSTRINGS):
        return True
    return any(word.lower() in _SECRET_WORDS for word in _WORD_RE.findall(name))


# Loading


class HarError(Exception):
    """The HAR cannot be read; the message says why without quoting it."""


def _load(path: str) -> list[dict[str, Any]]:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise HarError(f"{path} is not UTF-8 text") from exc
    except OSError as exc:
        raise HarError(f"cannot read {path}") from exc
    try:
        har = json.loads(text)
    except (ValueError, RecursionError) as exc:
        raise HarError(f"{path} is not JSON") from exc
    log = har.get("log") if isinstance(har, dict) else None
    entries = log.get("entries") if isinstance(log, dict) else None
    if not isinstance(entries, list):
        raise HarError(f"{path} has no log.entries list, so it is not a HAR")
    return [entry for entry in entries if isinstance(entry, dict)]


def _part(entry: dict[str, Any], key: str) -> dict[str, Any]:
    value = entry.get(key)
    return value if isinstance(value, dict) else {}


def _list(container: dict[str, Any], key: str) -> list[Any]:
    value = container.get(key)
    return value if isinstance(value, list) else []


_METHOD_RE = re.compile(r"[A-Z]{1,10}")


def _method(request: dict[str, Any]) -> str:
    method = str(request.get("method", "")).upper()
    return method if _METHOD_RE.fullmatch(method) else "(method not shown)"


def _names(raw: list[str]) -> str:
    """*raw* names, deduplicated, through the gate, with the rest counted."""
    names = list(dict.fromkeys(name for name in raw if name))
    shown = [name for name in names if printable_name(name)]
    hidden = len(names) - len(shown)
    pieces = [", ".join(shown)] if shown else []
    if hidden:
        pieces.append(_value_names(hidden))
    return "; ".join(pieces) or "(none)"


def _header_names(headers: list[Any]) -> list[str]:
    return [str(h.get("name", "")) for h in headers if isinstance(h, dict)]


def _header(headers: list[Any], name: str) -> str:
    for header in headers:
        if isinstance(header, dict) and str(header.get("name", "")).lower() == name:
            return str(header.get("value", ""))
    return ""


def _request_cookie_names(request: dict[str, Any]) -> list[str]:
    names = [str(c.get("name", "")) for c in _list(request, "cookies") if isinstance(c, dict)]
    for header in _list(request, "headers"):
        if isinstance(header, dict) and str(header.get("name", "")).lower() == "cookie":
            for fragment in str(header.get("value", "")).split(";"):
                name, sep, _value = fragment.partition("=")
                if sep:
                    names.append(name.strip())
    return list(dict.fromkeys(name for name in names if name))


_MIME_RE = re.compile(r"[a-z0-9][a-z0-9.+\-]{0,63}/[a-z0-9][a-z0-9.+\-]{0,63}")


def _mime(raw: str) -> str:
    base = raw.split(";", 1)[0].strip().lower()
    if not base:
        return ""
    return base if _MIME_RE.fullmatch(base) else "a content type that is not shown"


# cookies


def _cookie_host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def _domain(domain: str, host: str) -> str:
    if not domain:
        return "-"
    bare = domain.lower().lstrip(".")
    if host and (host == bare or host.endswith("." + bare)) and _HOST_RE.fullmatch(bare):
        return bare
    return "a domain outside the response's host"


def _same_site(value: str) -> str:
    if not value:
        return "-"
    known = {"lax": "Lax", "strict": "Strict", "none": "None"}
    return known.get(value.strip().lower(), "an unknown value")


def _cookie_path(value: str) -> str:
    return templated_path(value) if value.startswith("/") else ("-" if not value else "{}")


def _header_view(attributes: dict[str, str], host: str) -> dict[str, str]:
    return {
        "HttpOnly": "yes" if "httponly" in attributes else "no",
        "Secure": "yes" if "secure" in attributes else "no",
        "SameSite": _same_site(attributes.get("samesite", "")),
        "Path": _cookie_path(attributes.get("path", "")),
        "Domain": _domain(attributes.get("domain", ""), host),
        "expiry": "yes" if ("expires" in attributes or "max-age" in attributes) else "no",
    }


def _array_view(cookie: dict[str, Any], host: str) -> dict[str, str]:
    return {
        "HttpOnly": "yes" if cookie.get("httpOnly") is True else "no",
        "Secure": "yes" if cookie.get("secure") is True else "no",
        "SameSite": _same_site(str(cookie.get("sameSite") or "")),
        "Path": _cookie_path(str(cookie.get("path") or "")),
        "Domain": _domain(str(cookie.get("domain") or ""), host),
        "expiry": "yes" if cookie.get("expires") else "no",
    }


def _describe(view: dict[str, str]) -> str:
    return " ".join(f"{key}={value}" for key, value in view.items())


def cookies(entries: list[dict[str, Any]]) -> list[str]:
    settings: dict[str, Counter[tuple[str, str]]] = {}
    hidden_set: set[str] = set()
    sent: dict[str, None] = {}
    for entry in entries:
        request, response = _part(entry, "request"), _part(entry, "response")
        for name in _request_cookie_names(request):
            sent[name] = None
        url = str(request.get("url", ""))
        host = _cookie_host(url)
        where = f"{_method(request)} {templated_url(url)}"
        array: dict[str, list[dict[str, Any]]] = {}
        for cookie in _list(response, "cookies"):
            if isinstance(cookie, dict) and cookie.get("name"):
                array.setdefault(str(cookie["name"]), []).append(cookie)
        seen: Counter[str] = Counter()
        for name, attributes in set_cookie_lines(_list(response, "headers")):
            view = _header_view(attributes, host)
            text = f"header: {_describe(view)}"
            matches = array.get(name, [])
            if seen[name] < len(matches):
                other = _array_view(matches[seen[name]], host)
                differ = [
                    f"{key} is {other[key]} there"
                    for key in ("HttpOnly", "Secure", "Path", "Domain")
                    if other[key] != view[key]
                    and (key in ("HttpOnly", "Secure") or other[key] != "-")
                ]
                if differ:
                    text += f"; the response.cookies array differs: {', '.join(differ)}"
            else:
                text += "; not in the response.cookies array"
            seen[name] += 1
            if not printable_name(name):
                hidden_set.add(name)
                continue
            settings.setdefault(name, Counter())[(where, text)] += 1
        for name, cookies_of_name in array.items():
            for cookie in cookies_of_name[seen[name] :]:
                if not printable_name(name):
                    hidden_set.add(name)
                    continue
                view = _describe(_array_view(cookie, host))
                text = f"response.cookies array only (no Set-Cookie header line): {view}"
                settings.setdefault(name, Counter())[(where, text)] += 1
    lines: list[str] = []
    for name in sorted(settings):
        lines.append(f"cookie {name}")
        for (where, text), count in settings[name].items():
            times = "1 time" if count == 1 else f"{count} times"
            lines.append(f"  set by {where} ({times}); {text}")
    if hidden_set:
        lines.append(f"set: {_value_names(len(hidden_set), 'cookie name')}")
    only_sent = [name for name in sent if name not in settings and name not in hidden_set]
    if only_sent:
        lines.append(
            f"sent in requests and never set in this recording: {_names(sorted(only_sent))}"
        )
    return lines


# shapes


def _length_class(text: str) -> str:
    if not text:
        return "empty"
    if len(text) <= 16:
        return "short"
    return "medium" if len(text) <= 64 else "long"


def _type(value: Any) -> str:
    if isinstance(value, bool):
        return "bool"
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return f"string ({_length_class(value)})"
    if isinstance(value, dict):
        return "object"
    return "array"


def _count(value: Any) -> str:
    if isinstance(value, (list, dict)):
        noun = "item" if isinstance(value, list) else "key"
        return f" of {len(value)} {noun}{'' if len(value) == 1 else 's'}"
    return ""


def _field(name: str, detail: str) -> str:
    return (
        f"{name}: (name reads as a secret; not described)"
        if looks_secret(name)
        else f"{name}: {detail}"
    )


def _shape(value: Any, indent: str, depth: int) -> list[str]:
    """Lines describing *value*'s keys and types, never a value."""
    lines: list[str] = []
    if isinstance(value, dict):
        keys = [key for key in value if printable_name(str(key))]
        hidden = len(value) - len(keys)
        for key in keys[:_MAX_KEYS]:
            child = value[key]
            lines.append(f"{indent}{_field(str(key), _type(child) + _count(child))}")
            if (
                not looks_secret(str(key))
                and isinstance(child, (dict, list))
                and depth < _MAX_DEPTH
            ):
                lines.extend(_shape(child, indent + "  ", depth + 1))
        if len(keys) > _MAX_KEYS:
            lines.append(f"{indent}({len(keys) - _MAX_KEYS} more keys)")
        if hidden:
            lines.append(f"{indent}({_value_names(hidden, 'key')})")
    elif isinstance(value, list) and value:
        first = value[0]
        lines.append(f"{indent}[first item]: {_type(first)}{_count(first)}")
        if isinstance(first, (dict, list)) and depth < _MAX_DEPTH:
            lines.extend(_shape(first, indent + "  ", depth + 1))
    return lines


def _json_shape(text: str, indent: str) -> list[str] | None:
    try:
        value = json.loads(text)
    except (ValueError, RecursionError):
        return None
    try:
        return [f"{indent}JSON {_type(value)}{_count(value)}", *_shape(value, indent + "  ", 1)]
    except RecursionError:
        return None


def _named_fields(names: list[str], types: dict[str, str], indent: str) -> list[str]:
    """One line per printable name with its type and a repeat count; the rest counted."""
    counts = Counter(names)
    lines: list[str] = []
    hidden = 0
    for name in dict.fromkeys(names):
        if not printable_name(name):
            hidden += 1
            continue
        repeat = f" (sent {counts[name]} times)" if counts[name] > 1 else ""
        lines.append(f"{indent}{_field(name, types.get(name, 'string'))}{repeat}")
    if hidden:
        lines.append(f"{indent}({_value_names(hidden)})")
    return lines


def _form_shape(text: str, indent: str) -> list[str]:
    if "=" not in text:
        return [f"{indent}{_blob(text)}"]
    pairs, unnamed = _pairs(text)
    types: dict[str, str] = {}
    for name, value in pairs:
        types.setdefault(name, f"string ({_length_class(value)})")
    lines = _named_fields([name for name, _v in pairs], types, indent)
    if unnamed:
        lines.append(f"{indent}({unnamed} piece{'' if unnamed == 1 else 's'} with no name)")
    return lines or [f"{indent}(no fields)"]


def _multipart(text: str, indent: str) -> list[str]:
    names = re.findall(r'Content-Disposition:[^\r\n]*?\bname="([^"]*)"', text, re.I)
    files = set(
        re.findall(r'Content-Disposition:[^\r\n]*?\bname="([^"]*)"[^\r\n]*?\bfilename=', text, re.I)
    )
    types = {name: ("file" if name in files else "string") for name in names}
    lines = _named_fields(names, types, indent)
    return [f"{indent}multipart fields:", *lines] if lines else [f"{indent}multipart, no fields"]


_INPUT_TYPES = frozenset(
    (  # noqa: SIM905 - one compact string on purpose: the set is data
        "text password email hidden checkbox radio submit button number tel url search date "
        "datetime-local month week time file image reset range color"
    ).split()
)
_FORM_METHODS = ("GET", "POST", "DIALOG")


class _PageReader(HTMLParser):
    """An HTML page's title length and its forms' controls, never their text."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.svg_depth = 0
        self.in_title = False
        self.title_seen = False
        self.title: list[str] = []
        self.forms: list[dict[str, Any]] = []
        self.loose: dict[str, Any] = {"controls": []}
        self.current: dict[str, Any] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        found = {key: value or "" for key, value in attrs}
        if tag == "svg":
            self.svg_depth += 1
        elif tag == "title" and not self.svg_depth and not self.title_seen:
            self.in_title = True
            self.title_seen = True
        elif tag == "form":
            method = found.get("method", "get").upper()
            action = templated_url(found["action"]) if found.get("action") else "(this page)"
            self.current = {
                "method": method if method in _FORM_METHODS else "(method not shown)",
                "action": action,
                "controls": [],
            }
            self.forms.append(self.current)
        elif tag in ("input", "select", "textarea", "button"):
            if tag == "input":
                kind = found.get("type", "text").lower() or "text"
                kind = kind if kind in _INPUT_TYPES else "an input of another type"
            elif tag == "button":
                kind = found.get("type", "submit").lower() or "submit"
                kind = f"{kind} button" if kind in ("submit", "button", "reset") else "button"
            else:
                kind = tag
            target = self.current if self.current is not None else self.loose
            target["controls"].append((found.get("name", ""), kind, found.get("id", "")))

    def handle_endtag(self, tag: str) -> None:
        if tag == "form":
            self.current = None
        elif tag == "svg" and self.svg_depth:
            self.svg_depth -= 1
        elif tag == "title":
            self.in_title = False

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.title.append(data)


def _controls(controls: list[tuple[str, str, str]], indent: str) -> list[str]:
    lines: list[str] = []
    named = [(name, kind) for name, kind, _id in controls if name]
    counts = Counter(named)
    hidden = 0
    for name, kind in dict.fromkeys(named):
        if not printable_name(name):
            hidden += 1
            continue
        repeat = f" ({counts[(name, kind)]} controls)" if counts[(name, kind)] > 1 else ""
        lines.append(f"{indent}{name}: {kind}{repeat}")
    for position, (name, kind, ident) in enumerate(controls, start=1):
        if name:
            continue
        shown = f", id {ident}" if ident and printable_name(ident) else ""
        hidden_id = ", an id that reads as a value" if ident and not shown else ""
        lines.append(f"{indent}(no name) {kind}, control {position} of the form{shown}{hidden_id}")
    if hidden:
        lines.append(f"{indent}({_value_names(hidden)})")
    return lines


_TOKEN_NAME_RE = re.compile(r"token|csrf|xsrf|nonce", re.I)


def _html_shape(text: str, indent: str) -> list[str]:
    reader = _PageReader()
    try:
        reader.feed(text)
        reader.close()
    except Exception:  # noqa: BLE001 - an unreadable page is described as such
        return [f"{indent}HTML that does not parse"]
    title = " ".join("".join(reader.title).split())
    title_text = f"a {_length_class(title)} title" if reader.title_seen and title else "no title"
    lines = [f"{indent}HTML page with {title_text}"]
    groups = [(f"form {f['method']} {f['action']}", f["controls"]) for f in reader.forms]
    if reader.loose["controls"]:
        groups.append(("controls outside any form", reader.loose["controls"]))
    for label, controls in groups:
        notes = []
        if any(kind == "password" for _n, kind, _i in controls):
            notes.append("holds a password input")
        secret_named = [
            name
            for name, kind, _i in controls
            if name and kind != "password" and printable_name(name) and looks_secret(name)
        ]
        if any(_TOKEN_NAME_RE.search(name) for name in secret_named):
            notes.append("holds a token-named field")
        if any(not _TOKEN_NAME_RE.search(name) for name in secret_named):
            notes.append("holds a field named like a secret")
        note = " (" + "; ".join(notes) + ")" if notes else ""
        lines.append(f"{indent}{label}{note}")
        lines.extend(_controls(controls, indent + "  "))
    return lines


def _body_lines(mime: str, text: str | None, indent: str) -> list[str]:
    if text is None:
        return [f"{indent}no body text recorded"]
    if not text:
        return [f"{indent}empty"]
    if "json" in mime or text.lstrip()[:1] in ("{", "["):
        shape = _json_shape(text, indent)
        if shape is not None:
            return shape
    if "x-www-form-urlencoded" in mime:
        return _form_shape(text, indent)
    if "multipart/form-data" in mime:
        return _multipart(text, indent)
    if "html" in mime:
        return _html_shape(text, indent)
    return [f"{indent}{len(text)} characters of {mime or 'an unnamed type'}, not parsed"]


# request and response


def _path_of(url: str) -> str:
    try:
        return urlsplit(url).path
    except ValueError:
        return url.split("?", 1)[0].split("#", 1)[0]


def _matches(
    entries: list[dict[str, Any]], needle: str, method: str | None
) -> list[dict[str, Any]]:
    found = []
    for entry in entries:
        request = _part(entry, "request")
        url = str(request.get("url", ""))
        path = _path_of(url)
        if (
            needle not in path
            and needle not in templated_path(path)
            and needle not in url.split("?", 1)[0].split("#", 1)[0]
        ):
            continue
        if method and str(request.get("method", "")).upper() != method.upper():
            continue
        found.append(entry)
    return found


def describe_request(entry: dict[str, Any]) -> list[str]:
    request = _part(entry, "request")
    url = str(request.get("url", ""))
    lines = [f"{_method(request)} {templated_url(url)}{query_keys(url)}"]
    headers = _list(request, "headers")
    lines.append(f"  request headers: {_names(_header_names(headers))}")
    lines.append(f"  request cookies: {_names(_request_cookie_names(request))}")
    post = request.get("postData")
    if not isinstance(post, dict):
        lines.append("  body: none recorded")
        return lines
    mime = _mime(str(post.get("mimeType") or _header(headers, "content-type")))
    lines.append(f"  body: {mime or 'no content type'}")
    params = [p for p in _list(post, "params") if isinstance(p, dict)]
    text = post.get("text")
    if params and not isinstance(text, str):
        lines.extend(_named_fields([str(p.get("name", "")) for p in params], {}, "    "))
        return lines
    lines.extend(_body_lines(mime, text if isinstance(text, str) else None, "    "))
    return lines


def _response_text(entry: dict[str, Any], har_path: Path) -> tuple[str | None, str]:
    """The response body's text, or None with the reason."""
    content = _part(_part(entry, "response"), "content")
    body_file = content.get("_bodyFile")
    if isinstance(body_file, str) and body_file:
        bodies = (har_path.parent / "bodies").resolve()
        target = (har_path.parent / body_file).resolve()
        if bodies not in target.parents:
            return None, "a body file outside the recording, not read"
        try:
            return target.read_text(encoding="utf-8"), ""
        except UnicodeDecodeError:
            return None, "a body that is not text"
        except OSError:
            return None, "a body file that cannot be read"
    text = content.get("text")
    if not isinstance(text, str):
        return None, "no body text recorded"
    if content.get("encoding") == "base64":
        try:
            return base64.b64decode(text).decode("utf-8"), ""
        except (ValueError, UnicodeDecodeError):
            return None, "a body that is not text"
    return text, ""


def describe_response(entry: dict[str, Any], har_path: Path) -> list[str]:
    request, response = _part(entry, "request"), _part(entry, "response")
    lines = [f"{_method(request)} {templated_url(str(request.get('url', '')))}"]
    content = _part(response, "content")
    headers = _list(response, "headers")
    mime = _mime(str(content.get("mimeType") or _header(headers, "content-type")))
    status = response.get("status")
    status_text = str(status) if isinstance(status, int) and not isinstance(status, bool) else "?"
    lines.append(f"  status {status_text}, content type {mime or '(none)'}")
    lines.append(f"  response headers: {_names(_header_names(headers))}")
    names = [name for name, _a in set_cookie_lines(headers)]
    names += [
        str(c.get("name"))
        for c in _list(response, "cookies")
        if isinstance(c, dict) and c.get("name")
    ]
    lines.append(f"  sets cookies: {_names(names)}")
    text, reason = _response_text(entry, har_path)
    lines.append("  body:")
    if text is None:
        lines.append(f"    {reason}")
    else:
        lines.extend(_body_lines(mime, text, "    "))
    return lines


def main(argv: list[str]) -> int:
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(errors="backslashreplace")
    parser = argparse.ArgumentParser(prog="har-facts.py", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("cookies").add_argument("har")
    for name in ("request", "response"):
        one = sub.add_parser(name)
        one.add_argument("har")
        one.add_argument("url_part")
        one.add_argument("--method")
    try:
        args = parser.parse_args(argv[1:])
    except SystemExit as exc:
        return 2 if exc.code else 0
    try:
        entries = _load(args.har)
    except HarError as exc:
        print(f"har-facts: {exc}.", file=sys.stderr)
        return 2
    try:
        return _run(args, entries)
    except Exception as exc:  # noqa: BLE001 - never echo HAR text in a traceback
        print(f"har-facts: could not read an entry ({type(exc).__name__}).", file=sys.stderr)
        return 2


def _run(args: argparse.Namespace, entries: list[dict[str, Any]]) -> int:
    if args.command == "cookies":
        lines = cookies(entries)
        if not lines:
            print(
                "har-facts: no response in this HAR sets a cookie, and no request sends one.",
                file=sys.stderr,
            )
            return 1
        print("\n".join(lines))
        return 0
    found = _matches(entries, args.url_part, args.method)
    if not found:
        method = f" with method {args.method.upper()}" if args.method else ""
        print(f"har-facts: no request URL holds {args.url_part!r}{method}.", file=sys.stderr)
        return 1
    blocks = []
    for entry in found[:_MAX_ENTRIES]:
        if args.command == "request":
            blocks.append("\n".join(describe_request(entry)))
        else:
            blocks.append("\n".join(describe_response(entry, Path(args.har))))
    print("\n\n".join(blocks))
    if len(found) > _MAX_ENTRIES:
        more = len(found) - _MAX_ENTRIES
        print(f"\n({more} more entries match; narrow the URL part or add --method)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
