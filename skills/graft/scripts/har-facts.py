"""Answer a question about a recording's HAR with names, attributes, shapes, and counts.

Usage:
  har-facts.py cookies HAR [--name NAME]
  har-facts.py request HAR URL_PART [--method METHOD] [--recorded-path]
  har-facts.py response HAR URL_PART [--method METHOD] [--recorded-path]
  har-facts.py operations HAR [--operation NAME]

``request`` and ``response`` also take ``--operation NAME``.

A HAR holds the account's data and, from a recorded login, the session's cookies
and tokens, and it can hold the password the user typed. The graft skill reads one
through this script. What the script guarantees:
it prints no cookie, header, query, or body value. The method prints only when it
is a standard HTTP method, a URL scheme other than http, https, ws, and wss only
when it is ``mailto``, ``javascript``, ``data``, ``tel``, ``blob``, or ``about``,
and a content type only when its top-level type is a standard one, its subtype a
known one or one whose name passes the gate below (a vendor, personal, or ``x-``
tree prefix aside), and otherwise as ``{}`` with a known suffix kept
(``application/{}+json``). A host prints as recorded when it is an IP address;
a host name keeps its last two labels, the site's own domain, and each label
left of them prints only when it passes the gate (``www`` and a few other fixed
labels aside), else as ``{id}`` or ``{}``. Names and path segments are
recorded text too, and those print only when they pass a heuristic allowlist
gate, ``printable_name``, which withholds most value-shaped text but not all of
it. A name passes when it is a fixed framework name (below), or at most 48
characters of words joined by ``_``, ``-``, ``.``, ``$``, brackets, or camel case,
with every word spelled with the consonant pairs English uses (graftpunk's
``har.paths`` spelling rule), a short or capitalised word holding a vowel,
capitals only as a known acronym or in a name all in capitals (an acronym of
three letters or more also passes in lower or capitalised case inside camel
case, so ``Csrf``, ``xsrfToken``, and ``JwtToken`` pass, and a lone ``X`` may
stand before a word, as in ``XCsrf``, and an ``F``, ``L``, or ``M`` before
``Name``, as in ``FName``), digits only as a run of at most 2 ending a word or
in a known token (``sha256``, ``a11y``, ``e164``, ``s3`` starting a name), and
nothing graftpunk's ``holds_an_id`` reads as an id. The spelling pairs and rule,
and ``holds_an_id``, are copied here (the script cannot import graftpunk) and
held equal to graftpunk's by tests. A path segment must also hold no run of
more than three capitals outside a listed acronym, and be one graftpunk's
``looks_dynamic`` keeps, so no segment prints that the digest's key replaces.

The fixed framework names pass by exact name: standard, CDN, and bot-manager
header names (``Upgrade-Insecure-Requests``, ``WWW-Authenticate``, ``cf-ray``,
``x-amz-cf-id``), framework cookie names (``.ASPXAUTH``, ``ASP.NET_SessionId``,
``__cf_bm``, ``cf_clearance``, ``_abck``, ``bm_sz``, ``_fbp``, ``_gcl_au``),
ASP.NET WebForms page state (``__VIEWSTATE``, ``__EVENTVALIDATION``), and route
segments (``graphql``, ``jsonrpc``, ``xmlrpc.php``, ``cgi-bin``,
``eFiling``). A cookie name built from a framework's fixed prefix and a per-site
suffix (``_ga_``, ``_hjSession_``, ``_hjSessionUser_``, ``AMCV_``, ``AMCVS_``,
``_gat_gtag_UA_``, ``.AspNetCore.Antiforgery.``, ``ASPSESSIONID``) goes through
the gate whole, like any name, and when it fails prints as the prefix and ``{}``
in a cookie list. A shape a framework fixes passes when its other pieces pass
the gate: a WebForms control (``ctl00$MainContent$txtUserName``, ``btnLogin``,
``pwdConfirm``, and its element id ``ctl00_MainContent_txtUserName``, judged as
the ``$``-joined name is), a Django
formset field (``<prefix>-<index>-<field>``, the index at most 2 digits) or
management field (``<prefix>-TOTAL_FORMS`` and the other three), the prefix and
the field gated and the prefix gated unless it is 1 to 4 lower-case letters
(``bvp``), and a server page (``Login.aspx``, ``index.php``).

What fails the gate is replaced or counted:

- a URL prints as its scheme, its host as above, and its path with
  each segment that fails written ``{id}`` (digits or an id shape), ``{token}``
  (16 characters or more), or ``{}``; a URL of a scheme other than http, https,
  ws, or wss prints as that scheme alone, or as ``a URL of another scheme``;
- a query string prints its keys that pass, never a value, and counts the rest;
  a query or form body with no ``=`` is an unnamed blob of N bytes;
- a JSON key, a form, multipart, or ``params`` field name, a header name, and a
  cookie name print only when they pass, and are otherwise counted as names that
  read as values; an HTML control whose name fails keeps its type and position;
- an HTML page's title prints as its length class only, and a control's type
  prints only when it is a standard input type;
- a cookie's ``Path`` prints templated like a URL path (``(not shown)`` when it
  holds anything outside a plain path, such as a query), its ``Domain`` only when
  it is the setting response's host or a parent of it, and its ``SameSite`` only
  as ``Lax``, ``Strict``, or ``None``;
- a value's type and a string's length class (empty, short up to 16 characters,
  medium up to 64, long) print, never the value.

What the gate lets through. A plain word that is also a value cannot be told
from a field name: a username, a person's or place's name, a label, or a handle
with a two-digit suffix, used as a JSON key, a path segment, a subdomain, or a
piece of a content type's subtype, prints (``alice``, ``Mapleton``,
``main-street``, ``alice84``). A letter-only random token passes
when it happens to spell like a word. Measured on 10,000 samples per row (seed 277,
2026-10-19), the share of random values that pass: none of base62 at 12, 16, or
32 characters, hex at 16 or 40, base64url at 16, 22, or 43, base36 at 32, JWT
pieces, emails, prefixed ids, or UUIDs; 0.17% of base62 at 8, 0.38% of base64url
at 8, 0.05% of hex at 8, and 0.25% of base36 at 12; and of letter-only tokens,
lower or upper case, 14.59% at 8 letters, 10.84% at 9, 5.20% at 12, and 1.97% at
16.

``cookies`` prints one block per cookie a response sets: each response that sets
it (method and templated URL) with a count, and each setting's ``HttpOnly``,
``Secure``, ``SameSite``, ``Path``, ``Domain``, and ``expiry``, read from the
raw ``Set-Cookie`` header, every line of a newline-joined value, parsed as
graftpunk's own HAR parser does (``set_cookie_lines``, held equal by a test), and
from the entry's ``response.cookies`` array, paired by position within a name; a
line says when the two disagree. The words are the markdown digest's, held
equal to it by a test: ``yes``, ``no``, or ``unknown`` for a flag (an attribute
the array leaves out is ``unknown``); ``Lax``, ``Strict``, ``None``, ``-`` (not
set), or ``unknown`` (empty, bare, or not recognised) for ``SameSite``; ``-``
(not set or empty), ``unknown`` (not starting with ``/``), or ``(not shown)``
for a ``Path``; and ``session``, ``persistent``, ``past`` (``Max-Age`` 0 or
less, or an ``Expires`` or array ``expires`` no later than the request: the
response deletes the cookie), or ``unknown`` (not a number or a date, or an
entry whose ``startedDateTime`` does not parse) for ``expiry``. Then it names
the cookies the recording only sends. With ``--name``, it prints only the block
of that exact name, whatever the gate says, since the caller already holds the
name; its exit status says whether the recording holds that name, so a guessed
name is answered: pass only a name already in hand.

``request`` and ``response`` match URL_PART against each entry's own request URL
as this script prints it (never a ``Referer`` or ``Location`` header), and
``--method`` narrows by the method as it prints, so a match never says whether
text the script holds back is in the recording. ``--recorded-path`` also matches
URL_PART against the URL's path as recorded (never its userinfo, host, query, or
fragment): a hit then says the recording holds URL_PART, so pass only text
already in hand. ``request`` prints the method, the URL with its query keys, the
request header and cookie names, and the body's content type and field names
with each value's type. ``response`` prints the status, the content type, the
response header names, the names of the cookies it sets, and the body's shape:
JSON keys and types (JSON nested deeper than 64 levels is not parsed), or an
HTML page's title length and its forms, each with its method, templated action,
and controls by name and type (repeats collapsed with a count, a control with no
name or a name that fails by type, position, and an id that passes, and hidden
ones of those with no such id as one line with a count). A field whose name
reads as a password, a secret, a token, a card, or a key is listed by name
alone. A response body streamed to ``bodies/`` is read only from the HAR's own
``bodies/`` directory, never through a link.

``operations`` lists each GraphQL operation the recording's request bodies carry
(graftpunk's own rule, copied and held equal by a test: a JSON object with a
``query`` that reads as a GraphQL document, or an ``operationName`` beside
``extensions.persistedQuery``; each element of an array of them): its method,
URL (its path as the digest keys it, with a segment the key keeps and the gate
holds back written ``{}``), and name in parentheses (``(anonymous)`` for none),
the number of calls and how many came in a batch, its kind (``query``,
``mutation``, ``subscription``, or ``persisted`` for a hash sent without a
document), and the shapes of its first recorded variables and response, nested
keys and types, never a value. An operation name prints when it is a GraphQL
Name of at most 128 characters that ``holds_an_id`` does not read as an id; it
is an identifier fixed in the site's client code, the same for every account, so
it does not pass through ``printable_name``, and any other name is filed under
``(anonymous)``. ``operations`` ends with ``shared values (names only)``: each
pair of a response field and a later request's variable that carried the same
value (text of three characters or more, never a bool or null), as ``<Operation>
<response path> -> <Operation> variables.<path>`` with a request count, so the
links between operations (an id one returns and another takes) show without any
value; a key that fails the gate is written ``{}``. ``--operation NAME`` narrows
``operations``, ``request``, and ``response`` to one operation (``anonymous``
names the anonymous one), and ``request`` and ``response`` add the operations an
entry carries to its first line.

At most 20 matching entries are described; past that, a count prints. Exit 0 when
something was described, 1 when nothing matched, 2 on a usage error, a HAR that
is missing, is not a HAR, or nests JSON deeper than 512 levels (checked before
it is parsed), or an entry that could not be read; no error message quotes the
HAR. Standard library only, so it runs under any Python 3.9 or later.
"""

from __future__ import annotations

import argparse
import base64
import ipaddress
import json
import re
import sys
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse, urlsplit

_MAX_ENTRIES = 20
_MAX_KEYS = 40
_MAX_DEPTH = 4
# An operation's variables and response are read deeper: a GraphQL response nests.
_OPERATION_DEPTH = 10
_MAX_NAME_LEN = 48
_LINE_BREAK_RE = re.compile(r"\r\n|\r|\n")

# graftpunk.har.paths.holds_an_id, copied: the script cannot import graftpunk, and
# a test holds the two equal.
_MIN_HEX_LEN = 12
_MIN_PREFIXED_TAIL = 12
_MIN_BASE64_LEN = 24
# From each "@" with the one character before it: a search from every start
# was quadratic in a long run with no "@" (#304).
_EMAIL_SEARCH_RE = re.compile(r"(?<=[^@\s/])@[^@\s/]+\.[^@\s/.]+")
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


