"""Print every value from a capture that is still in the fixture derived from it.

Usage: fixture-leaks.py CAPTURE FIXTURE [SIDECAR]

The graft skill writes each fixture from a capture on the developer's workstation,
inventing every value. This reports what survived, so the skill can rewrite it.

What it looks for, from the capture: each JSON string, number, and key (a key that
is a plain identifier without a digit run is skipped), or each text node, attribute
value, and comment of any other text capture, whole, and the pieces of it an
account value hides in: each run of three or more digits, each word holding an
``@``, a digit, or a capital letter, both halves of an email address, and each pair
of words with one of those in it.

Where it looks, in the fixture, in Unicode compatibility form: the raw text, its
JSON or HTML values decoded, its backslash escapes decoded (``\\u00e9``, a
surrogate pair, ``\\/``), and its percent-encoding decoded (``%40``). A piece with a
space, a digit, or an ``@`` matches in any case; a single word matches as written,
in capitals, or in lower case inside an identifier or an address
(``test_okonkwo_order``, ``okonkwo@example.com``). FIXTURE can be any text file, so
a plugin module or a test module is checked the same way.

What it cannot see, which needs a read by eye: a captured lowercase word alone, a
capitalised word copied in lower case on its own, a number reformatted (``12345`` as
``12,345``), digits split across fields, and a copy re-encoded another way (base64).
It does one substring search per captured value, so a capture of a megabyte or more
takes tens of seconds.

A string the command branches or selects on (a status, a currency code, a class
name) may be kept on purpose; the skill decides, so this reports and never edits.
With SIDECAR, it also prints the names the sidecar lists, since a sidecar is
committed and the guide asks for both lists to be read.

Exit 0 when nothing survived, 1 when something did, 2 on a usage or read error (a
capture that is not UTF-8 text, such as a PDF, cannot be compared).
Standard library only, so it runs under any Python 3.9 or later.
"""

from __future__ import annotations

import json
import re
import sys
import unicodedata
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote_plus

_DIGITS = re.compile(r"\d{3,}")
# Words split at spaces, punctuation, and a URL's separators, so an email in a
# query string (?m=ann%40shop.example&page=2) is a word of its own.
_WORD = re.compile(r"[^\s,;:()\[\]{}<>\"'?&=/]+")
_IDENTIFIER = re.compile(r"[a-z_][a-z0-9_]*\Z")
_MIN_LENGTH = 3
# A \uXXXX escape as JSON and Python source spell one, decoded wherever it appears,
# and a high and low surrogate escape side by side, which together spell one
# character outside the Basic Multilingual Plane.
_UNICODE_ESCAPE = re.compile(r"\\u([0-9a-fA-F]{4})")
_SURROGATE_PAIR = re.compile(r"\\u(d[89ab][0-9a-f]{2})\\u(d[c-f][0-9a-f]{2})", re.IGNORECASE)