# graftpunk.har.paths's spelling rule, copied (a test holds the pair set and the
# rule equal): the consonant pairs English words are spelled with, y counting as a
# vowel, and no run of more than 3 capitals.
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
_MAX_CAPITALS_RUN = 3


def _spelled(letters: str) -> bool:
    """Whether *letters* use only consonant pairs English words use and are not a
    run of more than 3 capitals."""
    if len(letters) > _MAX_CAPITALS_RUN and letters.isupper():
        return False
    # bj is not a pair English words use, but in "object" and "subject", which name
    # many fields (objectId, subject); allowing bj everywhere passed more random
    # text (#361), and "bject" almost never occurs in it.
    lowered = letters.lower().replace("bject", "ject")
    return all(
        a in _VOWELS or b in _VOWELS or a + b in _WORD_CONSONANT_PAIRS
        for a, b in zip(lowered, lowered[1:])  # noqa: B905 - Python 3.9 has no strict=
    )


# The gate's own shape. A name is parts joined by _ - . [ ]; each part is a short
# number, capitals (PHPSESSID), or words in lower or camel case (startDate,
# XMLHttpRequest) with a digit run of at most 2 at its end (name1). Every word is
# spelled; a short lower-case word, and every capitalised word, also holds a
# vowel, unless it is a known acronym of three letters or more in any case (Csrf,
# xsrfToken); a capitals run inside camel case is a known acronym. Random tokens break
# into short unspelled or vowel-less words and fail.
_NAME_CHARS_RE = re.compile(r"[A-Za-z0-9_.$\-\[\]]+")
_NAME_SPLIT_RE = re.compile(r"[_.$\-\[\]]+")
# A lone capital before a capitalised word (XCsrf) is a word of its own, which
# must then be an acronym.
_CAMEL_WORD_RE = re.compile(r"[A-Z]{2,5}(?=[A-Z][a-z]|$)|[A-Z](?=[A-Z][a-z])|[A-Z]?[a-z]+")
_ACRONYMS = frozenset(
    (  # noqa: SIM905 - one compact string on purpose: the set is data
        "ID IDS URL URI API XML HTML JSON CSRF XSRF UUID IP UI SSO MFA OTP SMS PDF CSV UTC "
        "OS DOB SSN ZIP CVV CVC TOS GDPR HTTP HTTPS JWT SAML AJAX DOM CSS JS ISO VAT SKU "
        # the abbreviations cookie and token names use, some spelled with no vowel
        "X SID JSID SSID SESS AUTH OIDC NONCE TOTP HOTP TFA JTI PKCE HMAC TKN IDP NBF "
        # the vowel-less abbreviations API and form field names and routes use
        # (invoiceNbr, txn_id, txtQty, msrp, /svc/), and QL as in GraphQL
        "NBR TXN QTY MSRP SVC QL "
        # the abbreviations header names use (X-Amz-Target, Grpc-Timeout,
        # HX-Request, Ocp-Apim-Trace, Sec-GPC, X-DNS-Prefetch-Control, X-PJAX), cloud
        # SDKs' among them (X-Amz-Sdk-Checksum-Algorithm, X-Amz-Sns-Message-Type,
        # X-Amz-Apigw-Id, X-Firebase-GMPID, X-MSEdge-Ref)
        "AMZ AMZN GRPC HX OCP GPC DPR DNS CDN PJAX SDK SNS APIGW GMPID MS"
    ).split()
)
# An acronym this long or longer reads as one in any case inside camel case
# (Csrf, xsrfToken, JwtToken); a shorter one (Js, Os) stays capitals only.
_MIN_CASELESS_ACRONYM_LEN = 3
# A capitalised word of two letters in camel case must be one of these (userId).
_SHORT_WORDS = frozenset(
    "Id By At To Of On In Is Up No Or Ok Me My Do Go If As Us Tv Pm Am Ip Ms".split()  # noqa: SIM905
)
_MAX_PARTS = 8
_MAX_CAPITALS = 16
_LONG_LOWER = 9


def _camel_acronym(word: str) -> bool:
    if word.isupper():
        return word in _ACRONYMS
    return len(word) >= _MIN_CASELESS_ACRONYM_LEN and word.upper() in _ACRONYMS


def _lower_word(word: str) -> bool:
    if word.upper() in _ACRONYMS:
        return True
    if len(word) >= _LONG_LOWER:
        return _spelled(word) and any(ch in _VOWELS for ch in word)
    return _spelled(word) and (len(word) <= 3 or any(ch in _VOWELS for ch in word))


# A name with a digit run inside a part (line2Address, oauth2Token, k8sNamespace):
# the part splits into a known token that carries digits, matched in any case where
# a word starts (Sha256, base64, x509), or a version starting the part
# (v2Endpoint), and letter runs and digit runs. Every letter
# run reads as words (_plain_part) and is not a run of capitals other than a known
# acronym. A digit run of one or two digits sits between words of four letters or
# more: after one, and before a capitalised one (line2Address, covid19Status), the
# end, an x after a 2 or a 3 (retina2x), or, after a lone 2 read as a word
# (added2cart), a lowercase one. As in camel case, at most one short word
# that is not a listed short word or acronym. A version (v1beta1) passes whole.
# Random text with a digit inside rarely puts a long spelled word on both sides.
_DIGIT_TOKENS = tuple(
    sorted(
        (  # noqa: SIM905 - one compact string on purpose: the set is data
            "argon2id argon2 ed25519 pbkdf2 oauth1 oauth2 base32 base64 utf8 utf16 ipv4 "
            "ipv6 x509 k8s md5 sha1 sha224 sha256 sha384 sha512 i18n l10n a11y h264 h265 "
            "e164"
        ).split(),
        key=len,
        reverse=True,
    )
)
# Cloud service names with a digit, read as a token only in lower case starting a
# part (s3Key, s3Bucket): anywhere else two characters match too much random text.
_LEADING_DIGIT_TOKENS = ("s3", "ec2")
_MAX_INNER_DIGITS = 2
_MIN_WORD_BESIDE_DIGITS = 4
_DIGITS_READ_AS_WORDS = frozenset(("2",))
# A display scale (2x, 3x) after a word (retina2x).
_SCALE_DIGITS = frozenset(("2", "3"))


def _digit_token_at(part: str, at: int) -> int:
    """The end of a known digit token starting a word at *at* in *part*, or 0."""
    if at and not (part[at].isupper() or part[at - 1].isdigit()):
        return 0
    lowered = part[at:].lower()
    for token in _DIGIT_TOKENS:
        end = at + len(token)
        if lowered.startswith(token) and (end == len(part) or not part[end].islower()):
            return end
    for token in () if at else _LEADING_DIGIT_TOKENS:
        if part.startswith(token) and (len(token) == len(part) or part[len(token)].isupper()):
            return len(token)
    # A version only starts a part: inside one (jV29) it reads as random text.
    version = None if at else _VERSION_RE.match(lowered)
    if version:
        end = at + version.end()
        if end == len(part) or part[end].isupper():
            return end
    return 0


def _digit_word_part(part: str) -> bool:
    """*part* holds a digit run inside it and reads as words around it."""
    if _VERSION_RE.fullmatch(part):
        return True
    if not part[:1].isalpha() or not any(ch.isdigit() for ch in part):
        return False
    atoms: list[tuple[str, str]] = []
    at = 0
    while at < len(part):
        if part[at].isalpha() and (end := _digit_token_at(part, at)):
            atoms.append(("token", part[at:end]))
        elif part[at].isdigit():
            end = at
            while end < len(part) and part[end].isdigit():
                end += 1
            atoms.append(("digits", part[at:end]))
        else:
            end = at + 1
            while (
                end < len(part)
                and part[end].isalpha()
                and not (part[end].isupper() and _digit_token_at(part, end))
            ):
                end += 1
            atoms.append(("letters", part[at:end]))
        at = end
    words: list[str] = []
    for index, (kind, text) in enumerate(atoms):
        if kind == "letters":
            if text == "x" and index == len(atoms) - 1 and atoms[index - 1][1].isdigit():
                if atoms[index - 1][1] not in _SCALE_DIGITS:
                    return False
                continue
            if (text.isupper() and text not in _ACRONYMS) or not _plain_part(text):
                return False
            words += _CAMEL_WORD_RE.findall(text)
        elif kind == "digits" and not _inner_digits(atoms, index):
            return False
    short = [w for w in words if len(w) <= 3 and w not in _SHORT_WORDS and not _camel_acronym(w)]
    return len(short) <= 1


def _inner_digits(atoms: list[tuple[str, str]], index: int) -> bool:
    """Whether the digit run at *index* of *atoms* sits between words, by the rule
    above :func:`_digit_word_part`."""
    digits = atoms[index][1]
    before = atoms[index - 1] if index else ("", "")
    last = _CAMEL_WORD_RE.findall(before[1])[-1:] if before[0] == "letters" else []
    if len(digits) > _MAX_INNER_DIGITS or not last or len(last[0]) < _MIN_WORD_BESIDE_DIGITS:
        return False
    if index == len(atoms) - 1:
        return True
    kind, text = atoms[index + 1]
    if kind == "token":
        # A hash name glued on after digits reads as the end of a value
        # (abc12Sha256), as _hash_part rules.
        return False
    if text == "x":
        return True
    first = _CAMEL_WORD_RE.findall(text)[:1]
    if not first or len(first[0]) < _MIN_WORD_BESIDE_DIGITS:
        return False
    return first[0][0].isupper() or digits in _DIGITS_READ_AS_WORDS


def _part_reads_as_words(part: str) -> bool:
    if _WEBFORMS_CONTAINER_RE.fullmatch(part):
        return True
    hungarian = _HUNGARIAN_RE.fullmatch(part)
    if hungarian and (hungarian.group(2) == "Pwd" or _plain_part(hungarian.group(2))):
        return True
    return _plain_part(part) or _digit_word_part(part)


def _plain_part(part: str) -> bool:
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
    short = [w for w in words if len(w) <= 3 and w not in _SHORT_WORDS and not _camel_acronym(w)]
    if len(short) > 1:
        return False
    for index, word in enumerate(words):
        lowered = word.lower()
        if _camel_acronym(word) or _initial(words, index):
            continue
        if word.isupper():
            return False
        if len(word) == 2 and word[0].isupper():
            if word not in _SHORT_WORDS:
                return False
        elif len(word) < 2 or not _spelled(lowered) or not any(ch in _VOWELS for ch in lowered):
            return False
    return True


# A first, middle, or last name's initial starting a camel name before Name
# (txtFName). A lone letter before any capitalised word (eFiling, iPhone) passed
# more random text when allowed (#279).
_NAME_INITIALS = frozenset("FLM")


def _initial(words: list[str], index: int) -> bool:
    return not index and len(words) > 1 and words[0] in _NAME_INITIALS and words[1] == "Name"


# Hash and MAC names and their standard sizes (sha256, HmacSha512, md5, blake2b),
# and the checksum names crc32, crc32c, crc64, and crc64nvme:
# a run of three digits otherwise reads as a value, but here it is part of the
# algorithm's name. A size also reads as a word as a token of its own, joined by
# - or _, after such a name or a word naming what one makes (sha3_512,
# X-Hub-Signature-256).
_HASH_SIZES = "64|128|224|256|384|512"
_HASH_NAME_RE = re.compile(
    rf"(.*?)((?:sha[123]?|hmac|md5|blake2b?)(?:{_HASH_SIZES})?|crc(?:32c?|64(?:nvme)?))",
    re.IGNORECASE,
)
_HASH_SIZE_RE = re.compile(_HASH_SIZES)
_HASH_OUTPUT_WORDS = frozenset(("signature", "digest", "hash", "checksum"))
_NAME_PART_RE = re.compile(r"[^_.$\-\[\]]+")


def _hash_part(part: str) -> bool:
    """*part* is a hash or MAC name, with a standard size after it or none, alone
    or as a capitalised word after words ending in a letter (contentSha256,
    HmacSha512); glued on in lower case (abcsha256) or after a digit (abc12Sha256)
    it reads as the end of a value."""
    match = _HASH_NAME_RE.fullmatch(part)
    if match is None:
        return False
    head, name = match.groups()
    if not head:
        return True
    return name[0].isupper() and head[-1].isalpha() and (_plain_part(head) or _hash_part(head))


def _sized_part(previous: str, joiner: str, part: str) -> bool:
    """*part* is a standard size on its own, joined by *joiner* (- or _) to
    *previous*, a hash or MAC name or a part ending in a word naming its output."""
    if joiner not in ("-", "_") or not _HASH_SIZE_RE.fullmatch(part):
        return False
    words = _CAMEL_WORD_RE.findall(previous)
    return _hash_part(previous) or bool(words and words[-1].lower() in _HASH_OUTPUT_WORDS)


def _parts_read_as_words(text: str) -> bool:
    """Every part of *text* (split on ``_ . $ - [ ]``) reads as words, is a hash
    or MAC name (:func:`_hash_part`), or is a size after one (:func:`_sized_part`)."""
    matches = list(_NAME_PART_RE.finditer(text))
    for index, match in enumerate(matches):
        part = match.group()
        if _part_reads_as_words(part) or _hash_part(part):
            continue
        if index:
            before = matches[index - 1]
            if _sized_part(before.group(), text[before.end() : match.start()], part):
                continue
        return False
    return True


# Names a framework, a browser, a CDN, or a bot manager fixes, which no account
# varies: they pass by exact name, compared without regard to case.
_FRAMEWORK_NAMES = frozenset(
    (  # noqa: SIM905 - one compact string on purpose: the set is data
        # header names
        "upgrade-insecure-requests www-authenticate proxy-authenticate etag x-xss-protection "
        "alt-svc nel dnt cf-ray cf-cache-status cf-connecting-ip x-amz-cf-id x-amz-cf-pop "
        "x-amz-date x-amz-request-id x-amzn-requestid x-amzn-trace-id x-amz-id-2 x-fb-debug "
        "x-aspnet-version x-aspnetmvc-version x-dtpc grpc-status grpc-message x-cdn "
        "x-akamai-transformed akamai-grn x-iinfo x-sucuri-id apollographql-client-name "
        "apollographql-client-version upgrade "
        # header names the rules above cannot pass without passing random text (#361):
        # a camel word that splits into short words (WoW64, GUploader), a vowel-less
        # word (Rq), a consonant pair English rarely spells with (Object), a
        # two-letter word (Mp), and nine parts
        "sec-ch-ua-wow64 x-guploader-uploadid x-envoy-expected-rq-timeout-ms "
        "x-amz-object-lock-mode x-amz-mp-parts-count "
        "x-amz-server-side-encryption-aws-kms-key-id "
        # hidden input names a login form carries from a bot check: Cloudflare
        # Turnstile's response field, whose vendor word the spelling rule rejects
        "cf-turnstile-response "
        # body field names the rules hold back: Microsoft's login form field and the
        # vowel-less quantity abbreviation
        "loginfmt qty "
        # a JSON:API query switch, Segment's and ASP.NET Identity's cookie names
        "jsonapi ajs_user_id ajs_anonymous_id .aspnetcore.identity.application "
        # query parameter names the rules hold back: jQuery's cache-buster, a time
        # zone, ad click ids, a JSONP switch, and a foreign exchange flag (as an
        # acronym, FX passed random text inside camel case, #279)
        "_ tz gclid fbclid dclid jsonp fx "
        # more query names the rules hold back: an S3 signature v2 key, Ext JS's
        # cache-buster, a page number, a format switch, Solr's filter query, handler
        # and default field, imgix's format and library, and Mailchimp's campaign
        # and email ids
        "awsaccesskeyid _dc pg fmt fq qt df fm ixlib mc_cid mc_eid "
        # more body field names: the WebForms async postback flag, a mobile push
        # token, and Microsoft's login form fields beside loginfmt
        "__asyncpost fcmtoken ppft i0116 i0118 "
        # cookie names
        ".aspxauth asp.net_sessionid .aspnetcore.session .aspnetcore.antiforgery "
        ".aspnetcore.cookies __cf_bm cf_clearance __cfruid __cflb _cfuvid _abck bm_sz bm_sv "
        "bm_mi ak_bmsc _fbp _fbc _gcl_au _gcl_aw _gcl_dc phpsessid jsessionid django_language "
        "dtcookie rxvisitor rxvt arraffinity arraffinitysamesite _hjid _clck _clsk anonchk "
        "srm_b nlbi visid_incap awsalb awsalbcors awselb hubspotutk __hstc __hssc __hssrc "
        "_uetsid _uetvid muid csrftoken sessionid csrfmiddlewaretoken "
        # ASP.NET WebForms page state
        "__viewstate __viewstategenerator __viewstateencrypted __eventvalidation "
        "__eventtarget __eventargument __lastfocus __previouspage __requestverificationtoken"
    ).split()
)
# Cookie names a framework builds from a fixed prefix and a per-site suffix (an
# app, site, organisation, or container id). The name goes through the gate whole,
# like any other: a rule that passed the suffix on holds_an_id alone passed almost
# every random token after a prefix (#279 review). A cookie name that fails prints
# as its prefix and {} (shown_name), since the prefix is the framework's and
# names the cookie. Compared without regard to case.
_FRAMEWORK_PREFIXES = (
    ".aspnetcore.antiforgery.",
    "_hjsessionuser_",
    "_hjsession_",
    "amcv_",
    "amcvs_",
    "_ga_",
    "_gat_gtag_ua_",
    "aspsessionid",
)
# The suffix's characters: an Adobe organisation id ends in a percent-encoded @
# (AMCV_<id>%40AdobeOrg).
_PREFIXED_SUFFIX_RE = re.compile(r"[A-Za-z0-9_.\-%]{1,64}")


def framework_prefix(text: str) -> str:
    """The fixed framework prefix *text* starts with, as recorded, followed by a
    suffix of name characters, or ``""``."""
    lowered = text.lower()
    for prefix in _FRAMEWORK_PREFIXES:
        if lowered.startswith(prefix) and _PREFIXED_SUFFIX_RE.fullmatch(text[len(prefix) :]):
            return text[: len(prefix)]
    return ""


# ASP.NET WebForms: a naming container (ctl00) and a control-type prefix before a
# name that itself passes the gate (txtUserName, btnLogin, chkRememberMe).
_WEBFORMS_CONTAINER_RE = re.compile(r"ctl\d{2}")
# A WebForms element id: the control's name with its containers joined by _
# where the posted name joins them by $ (ctl00_ContentPlaceHolder1_Login1_UserName).
_WEBFORMS_ID_RE = re.compile(r"ctl\d{2}_")
_HUNGARIAN_RE = re.compile(
    r"(txt|btn|chk|ddl|lbl|hdn|pwd|rdo|rbl|cbl|lnk|img|pnl|cbo|cmb|lst|gv|rpt|hl)([A-Z]\w*)"
)
# Django formsets: <prefix>-<index>-<field> and the four management fields. The
# suffixes are code's; the field goes through the gate, and so does the prefix
# unless it is 1 to 4 lower-case letters (bvp): formset prefixes are short by
# convention, and 4 random letters carry under 19 bits, too few for a token.
_FORMSET_FIELD_RE = re.compile(r"([A-Za-z][A-Za-z_]{0,23})-(\d{1,2})-(.+)")
_FORMSET_MANAGEMENT_RE = re.compile(
    r"[A-Za-z][A-Za-z_]{0,23}-(?:TOTAL_FORMS|INITIAL_FORMS|MIN_NUM_FORMS|MAX_NUM_FORMS)"
)


_SHORT_PREFIX_RE = re.compile(r"[a-z]{1,4}")


def _formset_prefix(prefix: str) -> bool:
    return bool(_SHORT_PREFIX_RE.fullmatch(prefix)) or printable_name(prefix)


def printable_name(text: str) -> bool:
    """The one output gate: whether recorded *text* may be printed as a name."""
    if not text or len(text) > _MAX_NAME_LEN or not _NAME_CHARS_RE.fullmatch(text):
        return False
    if text.lower() in _FRAMEWORK_NAMES:
        return True
    if _FORMSET_MANAGEMENT_RE.fullmatch(text):
        return _formset_prefix(text.rpartition("-")[0])
    formset = _FORMSET_FIELD_RE.fullmatch(text)
    if formset:
        return _formset_prefix(formset.group(1)) and printable_name(formset.group(3))
    parts = [part for part in _NAME_SPLIT_RE.split(text) if part]
    if not parts or len(parts) > _MAX_PARTS or not any(_has_letter(part) for part in parts):
        return False
    if not _parts_read_as_words(text):
        return False
    # Capitals beside lower case (uid_SAMCOOKIE, addr[ADDRKEY]) read as a value
    # dropped into a name; a name all in capitals (PHPSESSID, XSRF-TOKEN) does not.
    if any(_long_capitals(part) for part in parts) and not all(
        part.rstrip("0123456789").isupper() or part.isdigit() for part in parts
    ):
        return False
    if _WEBFORMS_ID_RE.match(text):
        # Judged as its posted name is: joined by _, the whole id reads as a
        # base64 token by its letter-digit switches alone.
        return not holds_an_id(text.replace("_", "$"))
    return not holds_an_id(text)


def _long_capitals(part: str) -> bool:
    letters = part.rstrip("0123456789")
    return letters.isupper() and len(letters) > 3 and letters not in _ACRONYMS


def _printable_segment(segment: str) -> bool:
    """A path segment prints only when graftpunk's path rule keeps it
    (:func:`looks_dynamic`), so the script never prints a segment the digest's key
    replaces with a parameter, and then when it is a listed route segment or
    passes the gate and holds no run of capitals longer than an acronym (routes
    are words, and capitals in one are a code)."""
    if looks_dynamic(segment):
        return False
    text = unquote(segment)
    if text.lower() in _ENDPOINT_SEGMENTS:
        return True
    page = _PAGE_RE.fullmatch(text)
    if page:
        text = page.group(1)
    parts = [part for part in _NAME_SPLIT_RE.split(text) if part]
    return printable_name(text) and not any(_long_capitals(part) for part in parts)


# Route segments a framework or protocol fixes, and server pages by extension, the
# stem of which still goes through the gate (Login.aspx, index.php). A lone letter
# before a word (eFiling) passed random text as a rule of the gate (#279), so
# such a route is listed.
_ENDPOINT_SEGMENTS = frozenset(
    (  # noqa: SIM905 - one compact string on purpose: the set is data
        "graphql gql jsonrpc xmlrpc.php cgi-bin robots.txt favicon.ico sitemap.xml efiling"
    ).split()
)
_PAGE_RE = re.compile(r"(.+)\.(?:aspx|ashx|asmx|php|jsp|do|cfm|html?)", re.I)