class _TextParts(HTMLParser):
    """Text nodes, attribute values, and comments of a non-JSON body: the parts a
    site fills in, as opposed to its markup."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.values: list[str] = []

    def handle_data(self, data: str) -> None:
        self.values.append(data)

    def handle_comment(self, data: str) -> None:
        # The parser leaves a comment's entities as written; decode them like text.
        self.values.append(unescape(data))

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.values.extend(value for _name, value in attrs if value)


def _json_values(node: object) -> list[str]:
    if isinstance(node, dict):
        keys = [str(k) for k in node if not _IDENTIFIER.match(str(k)) or _DIGITS.search(str(k))]
        return keys + [leaf for value in node.values() for leaf in _json_values(value)]
    if isinstance(node, list):
        return [leaf for value in node for leaf in _json_values(value)]
    if isinstance(node, bool) or node is None:
        return []
    return [str(node)]


def body_values(text: str) -> list[str]:
    """The filled-in values of a body, decoded: JSON leaves and non-identifier keys,
    or the text parts of anything else."""
    try:
        return _json_values(json.loads(text))
    except ValueError:
        parser = _TextParts()
        parser.feed(text)
        parser.close()
        return parser.values


def _pieces(value: str) -> list[str]:
    """*value* and the parts of it an account value can hide in."""
    words = _WORD.findall(value)
    marked = [any(c == "@" or c.isdigit() or c.isupper() for c in w) for w in words]
    pieces = [value]
    pieces += _DIGITS.findall(value)
    pieces += [words[i] for i in range(len(words)) if marked[i]]
    # An address kept under a new domain still leaks its local part, and the reverse.
    pieces += [part for word in words if "@" in word for part in word.split("@", 1)]
    # A pair counts when either word is marked, so "Jane Doe" is a piece and a
    # pair of plain lowercase words ("at the") is not.
    pieces += [
        " ".join(words[i : i + 2]) for i in range(len(words) - 1) if marked[i] or marked[i + 1]
    ]
    return pieces


def captured_values(text: str) -> list[str]:
    """What to look for in a fixture, in first-seen order and without repeats."""
    found: dict[str, None] = {}
    for raw in body_values(text):
        # A percent-encoded value (a link's query string) is looked for decoded too,
        # since the fixture side is decoded the same way.
        decoded = unquote_plus(raw.strip())
        values = [raw.strip()] + ([decoded] if decoded != raw.strip() else [])
        for piece in (p for value in values for p in _pieces(value)):
            piece = piece.strip()
            if len(piece) >= _MIN_LENGTH:
                found[piece] = None
    return list(found)


def _normal(text: str) -> str:
    """*text* compatibility-normalised, so a composed and a decomposed accent match."""
    return unicodedata.normalize("NFKC", text)


def _unescape(text: str) -> str:
    """*text* with its ``\\uXXXX`` escapes decoded, a surrogate pair rejoined into
    the one character it spells, and ``\\/`` read as ``/``."""

    def pair(m: re.Match[str]) -> str:
        high, low = int(m.group(1), 16), int(m.group(2), 16)
        return chr(0x10000 + ((high - 0xD800) << 10) + (low - 0xDC00))

    # Pairs first, each on its own, so a lone surrogate elsewhere in the file
    # leaves every pair still rejoined.
    decoded = _SURROGATE_PAIR.sub(pair, text)
    decoded = _UNICODE_ESCAPE.sub(lambda m: chr(int(m.group(1), 16)), decoded)
    return decoded.replace("\\/", "/")


def _decodings(fixture: str) -> list[str]:
    """*fixture* and the forms a copied value can hide behind in it."""
    return [fixture, "\n".join(body_values(fixture)), _unescape(fixture), unquote_plus(fixture)]


# Around a single word: no letter or digit on either side. An underscore counts as a
# separator, so OKONKWO_ID holds the word OKONKWO.
_BEFORE = r"(?<![^\W_])"
_AFTER = r"(?![^\W_])"


def _found(value: str, haystack: str, folded: str) -> bool:
    """Whether *value* is in the fixture.

    A piece with a space, a digit, or an ``@`` matches in any case. A single word
    matches as written; in capitals as a whole word (``OKONKWO_ID``); and in lower
    case as a whole word joined to an identifier or an address by ``_``, ``@``, or
    ``.`` (``test_okonkwo_order``, ``okonkwo@example.com``). A lowercase word standing
    alone is not matched, so ``Total`` on a page is not found in ``total, shipping``.
    """
    if value in haystack:
        return True
    if any(c.isspace() or c.isdigit() or c == "@" for c in value):
        return value.casefold() in folded
    word = re.escape(value.upper())
    if re.search(rf"{_BEFORE}{word}{_AFTER}", haystack):
        return True
    low = re.escape(value.casefold())
    joined = rf"(?:(?<=[_@.]){low}{_AFTER}|{_BEFORE}{low}(?=[_@.]))"
    return re.search(joined, folded) is not None


def survivors(capture: str, fixture: str) -> list[str]:
    """Each captured value that still appears in *fixture*, in any of its forms."""
    haystack = _normal("\n".join(_decodings(fixture)))
    folded = haystack.casefold()
    return [v for v in captured_values(capture) if _found(_normal(v), haystack, folded)]


def main(argv: list[str]) -> int:
    if len(argv) not in (3, 4):
        print("usage: fixture-leaks.py CAPTURE FIXTURE [SIDECAR]", file=sys.stderr)
        return 2
    try:
        capture = Path(argv[1]).read_text(encoding="utf-8")
        fixture = Path(argv[2]).read_text(encoding="utf-8")
        sidecar = json.loads(Path(argv[3]).read_text(encoding="utf-8")) if len(argv) == 4 else None
    except (OSError, ValueError) as exc:
        print(f"fixture-leaks: cannot read the inputs: {exc}", file=sys.stderr)
        return 2
    if sidecar is not None and not isinstance(sidecar, dict):
        print("fixture-leaks: the sidecar is not a JSON object.", file=sys.stderr)
        return 2
    left = survivors(capture, fixture)
    for value in left:
        print(f"still in the fixture: {value!r}")
    if sidecar is not None:
        for key in ("body_params", "flagged_names"):
            names = sidecar.get(key) or []
            print(f"sidecar {key}: {', '.join(map(str, names)) if names else '(none)'}")
    if not left:
        print("fixture-leaks: no captured value is left in the fixture.")
    return 1 if left else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