def _placeholder(segment: str) -> str:
    text = unquote(segment)
    if text.isdigit() or holds_an_id(text):
        return "{id}"
    return "{token}" if len(text) >= 16 else "{}"


def templated_path(path: str) -> str:
    """*path* with each segment that fails the gate replaced by a placeholder."""
    # Each segment's ;params are dropped first, as the digest's key drops them.
    segments = _PATH_PARAMS_RE.sub("", path).split("/")
    out = [seg if not seg or _printable_segment(seg) else _placeholder(seg) for seg in segments]
    return "/".join(out)


_HOST_RE = re.compile(r"[a-z0-9.\-]{1,253}|\[[0-9a-f:.]{2,45}\]")
_WEB_SCHEMES = ("http", "https", "ws", "wss")
# The schemes a page or a redirect uses besides the web's, named; any other
# scheme is recorded text (a custom app scheme can be a word the account chose).
_KNOWN_SCHEMES = frozenset(("mailto", "javascript", "data", "tel", "blob", "about"))
# The labels a site's own host starts with that the gate holds back, each with an
# optional digit (www4, img1); measured on random labels of 2 to 32 characters, it
# prints none the gate held back but these words.
_HOST_WORD_RE = re.compile(r"(?:www|ww|img|gw|smtp|px|b2b|b2c|graphql|gql)\d?")
# The labels a host keeps as recorded: the site's own registrable domain. A
# per-account subdomain sits to their left.
_SITE_LABELS = 2


def _scheme(scheme: str) -> str:
    return f"{scheme}:" if scheme in _KNOWN_SCHEMES else "a URL of another scheme"


def _host_label(label: str) -> str:
    if not label or _HOST_WORD_RE.fullmatch(label) or printable_name(label):
        return label
    return "{id}" if label.isdigit() or holds_an_id(label) else "{}"


def shown_host(host: str) -> str:
    """*host* as the script prints it: an IP address as recorded, and a name with
    its last two labels as recorded and each label left of them through the gate,
    ``{id}`` or ``{}`` when it fails, so a per-account subdomain does not print."""
    if not _HOST_RE.fullmatch(host):
        return "{host}"
    if host.startswith("[") or _is_ip(host):
        return host
    labels = host.split(".")
    if len(labels) <= _SITE_LABELS:
        return host
    left, site = labels[:-_SITE_LABELS], labels[-_SITE_LABELS:]
    return ".".join([*(_host_label(label) for label in left), *site])


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
    host = shown_host((parts.hostname or "").lower())
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


# How deep a whole HAR may nest: past any HAR a browser or gp writes, and under the
# depth at which json.loads raises RecursionError on every Python the script runs
# on, which differs by version (a HAR 20,000 deep failed on 3.12 and read on 3.14).
_MAX_HAR_NESTING = 512


class HarError(Exception):
    """The HAR cannot be read; the message says why without quoting it."""


def _load(path: str) -> list[dict[str, Any]]:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise HarError(f"{path} is not UTF-8 text") from exc
    except OSError as exc:
        raise HarError(f"cannot read {path}") from exc
    if _nesting(text, _MAX_HAR_NESTING) > _MAX_HAR_NESTING:
        raise HarError(
            f"{path} nests JSON deeper than {_MAX_HAR_NESTING} levels, so it is not read"
        )
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


# graftpunk.har.naming.HTTP_METHODS, copied (a test holds the two equal): any
# other method is recorded text, which can be a word the account chose.
_HTTP_METHODS = frozenset(
    {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "TRACE", "CONNECT"}
)


def _method(request: dict[str, Any]) -> str:
    method = str(request.get("method", "")).upper()
    return method if method in _HTTP_METHODS else "(method not shown)"


def shown_name(name: str) -> str | None:
    """Cookie *name* as the script prints it: whole when it passes the gate, as its
    fixed framework prefix and ``{}`` when it fails and starts with one
    (``_hjSessionUser_{}``), or None when it is counted instead."""
    if printable_name(name):
        return name
    prefix = framework_prefix(name)
    return f"{prefix}{{}}" if prefix else None


def _names(raw: list[str], *, cookies: bool = False) -> str:
    """*raw* names, deduplicated, through the gate, with the rest counted; cookie
    names through :func:`shown_name`."""
    names = list(dict.fromkeys(name for name in raw if name))
    labels = [shown_name(n) if cookies else (n if printable_name(n) else None) for n in names]
    shown = list(dict.fromkeys(label for label in labels if label is not None))
    hidden = labels.count(None)
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
_MEDIA_TYPES = frozenset(
    "application audio font image message model multipart text video".split()  # noqa: SIM905
)
# Subtypes the gate holds back, named here: they print as they are.
_MEDIA_SUBTYPES = frozenset(
    (  # noqa: SIM905 - one compact string on purpose: the set is data
        "x-www-form-urlencoded xhtml+xml svg+xml x-ndjson ndjson x-msgpack msgpack gzip "
        "x-gzip graphql graphql-response+json grpc+proto grpc-web+proto sparql-query sql "
        "x-7z-compressed x-httpd-php vcard vtt jpeg jpg mp2t rfc822 gltf+json gltf-binary "
        "vnd.openxmlformats-officedocument.spreadsheetml.sheet "
        "vnd.openxmlformats-officedocument.wordprocessingml.document "
        "vnd.openxmlformats-officedocument.presentationml.presentation"
    ).split()
)
# The structured syntax suffixes, kept after a subtype the gate holds back.
_MEDIA_SUFFIXES = frozenset("json xml zip cbor gzip yaml jwt json-seq wbxml ber der".split())  # noqa: SIM905
# A vendor, personal, or unregistered tree's prefix, which the gate would read as
# a word.
_MEDIA_TREE_RE = re.compile(r"(?:vnd|prs|x)[.\-]")


def _subtype(subtype: str) -> str:
    """*subtype* when it is a known one or its pieces pass the gate, else ``{}``
    with its structured syntax suffix (``{}+json``) when that is a known one."""
    if subtype in _MEDIA_SUBTYPES:
        return subtype
    stem, plus, suffix = subtype.rpartition("+")
    if not plus:
        stem, suffix = subtype, ""
    if suffix and suffix not in _MEDIA_SUFFIXES:
        return "{}"
    tree = _MEDIA_TREE_RE.match(stem)
    name = stem[tree.end() :] if tree else stem
    if name and printable_name(name):
        return subtype
    return f"{{}}+{suffix}" if suffix else "{}"


def _mime(raw: str) -> str:
    """A content type's base as the script prints it: a standard top-level type,
    and its subtype through :func:`_subtype`."""
    base = raw.split(";", 1)[0].strip().lower()
    if not base:
        return ""
    if not _MIME_RE.fullmatch(base):
        return "a content type that is not shown"
    kind, _slash, subtype = base.partition("/")
    if kind not in _MEDIA_TYPES:
        return "a content type that is not shown"
    return f"{kind}/{_subtype(subtype)}"


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
        return shown_host(bare)
    return "a domain outside the response's host"


def _same_site(value: str | None) -> str:
    if value is None:
        return "-"
    known = {"lax": "Lax", "strict": "Strict", "none": "None"}
    return known.get(value.strip().lower(), "unknown")


# graftpunk.har.digest._PLAIN_COOKIE_PATH_RE, copied (a test holds the two equal):
# one leading slash, then letters, digits, ``.``, ``_``, ``~``, ``-``, and single
# slashes alone. Anything else (a comma, query, fragment, ``=``, ``;``, quote,
# space, percent escape, ``:``, ``@``, ``+``, or ``//``) can carry a second cookie
# folded into the header, a value, a host, or a user.
_PLAIN_COOKIE_PATH_RE = re.compile(r"(?!.*//)/[A-Za-z0-9._~/-]*")


def _cookie_path(value: str) -> str:
    if not value:
        return "-"
    if not value.startswith("/"):
        return "unknown"
    return templated_path(value) if _PLAIN_COOKIE_PATH_RE.fullmatch(value) else "(not shown)"


_MAX_AGE_RE = re.compile(r"-?[0-9]+")


def expiry_of(attributes: dict[str, str], sent_at: datetime | None) -> str:
    """``session``, ``persistent``, ``past``, or ``unknown`` for a ``Set-Cookie``
    line's attributes, by graftpunk.har.digest._expiry's rule (a test holds the
    two equal): ``Max-Age`` wins over ``Expires``, and an ``Expires`` is compared
    with *sent_at*, when the request was sent (``unknown`` without one)."""
    max_age = attributes.get("max-age")
    if max_age is not None and _MAX_AGE_RE.fullmatch(max_age):
        return "past" if int(max_age) <= 0 else "persistent"
    expires = attributes.get("expires")
    if expires is None:
        return "session" if max_age is None else "unknown"
    try:
        when = parsedate_to_datetime(expires)
    except (TypeError, ValueError, IndexError):
        return "unknown"
    return _past_or_persistent(when, sent_at)


def _past_or_persistent(when: datetime, sent_at: datetime | None) -> str:
    if sent_at is None:
        return "unknown"
    # timezone.utc, not datetime.UTC: the script runs on Python 3.9, which lacks it.
    utc = timezone.utc  # noqa: UP017
    sent = sent_at if sent_at.tzinfo else sent_at.replace(tzinfo=utc)
    when = when if when.tzinfo else when.replace(tzinfo=utc)
    return "past" if when <= sent else "persistent"


# Before Python 3.11, datetime.fromisoformat reads only isoformat's own output.
# _iso_by_hand and the helpers below read every string 3.11's reader reads, with
# the same result, its leniencies included (any one character between the date
# and the time, a fraction after any part of the time or the offset, one stray
# character before an offset where 3.11 allows one, which is not after a short
# fraction); a test holds it equal to fromisoformat on 3.11 and 3.12 over a
# generated corpus. 3.13 patch releases changed a few lenient readings (3.13.18
# rejects a bare comma before an offset and keeps the fraction of a zero offset,
# which 3.13.14 reads like 3.11), and 3.14 changed the reader (it reads an hour of 24
# and rejects some strings 3.11 reads), so from 3.11 on the script uses the
# running Python's reader, as the digest does.


def _iso_byte(raw: bytes, at: int) -> int:
    """The byte at *at*, or 0 past the end, as the C reader sees its NUL."""
    return raw[at] if at < len(raw) else 0


def _iso_digits(raw: bytes, at: int, count: int) -> int | None:
    """The number the *count* ASCII digits from *at* spell, or None."""
    chunk = raw[at : at + count]
    if len(chunk) != count or not all(48 <= byte <= 57 for byte in chunk):
        return None
    return int(chunk) if chunk else 0


def _iso_separator_at(raw: bytes) -> int | None:
    """Where the date ends: 3.11 decides it from the date's shape alone, so
    any one character may separate the date from the time."""
    size = len(raw)
    if size == 7:
        return 7
    if raw[4:5] == b"-":
        if raw[5:6] != b"W":
            return 10
        if size > 8 and raw[8:9] == b"-":
            if size == 9:
                return None
            return 8 if size > 10 and raw[10:11].isdigit() else 10
        return 8
    if raw[4:5] == b"W":
        end = 7
        while end < size and raw[end : end + 1].isdigit():
            end += 1
        if end < 9:
            return end
        return 7 if end % 2 == 0 else 8
    return 8


def _iso_date(raw: bytes, size: int) -> tuple[int, int, int] | None:
    """The year, month, and day of the date in ``raw[:size]``: extended or basic,
    and a week date with or without its day."""
    year = _iso_digits(raw, 0, 4)
    if year is None:
        return None
    dashed = _iso_byte(raw, 4) == 0x2D
    at = 5 if dashed else 4
    if _iso_byte(raw, at) == 0x57:  # W
        week = _iso_digits(raw, at + 1, 2)
        at += 3
        day: int | None = 1
        if at < size:
            if dashed:
                if _iso_byte(raw, at) != 0x2D:
                    return None
                at += 1
            day = _iso_digits(raw, at, 1)
        if week is None or day is None:
            return None
        try:
            found = date.fromisocalendar(year, week, day)
        except ValueError:
            return None
        return found.year, found.month, found.day
    month = _iso_digits(raw, at, 2)
    at += 2
    if dashed:
        if _iso_byte(raw, at) != 0x2D:
            return None
        at += 1
    day = _iso_digits(raw, at, 2)
    if month is None or day is None:
        return None
    return year, month, day


def _iso_clock(raw: bytes, at: int, end: int) -> tuple[list[int], bool] | None:
    """Hours, minutes, seconds, and microseconds read from *at* up to *end*, and
    whether anything follows them, or None. Each part is two digits, the parts
    joined by ``:`` throughout or not at all, and a ``.`` or ``,`` fraction may
    follow any part; one character after the last part counts as following."""
    parts = [0, 0, 0, 0]
    colons = True
    for index in range(3):
        value = _iso_digits(raw, at, 2)
        if value is None:
            return None
        parts[index] = value
        mark = _iso_byte(raw, at + 2)
        at += 3
        if index == 0:
            colons = mark == 0x3A
        if at >= end:
            return parts, mark != 0
        if colons and mark == 0x3A:
            continue
        if mark in (0x2C, 0x2E):
            break
        if colons:
            return None
        at -= 1
    count = min(end - at, 6)
    fraction = _iso_digits(raw, at, count)
    if fraction is None:
        return None
    parts[3] = fraction * 10 ** (6 - count)
    at += count
    while 48 <= _iso_byte(raw, at) <= 57:
        at += 1
    return parts, _iso_byte(raw, at) != 0


def _iso_by_hand(text: str) -> datetime | None:
    """*text* read as ``datetime.fromisoformat(text.replace("Z", "+00:00"))``
    reads it on Python 3.11 and 3.12, or None where that raises."""
    text = text.replace("Z", "+00:00")
    if len(text) < 7:
        return None
    for at in (7, 8, 10):
        if at >= len(text):
            break
        if "\ud800" <= text[at] <= "\udfff":
            # 3.11 reads a surrogate as the separator only.
            text = text[:at] + "T" + text[at + 1 :]
            break
    try:
        raw = text.encode("utf-8")
    except UnicodeEncodeError:
        return None
    split = _iso_separator_at(raw)
    if split is None:
        return None
    ymd = _iso_date(raw, split)
    if ymd is None:
        return None
    clock = [0, 0, 0, 0]
    zone = None
    if len(raw) > split:
        # The date is ASCII, so the separator is the character at split, of
        # however many bytes.
        rest = text[split + 1 :].encode("utf-8")
        sign_at = 0
        while sign_at < len(rest) and rest[sign_at] not in (0x2B, 0x2D):
            sign_at += 1
        read = _iso_clock(rest, 0, sign_at)
        if read is None:
            return None
        clock, trailing = read
        if sign_at >= len(rest):
            if trailing:
                return None
        else:
            offset = _iso_clock(rest, sign_at + 1, len(rest))
            if offset is None or offset[1]:
                return None
            hours, minutes, seconds, micros = offset[0]
            # 3.11 drops the fraction of an offset of zero whole seconds.
            if hours == minutes == seconds == 0:
                micros = 0
            delta = timedelta(hours=hours, minutes=minutes, seconds=seconds, microseconds=micros)
            try:
                zone = timezone(-delta if rest[sign_at] == 0x2D else delta)
            except ValueError:
                return None
    try:
        return datetime(*ymd, *clock, tzinfo=zone)
    except ValueError:
        return None


def _iso_datetime(text: str) -> datetime | None:
    """*text* read as graftpunk reads an ISO 8601 time (``fromisoformat`` after a
    ``Z`` becomes ``+00:00``). Before Python 3.11, ``fromisoformat`` reads only
    its own output, so :func:`_iso_by_hand` reads every string 3.11 reads; from
    3.11 on the running Python's reader decides, as it does for the digest."""
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        if sys.version_info >= (3, 11):  # noqa: UP036 - the script runs on 3.9 and 3.10
            return None
        return _iso_by_hand(text)


def array_expiry_of(expires: Any, sent_at: datetime | None) -> str:
    """The ``expiry`` word for a ``response.cookies`` entry's ``expires``, by
    graftpunk.har.digest._array_expiry's rule (a test holds the two equal): an
    ISO 8601 or HTTP date compared with *sent_at*, and ``unknown`` when it is
    missing, empty, or neither."""
    if not isinstance(expires, str) or not expires:
        return "unknown"
    when = _iso_datetime(expires)
    if when is None:
        try:
            when = parsedate_to_datetime(expires)
        except (TypeError, ValueError, IndexError):
            return "unknown"
    return _past_or_persistent(when, sent_at)


def _sent_at(entry: dict[str, Any]) -> datetime | None:
    started = entry.get("startedDateTime")
    return _iso_datetime(started) if isinstance(started, str) else None


def _array_flag(value: Any) -> str:
    if value is True:
        return "yes"
    return "no" if value is False else "unknown"


def _header_view(attributes: dict[str, str], host: str, sent_at: datetime | None) -> dict[str, str]:
    return {
        "HttpOnly": "yes" if "httponly" in attributes else "no",
        "Secure": "yes" if "secure" in attributes else "no",
        "SameSite": _same_site(attributes.get("samesite")),
        "Path": _cookie_path(attributes.get("path", "")),
        "Domain": _domain(attributes.get("domain", ""), host),
        "expiry": expiry_of(attributes, sent_at),
    }


def _array_view(cookie: dict[str, Any], host: str, sent_at: datetime | None) -> dict[str, str]:
    same_site, path = cookie.get("sameSite"), cookie.get("path")
    return {
        "HttpOnly": _array_flag(cookie.get("httpOnly")),
        "Secure": _array_flag(cookie.get("secure")),
        "SameSite": _same_site(same_site) if isinstance(same_site, str) else "unknown",
        "Path": _cookie_path(path) if isinstance(path, str) else "unknown",
        "Domain": _domain(str(cookie.get("domain") or ""), host),
        "expiry": array_expiry_of(cookie.get("expires"), sent_at),
    }


def _describe(view: dict[str, str]) -> str:
    return " ".join(f"{key}={value}" for key, value in view.items())


def cookies(entries: list[dict[str, Any]], only: str | None = None) -> list[str]:
    """One block per cookie set, or with *only*, the block of that exact name,
    which the caller already holds and so may print whatever the gate says."""

    def label_of(name: str) -> str | None:
        if only is not None:
            return name if name == only else None
        return shown_name(name)

    settings: dict[str, Counter[tuple[str, str]]] = {}
    hidden_set: set[str] = set()
    shown_set: set[str] = set()
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
            view = _header_view(attributes, host, _sent_at(entry))
            text = f"header: {_describe(view)}"
            matches = array.get(name, [])
            if seen[name] < len(matches):
                other = _array_view(matches[seen[name]], host, _sent_at(entry))
                # An attribute the array leaves out is a disagreement only for a
                # flag the header sets: the array often drops one it carried.
                differ = [
                    f"{key} is {other[key]} there"
                    for key in ("HttpOnly", "Secure", "Path", "Domain")
                    if other[key] != view[key]
                    and (
                        other[key] not in ("-", "unknown")
                        or (key in ("HttpOnly", "Secure") and view[key] == "yes")
                    )
                ]
                if differ:
                    text += f"; the response.cookies array differs: {', '.join(differ)}"
            else:
                text += "; not in the response.cookies array"
            seen[name] += 1
            if only is not None and name != only:
                continue
            label = label_of(name)
            if label is None:
                hidden_set.add(name)
                continue
            shown_set.add(name)
            settings.setdefault(label, Counter())[(where, text)] += 1
        for name, cookies_of_name in array.items():
            for cookie in cookies_of_name[seen[name] :]:
                if only is not None and name != only:
                    continue
                label = label_of(name)
                if label is None:
                    hidden_set.add(name)
                    continue
                shown_set.add(name)
                view = _describe(_array_view(cookie, host, _sent_at(entry)))
                text = f"response.cookies array only (no Set-Cookie header line): {view}"
                settings.setdefault(label, Counter())[(where, text)] += 1
    lines: list[str] = []
    for name in sorted(settings):
        lines.append(f"cookie {name}")
        for (where, text), count in settings[name].items():
            times = "1 time" if count == 1 else f"{count} times"
            lines.append(f"  set by {where} ({times}); {text}")
    if hidden_set:
        lines.append(f"set: {_value_names(len(hidden_set), 'cookie name')}")
    if only is not None:
        if not lines and only in sent:
            lines.append(f"cookie {only}: sent in requests and never set in this recording")
        return lines
    only_sent = [name for name in sent if name not in shown_set and name not in hidden_set]
    if only_sent:
        names = _names(sorted(only_sent), cookies=True)
        lines.append(f"sent in requests and never set in this recording: {names}")
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


def _shape(value: Any, indent: str, depth: int, max_depth: int = _MAX_DEPTH) -> list[str]:
    """Lines describing *value*'s keys and types, never a value, down to *max_depth*."""
    lines: list[str] = []
    if isinstance(value, dict):
        keys = [key for key in value if printable_name(str(key))]
        hidden = len(value) - len(keys)
        for key in keys[:_MAX_KEYS]:
            child = value[key]
            lines.append(f"{indent}{_field(str(key), _type(child) + _count(child))}")
            if not looks_secret(str(key)) and isinstance(child, (dict, list)) and depth < max_depth:
                lines.extend(_shape(child, indent + "  ", depth + 1, max_depth))
        if len(keys) > _MAX_KEYS:
            lines.append(f"{indent}({len(keys) - _MAX_KEYS} more keys)")
        if hidden:
            lines.append(f"{indent}({_value_names(hidden, 'key')})")
    elif isinstance(value, list) and value:
        first = value[0]
        lines.append(f"{indent}[first item]: {_type(first)}{_count(first)}")
        if isinstance(first, (dict, list)) and depth < max_depth:
            lines.extend(_shape(first, indent + "  ", depth + 1, max_depth))
    return lines


_MAX_JSON_NESTING = 64


def _nesting(text: str, limit: int = _MAX_JSON_NESTING) -> int:
    """How deep JSON *text* nests objects and arrays, strings skipped (one left
    open runs to the end), read in one pass that stops once it passes *limit*;
    the script bounds this itself, since how deep json.loads goes before
    RecursionError changed across Python versions. A loop over characters holds
    nothing but its state, which matters since it reads the whole HAR before
    json.loads does; a regex over the whole text took several times the memory."""
    depth = deepest = 0
    in_string = escaped = False
    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char in "[{":
            depth += 1
            if depth > deepest:
                deepest = depth
                if deepest > limit:
                    break
        elif char in "]}":
            depth -= 1
    return deepest


def _json_shape(text: str, indent: str) -> list[str] | None:
    if _nesting(text) > _MAX_JSON_NESTING:
        return [
            f"{indent}{len(text)} characters of JSON nested deeper than "
            f"{_MAX_JSON_NESTING} levels, not parsed"
        ]
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
    named = [(name, kind) for name, kind, _id in controls if name and printable_name(name)]
    counts = Counter(named)
    for name, kind in dict.fromkeys(named):
        repeat = f" ({counts[(name, kind)]} controls)" if counts[(name, kind)] > 1 else ""
        lines.append(f"{indent}{name}: {kind}{repeat}")
    # The controls with no name or a name that fails, each on its own line, except
    # that hidden inputs with no id that prints are one line per label with a count,
    # as a named repeat is: a form's run of hidden state is not a field to fill,
    # and an id that prints is the only selector a nameless control has.
    entries: list[tuple[str, str, list[tuple[int, str]]]] = []
    hidden_runs: dict[str, list[tuple[int, str]]] = {}
    for position, (name, kind, ident) in enumerate(controls, start=1):
        if name and printable_name(name):
            continue
        label = "(name not shown)" if name else "(no name)"
        if kind == "hidden" and not (ident and printable_name(ident)):
            if label not in hidden_runs:
                hidden_runs[label] = []
                entries.append((label, kind, hidden_runs[label]))
            hidden_runs[label].append((position, ident))
        else:
            entries.append((label, kind, [(position, ident)]))
    unlisted = 0
    for listed, (label, kind, members) in enumerate(entries):
        if listed >= _MAX_UNNAMED:
            unlisted += len(members)
            continue
        position, ident = members[0]
        if len(members) > 1:
            lines.append(
                f"{indent}{label} {kind} ({len(members)} controls, the first control "
                f"{position} of the form)"
            )
            continue
        shown = f", id {ident}" if ident and printable_name(ident) else ""
        hidden_id = ", an id that reads as a value" if ident and not shown else ""
        lines.append(f"{indent}{label} {kind}, control {position} of the form{shown}{hidden_id}")
    if unlisted:
        lines.append(f"{indent}({unlisted} more controls with no name or a name not shown)")
    return lines


_MAX_UNNAMED = 12


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


def _recorded_path(url: str) -> str:
    """*url*'s path as recorded: never its userinfo, host, query, or fragment."""
    try:
        return urlsplit(url).path
    except ValueError:
        return ""


def _matches(
    entries: list[dict[str, Any]],
    needle: str,
    method: str | None,
    har_path: Path,
    *,
    recorded: bool = False,
) -> list[dict[str, Any]]:
    """The entries whose URL, as this script prints it, holds *needle*, and with
    *method* whose method prints as it. A match on text the script does not print
    would answer whether a guessed value is in the recording, so the recorded path
    is searched only when *recorded* is set: the caller then holds the text."""
    found = []
    for entry in entries:
        request = _part(entry, "request")
        url = str(request.get("url", ""))
        if not (
            needle in templated_url(url)
            or (entry_operations(entry, har_path) and needle in _operation_url(url))
            or (recorded and needle in _recorded_path(url))
        ):
            continue
        if method and _method(request) != method.upper():
            continue
        found.append(entry)
    return found


def _byte_class(size: Any) -> str:
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        return "of unrecorded size"
    if size == 0:
        return "empty"
    if size < 1024:
        return "under 1 KB"
    return "under 1 MB" if size < 1024 * 1024 else "1 MB or more"


_LEFT_OUT_REASONS = {
    "over_max_body_size": "over the capture's max body size",
    "binary": "not text",
}


def _request_body_outcome(request: dict[str, Any], headers: list[Any], har_path: Path) -> list[str]:
    """What happened to a request body the HAR's postData does not hold.

    gp's recorder writes a body over its max body size, or one that is not
    text, to bodies/ (request._postDataFile), and marks one it could not get
    (request._postDataNotCaptured) or chose not to keep (request._postDataOmitted).
    """
    body_file = request.get("_postDataFile")
    if isinstance(body_file, str) and body_file:
        text, reason = _bodies_file_text(body_file, har_path)
        if text is None and reason == _NOT_TEXT:
            size = _byte_class(request.get("_postDataSize"))
            return [f"  body: stored in the recording's bodies/ directory, {reason}, {size}"]
        if text is None and reason == _OUTSIDE:
            return ["  body: not read (its file is outside the recording)"]
        if text is None:
            return ["  body: not captured (its file under bodies/ is missing or cannot be read)"]
        mime = _mime(_header(headers, "content-type"))
        size = _byte_class(len(text.encode("utf-8")))
        lines = [
            f"  body: stored in the recording's bodies/ directory, text, {size}, "
            f"{mime or 'no content type'}"
        ]
        lines.extend(_body_lines(mime, text, "    "))
        return lines
    omitted = request.get("_postDataOmitted")
    if isinstance(omitted, dict):
        why = _LEFT_OUT_REASONS.get(str(omitted.get("reason")), "for a reason not recorded")
        return [f"  body: left out by the recorder, {why}, {_byte_class(omitted.get('size'))}"]
    if request.get("_postDataNotCaptured") is True:
        return ["  body: not captured (the request sent one the recorder could not get)"]
    return ["  body: none recorded"]


def describe_request(entry: dict[str, Any], har_path: Path) -> list[str]:
    request = _part(entry, "request")
    url = str(request.get("url", ""))
    lines = [f"{_method(request)} {templated_url(url)}{query_keys(url)}"]
    headers = _list(request, "headers")
    lines.append(f"  request headers: {_names(_header_names(headers))}")
    lines.append(f"  request cookies: {_names(_request_cookie_names(request), cookies=True)}")
    post = request.get("postData")
    if not isinstance(post, dict):
        lines.extend(_request_body_outcome(request, headers, har_path))
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


_NOT_TEXT = "a body that is not text"
_OUTSIDE = "a body file outside the recording, not read"


def _bodies_file_text(body_file: str, har_path: Path) -> tuple[str | None, str]:
    """The text of a file the HAR names under the recording's bodies/, or None with
    the reason. A file outside bodies/, or a symlinked bodies/, is not read."""
    unlinked = har_path.parent / "bodies"
    try:
        if unlinked.is_symlink():
            return None, _OUTSIDE
        bodies = unlinked.resolve()
        target = (har_path.parent / body_file).resolve()
    except (OSError, ValueError):
        return None, "a body file that cannot be read"
    if bodies not in target.parents:
        return None, _OUTSIDE
    try:
        return target.read_text(encoding="utf-8"), ""
    except UnicodeDecodeError:
        return None, _NOT_TEXT
    except (OSError, ValueError):
        return None, "a body file that cannot be read"


def _response_text(entry: dict[str, Any], har_path: Path) -> tuple[str | None, str]:
    """The response body's text, or None with the reason."""
    content = _part(_part(entry, "response"), "content")
    body_file = content.get("_bodyFile")
    if isinstance(body_file, str) and body_file:
        return _bodies_file_text(body_file, har_path)
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
    lines.append(f"  sets cookies: {_names(names, cookies=True)}")
    text, reason = _response_text(entry, har_path)
    lines.append("  body:")
    if text is None:
        lines.append(f"    {reason}")
    else:
        lines.extend(_body_lines(mime, text, "    "))
    return lines


# graftpunk.har.graphql, copied: the script cannot import graftpunk, and a test holds
# the two equal on a corpus of bodies.
ANONYMOUS_OPERATION = "(anonymous)"
_MAX_OPERATION_NAME = 128
_GRAPHQL_NAME_RE = re.compile(r"[_A-Za-z][_0-9A-Za-z]*")
_OPERATION_KEYWORDS = frozenset({"query", "mutation", "subscription"})
_GRAPHQL_TOKEN_RE = re.compile(
    r'(?P<block>"""(?:\\"""|[^"]|"(?!""))*""")'
    r'|(?P<string>"(?:[^"\\\n\r]|\\.)*")'
    r"|(?P<number>-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?)"
    r"|(?P<name>[_A-Za-z][_0-9A-Za-z]*)"
    r"|(?P<punct>\.\.\.|[!$&()\[\]{}:=@|])"
    r"|(?P<comment>#[^\n\r]*)"
    r"|(?P<ignored>[\s,﻿]+)"
)
_MAX_GRAPHQL_NESTING = 128


def _graphql_tokens(document: str) -> list[tuple[str, str]] | None:
    tokens: list[tuple[str, str]] = []
    at = 0
    while at < len(document):
        match = _GRAPHQL_TOKEN_RE.match(document, at)
        if match is None:
            return None
        kind = match.lastgroup or ""
        if kind not in ("ignored", "comment"):
            tokens.append((kind, match.group()))
        at = match.end()
    return tokens


class _NotADocumentError(Exception):
    pass


class _DocumentReader:
    """The executable document grammar's structure, as graftpunk's parser checks it:
    ``(kind, name)`` per definition, kind ``shorthand`` for ``{ ... }``."""

    def __init__(self, tokens: list[tuple[str, str]]) -> None:
        self.tokens = tokens
        self.at = 0
        self.depth = 0

    def _peek(self) -> tuple[str, str] | None:
        return self.tokens[self.at] if self.at < len(self.tokens) else None

    def _is(self, text: str) -> bool:
        return self._peek() == ("punct", text)

    def _is_name(self) -> bool:
        token = self._peek()
        return token is not None and token[0] == "name"

    def _take(self) -> tuple[str, str]:
        token = self._peek()
        if token is None:
            raise _NotADocumentError
        self.at += 1
        return token

    def _punct(self, text: str) -> None:
        if not self._is(text):
            raise _NotADocumentError
        self.at += 1

    def _name(self) -> str:
        if not self._is_name():
            raise _NotADocumentError
        return self._take()[1]

    def _nest(self) -> None:
        self.depth += 1
        if self.depth > _MAX_GRAPHQL_NESTING:
            raise _NotADocumentError

    def document(self) -> list[tuple[str, str]]:
        found: list[tuple[str, str]] = []
        while self._peek() is not None:
            found.append(self._definition())
        operations = [d for d in found if d[0] != "fragment"]
        if not found or (len(operations) > 1 and any(not d[1] for d in operations)):
            raise _NotADocumentError
        return found

    def _definition(self) -> tuple[str, str]:
        if self._is("{"):
            self._selection_set()
            return ("shorthand", "")
        first = self._name()
        if first == "fragment":
            name = self._name()
            if name == "on" or self._name() != "on":
                raise _NotADocumentError
            self._name()
            self._directives(const=False)
            self._selection_set()
            return ("fragment", name)
        if first not in _OPERATION_KEYWORDS:
            raise _NotADocumentError
        name = self._take()[1] if self._is_name() else ""
        if self._is("("):
            self._take()
            count = 0
            while not self._is(")"):
                self._punct("$")
                self._name()
                self._punct(":")
                self._type()
                if self._is("="):
                    self._take()
                    self._value(const=True)
                self._directives(const=True)
                count += 1
            if not count:
                raise _NotADocumentError
            self._take()
        self._directives(const=False)
        self._selection_set()
        return (first, name)

    def _type(self) -> None:
        if self._is("["):
            self._take()
            self._nest()
            self._type()
            self.depth -= 1
            self._punct("]")
        else:
            self._name()
        if self._is("!"):
            self._take()

    def _directives(self, *, const: bool) -> None:
        while self._is("@"):
            self._take()
            self._name()
            if self._is("("):
                self._arguments(const=const)

    def _arguments(self, *, const: bool) -> None:
        self._punct("(")
        count = 0
        while not self._is(")"):
            self._name()
            self._punct(":")
            self._value(const=const)
            count += 1
        if not count:
            raise _NotADocumentError
        self._take()

    def _selection_set(self) -> None:
        self._punct("{")
        self._nest()
        count = 0
        while not self._is("}"):
            self._selection()
            count += 1
        if not count:
            raise _NotADocumentError
        self._take()
        self.depth -= 1

    def _selection(self) -> None:
        if self._is("..."):
            self._take()
            following = self._peek()
            if following is not None and following[0] == "name" and following[1] != "on":
                self._take()
                self._directives(const=False)
                return
            if following is not None and following[0] == "name":
                self._take()
                self._name()
            self._directives(const=False)
            self._selection_set()
            return
        self._name()
        if self._is(":"):
            self._take()
            self._name()
        if self._is("("):
            self._arguments(const=False)
        self._directives(const=False)
        if self._is("{"):
            self._selection_set()

    def _value(self, *, const: bool) -> None:
        kind, text = self._take()
        if kind in ("number", "string", "block", "name"):
            return
        if kind != "punct":
            raise _NotADocumentError
        if text == "$" and not const:
            self._name()
        elif text == "[":
            self._nest()
            while not self._is("]"):
                self._value(const=const)
            self._take()
            self.depth -= 1
        elif text == "{":
            self._nest()
            while not self._is("}"):
                self._name()
                self._punct(":")
                self._value(const=const)
            self._take()
            self.depth -= 1
        else:
            raise _NotADocumentError


_DOCUMENTS: dict[str, list[tuple[str, str]] | None] = {}
_MAX_DOCUMENTS = 256


def _definitions(document: str) -> list[tuple[str, str]] | None:
    """*document*'s definitions, read once per distinct text, or None when it is
    not an executable GraphQL document."""
    if document in _DOCUMENTS:
        return _DOCUMENTS[document]
    tokens = _graphql_tokens(document)
    found: list[tuple[str, str]] | None = None
    if tokens:
        try:
            found = _DocumentReader(tokens).document()
        except (_NotADocumentError, RecursionError):
            found = None
    if len(_DOCUMENTS) >= _MAX_DOCUMENTS:
        _DOCUMENTS.clear()
    _DOCUMENTS[document] = found
    return found


def _usable_operation_name(name: str) -> bool:
    return (
        len(name) <= _MAX_OPERATION_NAME
        and _GRAPHQL_NAME_RE.fullmatch(name) is not None
        and not holds_an_id(name)
    )


def document_operation(document: str) -> tuple[str, str] | None:
    """``(name, kind)`` of a GraphQL document's first operation, or None when the text
    is not an executable GraphQL document (graftpunk.har.graphql.document_operation)."""
    for kind, name in _definitions(document) or []:
        if kind != "fragment":
            usable = name if _usable_operation_name(name) else ANONYMOUS_OPERATION
            return usable, "query" if kind == "shorthand" else kind
    return None


def _operation_of(body: Any, path: str, *, batched: bool) -> dict[str, Any] | None:
    """One body object's operation: name, kind, variables, batched; None when it is
    not GraphQL."""
    if not isinstance(body, dict):
        return None
    sent = body.get("operationName")
    variables = body.get("variables") if isinstance(body.get("variables"), dict) else None
    query = body.get("query")
    if isinstance(query, str):
        read = document_operation(query)
        if read is None:
            return None
        definitions = _definitions(query) or []
        if (
            not (isinstance(sent, str) and sent)
            and definitions[0][0] == "shorthand"
            and "graphql" not in path.casefold()
            and variables is None
            and not isinstance(body.get("extensions"), dict)
        ):
            return None
        if isinstance(sent, str) and _GRAPHQL_NAME_RE.fullmatch(sent):
            name = sent if _usable_operation_name(sent) else ANONYMOUS_OPERATION
        else:
            name = read[0]
        return {"name": name, "kind": read[1], "variables": variables, "batched": batched}
    extensions = body.get("extensions")
    persisted = extensions.get("persistedQuery") if isinstance(extensions, dict) else None
    if not isinstance(persisted, dict) or not isinstance(sent, str):
        return None
    if not _GRAPHQL_NAME_RE.fullmatch(sent):
        return None
    name = sent if _usable_operation_name(sent) else ANONYMOUS_OPERATION
    return {"name": name, "kind": "persisted", "variables": variables, "batched": batched}


def operations_in(parsed: Any, path: str) -> list[dict[str, Any]]:
    """Every operation a decoded request body sent to *path* carries
    (graftpunk.har.graphql.operations_in)."""
    if isinstance(parsed, dict):
        found = _operation_of(parsed, path, batched=False)
        return [] if found is None else [found]
    if isinstance(parsed, list) and parsed:
        operations = []
        for element in parsed:
            found = _operation_of(element, path, batched=True)
            if found is None:
                return []
            operations.append(found)
        return operations
    return []


def _request_text(entry: dict[str, Any], har_path: Path) -> str | None:
    request = _part(entry, "request")
    post = request.get("postData")
    if isinstance(post, dict) and isinstance(post.get("text"), str):
        return post["text"]
    body_file = request.get("_postDataFile")
    if isinstance(body_file, str) and body_file:
        return _bodies_file_text(body_file, har_path)[0]
    return None


def _decoded(text: str | None) -> Any:
    if text is None or _nesting(text) > _MAX_JSON_NESTING:
        return None
    try:
        return json.loads(text)
    except (ValueError, RecursionError):
        return None


def _url_path(entry: dict[str, Any]) -> str:
    try:
        return urlsplit(str(_part(entry, "request").get("url", ""))).path
    except ValueError:
        return ""


def entry_operations(entry: dict[str, Any], har_path: Path) -> list[dict[str, Any]]:
    """The GraphQL operations *entry*'s request body carries, in order."""
    return operations_in(_decoded(_request_text(entry, har_path)), _url_path(entry))


# graftpunk.har.paths.template_path, copied (a test holds the two equal): the
# digest's key for a path keeps a segment as it is unless it reads as an id
# (/GraphQLController stays), so an operation's line names the key the digest,
# --command and --match take.
_JOIN_SPLIT_RE = re.compile(r"[_\-]")
_WORD_WITH_DIGITS_RE = re.compile(r"([A-Za-z]+)\d{1,2}")
_VERSION_RE = re.compile(r"v\d{1,2}(?:alpha|beta|rc)?\d{0,2}")
_PATH_PARAMS_RE = re.compile(r";[^/]*")
_SINGULAR_BEFORE_FINAL_S = frozenset("sui")


def _literal_part(part: str) -> bool:
    if not _has_digit(part):
        return True
    if _VERSION_RE.fullmatch(part) or (part.isdigit() and len(part) <= 2):
        return True
    match = _WORD_WITH_DIGITS_RE.fullmatch(part)
    return match is not None and _spelled(match.group(1))


def _literal_segment(text: str) -> bool:
    parts = _parts(text)
    if not all(_literal_part(part) for part in parts):
        return False
    digit_parts = [part for part in parts if _has_digit(part)]
    return not (len(digit_parts) > 1 and any(part.isdigit() for part in digit_parts))


def _joined_token(text: str) -> bool:
    parts = _JOIN_SPLIT_RE.split(text)
    return len(parts) >= 3 and all(
        2 <= len(part) <= 6 and _has_digit(part) and _has_letter(part) and part.isalnum()
        for part in parts
    )


def looks_dynamic(segment: str) -> bool:
    """graftpunk.har.paths.looks_dynamic, copied."""
    if not segment:
        return False
    if holds_an_id(segment):
        return True
    text = unquote(segment)
    if not _has_digit(text):
        return False
    return _joined_token(text) or not _literal_segment(text)


def _param_name(previous: str) -> str:
    if not previous or previous.startswith("{"):
        return "id"
    plural = previous.endswith("s") and len(previous) > 1
    plural = plural and previous[-2] not in _SINGULAR_BEFORE_FINAL_S
    return f"{previous[:-1] if plural else previous}_id"


def template_path(path: str) -> str:
    """*path* as the digest keys it (graftpunk.har.paths.template_path)."""
    path = _PATH_PARAMS_RE.sub("", path)
    leading = "/" if path.startswith("/") else ""
    trailing = "/" if len(path) > 1 and path.endswith("/") else ""
    segments = path.strip("/").split("/") if path.strip("/") else []
    result: list[str] = []
    for segment in segments:
        if looks_dynamic(segment):
            result.append(f"{{{_param_name(result[-1] if result else '')}}}")
        else:
            result.append(segment)
    body = "/".join(result)
    return f"{leading}{body}{trailing}" if body else (leading or "/")


def _operation_url(url: str) -> str:
    """*url* as an operation's line prints it: scheme and host as ``templated_url``
    prints them, and the path by :func:`_operation_path`."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return "a URL that does not parse"
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        return templated_url(url)
    host = shown_host((parts.hostname or "").lower())
    try:
        port = parts.port
    except ValueError:
        port = None
    return f"{scheme}://{host}{f':{port}' if port else ''}{_operation_path(parts.path)}"


def _operation_path(path: str) -> str:
    """*path* as the digest keys it (:func:`template_path`), with each segment the
    key keeps that the script's gate holds back (:func:`_printable_segment`)
    written ``{}``: the key's placeholders and every segment that prints are the
    digest's own, and nothing prints that a request line would hold back."""
    key = template_path(path)
    recorded = _PATH_PARAMS_RE.sub("", path).strip("/").split("/")
    keyed = key.strip("/").split("/")
    if len(recorded) != len(keyed):
        return templated_path(path)
    shown = [
        segment if not segment or segment != raw or _printable_segment(raw) else "{}"
        for raw, segment in zip(recorded, keyed)  # noqa: B905 - Python 3.9 has no strict=
    ]
    leading = "/" if key.startswith("/") else ""
    trailing = "/" if len(key) > 1 and key.endswith("/") else ""
    body = "/".join(shown)
    return f"{leading}{body}{trailing}" if body else key


# graftpunk.har.digest's scope rule, copied (a test holds the two equal): the
# digest reads only the run's own hosts (the one that answered the most requests
# that are not assets, a host that served an HTML page first, and its parent
# domain's subtree) and leaves assets, trackers and beacons out, so the operations
# and shared values here come from the entries the digest reads.
_ASSET_EXTENSION_RE = re.compile(
    r"\.(js|css|png|jpg|jpeg|gif|svg|ico|woff|woff2|ttf|eot|map)$", re.IGNORECASE
)
_EXCLUDE_HOST_PATTERNS = [
    r"google-analytics",
    r"googletagmanager",
    r"facebook\.com",
    r"analytics",
    r"tracking",
    r"cdn\.",
    r"static\.",
    r"assets\.",
    r"fonts\.",
]
_EXCLUDE_HOST_REGEX = re.compile("|".join(_EXCLUDE_HOST_PATTERNS), re.IGNORECASE)
_EXCLUDE_PATH_SEGMENTS = frozenset({"pixel", "beacon"})
_STATIC_MAIN_TYPES = frozenset({"image", "font", "audio", "video"})
_STATIC_CONTENT_TYPES = frozenset(
    {
        "text/css",
        "text/javascript",
        "application/javascript",
        "application/x-javascript",
        "application/ecmascript",
        "text/hyperscript",
        "application/wasm",
        "application/font-woff",
        "application/font-woff2",
        "application/vnd.ms-fontobject",
        "image/svg+xml",
    }
)
_DEFAULT_PORTS = {"http": "80", "https": "443"}


def _response_content_type(entry: dict[str, Any]) -> str:
    return _header(_list(_part(entry, "response"), "headers"), "content-type")


def _is_static(entry: dict[str, Any], path: str, netloc: str) -> bool:
    if _ASSET_EXTENSION_RE.search(path) or _EXCLUDE_HOST_REGEX.search(netloc):
        return True
    if any(segment.lower() in _EXCLUDE_PATH_SEGMENTS for segment in path.split("/")):
        return True
    content_type = _response_content_type(entry).split(";")[0].strip().lower()
    if not content_type:
        return False
    return content_type.split("/")[0] in _STATIC_MAIN_TYPES or content_type in _STATIC_CONTENT_TYPES


def _normal_host(scheme: str, netloc: str) -> str:
    host = netloc.rpartition("@")[2].lower()
    port = _DEFAULT_PORTS.get(scheme.lower())
    if port and host.endswith(f":{port}"):
        host = host[: -len(port) - 1]
    return host


def _is_ip(text: str) -> bool:
    try:
        ipaddress.ip_address(text)
    except ValueError:
        return False
    return True


def _host_name(host: str) -> str:
    if host.startswith("["):
        end = host.find("]")
        return host[: end + 1] if end != -1 else host
    if _is_ip(host):
        return host
    name, colon, port = host.rpartition(":")
    return name if colon and port.isdigit() else host


def _ip_literal(host: str) -> bool:
    name = _host_name(host)
    if name.startswith("[") and name.endswith("]"):
        name = name[1:-1]
    return _is_ip(name)


def _scope_root(primary_host: str) -> str:
    if _ip_literal(primary_host):
        return primary_host
    labels = primary_host.split(".")
    return primary_host if len(labels) <= 2 else ".".join(labels[1:])


def _in_scope(host: str, root: str) -> bool:
    if not root:
        return False
    if _ip_literal(root) or _ip_literal(host):
        return host == root
    return host == root or host.endswith(f".{root}")


def in_scope_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The entries the digest reads: on the run's own hosts, and no asset, tracker,
    or beacon (graftpunk.har.digest's static and scope rules)."""
    kept: list[tuple[dict[str, Any], str]] = []
    counts: dict[str, int] = {}
    documents: set[str] = set()
    for entry in entries:
        try:
            parts = urlparse(str(_part(entry, "request").get("url", "")))
        except ValueError:
            continue
        if parts.scheme.lower() not in ("http", "https"):
            continue
        host = _normal_host(parts.scheme, parts.netloc)
        if _is_static(entry, parts.path, parts.netloc):
            continue
        counts[host] = counts.get(host, 0) + 1
        status = _part(entry, "response").get("status")
        served = "html" in _response_content_type(entry).lower()
        if served and isinstance(status, int) and 200 <= status < 300:
            documents.add(host)
        kept.append((entry, host))
    primary = max(counts, key=lambda host: (host in documents, counts[host]), default="")
    root = _scope_root(primary) if primary else ""
    return [entry for entry, host in kept if _in_scope(host, root)]


def _operation_wanted(name: str, wanted: str | None) -> bool:
    if wanted is None:
        return True
    return name == wanted or (name == ANONYMOUS_OPERATION and wanted == "anonymous")


def _operation_label(name: str) -> str:
    return name if name == ANONYMOUS_OPERATION else f"({name})"


def operations(entries: list[dict[str, Any]], har_path: Path, wanted: str | None) -> list[str]:
    """One block per GraphQL operation on the run's own hosts (:func:`in_scope_entries`):
    its request line (the path as :func:`_operation_path` prints it), calls, kind, and the
    variables' and response's shapes (keys and types, never a value)."""
    entries = in_scope_entries(entries)
    groups: dict[tuple[str, str, str], dict[str, Any]] = {}
    for entry in entries:
        found = entry_operations(entry, har_path)
        if not found:
            continue
        request = _part(entry, "request")
        url = _operation_url(str(request.get("url", "")))
        response_text, _reason = _response_text(entry, har_path)
        response = _decoded(response_text)
        for index, operation in enumerate(found):
            if not _operation_wanted(operation["name"], wanted):
                continue
            key = (_method(request), url, operation["name"])
            group = groups.setdefault(
                key,
                {
                    "calls": 0,
                    "batched": 0,
                    "kind": operation["kind"],
                    "variables": None,
                    "response": None,
                },
            )
            group["calls"] += 1
            group["batched"] += 1 if operation["batched"] else 0
            if group["variables"] is None and operation["variables"] is not None:
                group["variables"] = operation["variables"]
            if group["response"] is None:
                if not operation["batched"]:
                    group["response"] = response
                elif isinstance(response, list) and len(response) == len(found):
                    group["response"] = response[index]
    blocks: list[str] = []
    for (method, url, name), group in groups.items():
        batched = f", {group['batched']} in a batch" if group["batched"] else ""
        lines = [
            f"{method} {url} {_operation_label(name)}: {group['calls']} call(s), "
            f"{group['kind']}{batched}"
        ]
        variables = group["variables"]
        lines.append("  variables:" if variables else "  variables: none recorded")
        if variables:
            lines.extend(_shape(variables, "    ", 1, _OPERATION_DEPTH))
        response = group["response"]
        if response is None:
            lines.append("  response: no JSON body recorded")
        else:
            lines.append(f"  response: JSON {_type(response)}{_count(response)}")
            lines.extend(_shape(response, "    ", 1, _OPERATION_DEPTH))
        blocks.append("\n".join(lines))
    shared = [
        line
        for line in shared_values(entries, har_path)
        if wanted is None or f" {wanted} " in f" {line} " or f"({wanted})" in line
    ]
    if blocks and shared:
        blocks.append("\n".join(["shared values (names only):", *shared]))
    return blocks


# graftpunk.har.digest._shared_values, copied (a test holds the two equal).
_MAX_SHARED_LEAVES = 2000
_MAX_SHARED_DEPTH = 24
_MAX_PRODUCERS = 8
_MIN_SHARED_TEXT = 4
_MIN_SHARED_DIGITS = 4
_FORM_STATE_TOKEN_RE = re.compile(r"[A-Z]+(?:_[A-Z]+)*")


def _shared_leaves(value: Any, path: str = "", depth: int = 0) -> list[tuple[str, Any]]:
    if depth > _MAX_SHARED_DEPTH:
        return []
    if isinstance(value, dict):
        found: list[tuple[str, Any]] = []
        for key, item in value.items():
            label = str(key) if printable_name(str(key)) else "{}"
            found += _shared_leaves(item, f"{path}.{label}" if path else label, depth + 1)
        return found
    if isinstance(value, list):
        return [leaf for item in value for leaf in _shared_leaves(item, f"{path}[]", depth + 1)]
    return [(path, value)]


def _linkable(value: Any) -> str | None:
    if value is None or isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    if not isinstance(value, str):
        text = repr(value)
        return text if sum(ch.isdigit() for ch in text) >= _MIN_SHARED_DIGITS else None
    if len(value.strip()) < _MIN_SHARED_TEXT or _FORM_STATE_TOKEN_RE.fullmatch(value):
        return None
    return value


def shared_values(entries: list[dict[str, Any]], har_path: Path) -> list[str]:
    """One line per pair of a GraphQL response field and a later request's variable
    that carried the same value, by names only, most frequent first. *entries* are
    the ones the digest reads (:func:`in_scope_entries`)."""
    producers: dict[str, list[tuple[str, str]]] = {}
    counts: dict[tuple[str, str, str, str], int] = {}
    for entry in entries:
        found = entry_operations(entry, har_path)
        if not found:
            continue
        for operation in found:
            seen: set[tuple[str, str, str, str]] = set()
            leaves = _shared_leaves(operation["variables"] or {})[:_MAX_SHARED_LEAVES]
            for path, value in leaves:
                text = _linkable(value)
                for source in producers.get(text, []) if text is not None else []:
                    seen.add((source[0], source[1], operation["name"], path))
            for key in seen:
                counts[key] = counts.get(key, 0) + 1
        response = _decoded(_response_text(entry, har_path)[0])
        if any(operation["batched"] for operation in found):
            answers = (
                [(found[i]["name"], response[i]) for i in range(len(found))]
                if isinstance(response, list) and len(response) == len(found)
                else []
            )
        else:
            answers = [(found[0]["name"], response)]
        for name, answer in answers:
            for path, value in _shared_leaves(answer)[:_MAX_SHARED_LEAVES]:
                text = _linkable(value)
                if text is None:
                    continue
                known = producers.setdefault(text, [])
                if (name, path) not in known and len(known) < _MAX_PRODUCERS:
                    known.append((name, path))
    ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return [
        f"  {source} {source_path} -> {target} variables.{variable}: {n} request(s)"
        for (source, source_path, target, variable), n in ordered
    ]


def main(argv: list[str]) -> int:
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(errors="backslashreplace")
    parser = argparse.ArgumentParser(prog="har-facts.py", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    named = sub.add_parser("cookies")
    named.add_argument("har")
    named.add_argument("--name")
    for name in ("request", "response"):
        one = sub.add_parser(name)
        one.add_argument("har")
        one.add_argument("url_part")
        one.add_argument("--method")
        one.add_argument("--operation")
        one.add_argument("--recorded-path", action="store_true")
    graphql = sub.add_parser("operations")
    graphql.add_argument("har")
    graphql.add_argument("--operation")
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
        lines = cookies(entries, args.name)
        if not lines and args.name is not None:
            print(f"har-facts: no cookie named {args.name!r} is set or sent.", file=sys.stderr)
            return 1
        if not lines:
            print(
                "har-facts: no response in this HAR sets a cookie, and no request sends one.",
                file=sys.stderr,
            )
            return 1
        print("\n".join(lines))
        return 0
    har_path = Path(args.har)
    if args.command == "operations":
        blocks = operations(entries, har_path, args.operation)
        if not blocks:
            which = f" named {args.operation!r}" if args.operation else ""
            print(f"har-facts: no GraphQL operation{which} in this HAR.", file=sys.stderr)
            return 1
        print("\n\n".join(blocks))
        return 0
    found = _matches(entries, args.url_part, args.method, har_path, recorded=args.recorded_path)
    if args.operation is not None:
        found = [
            entry
            for entry in found
            if any(
                _operation_wanted(op["name"], args.operation)
                for op in entry_operations(entry, har_path)
            )
        ]
    if not found:
        method = f" with method {args.method.upper()}" if args.method else ""
        if args.operation is not None:
            method += f" and operation {args.operation!r}"
        print(f"har-facts: no request URL holds {args.url_part!r}{method}.", file=sys.stderr)
        return 1
    blocks = []
    for entry in found[:_MAX_ENTRIES]:
        if args.command == "request":
            lines = describe_request(entry, har_path)
        else:
            lines = describe_response(entry, har_path)
        names = list(dict.fromkeys(op["name"] for op in entry_operations(entry, har_path)))
        if names:
            # The operation's key, as the digest prints it and --command takes it.
            url = str(_part(entry, "request").get("url", ""))
            lines[0] = lines[0].replace(templated_url(url), _operation_url(url), 1)
            lines[0] += " " + " ".join(_operation_label(name) for name in names)
        blocks.append("\n".join(lines))
    print("\n\n".join(blocks))
    if len(found) > _MAX_ENTRIES:
        more = len(found) - _MAX_ENTRIES
        print(f"\n({more} more entries match; narrow the URL part or add --method)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
