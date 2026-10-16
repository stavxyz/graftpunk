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
a plugin module or a test module is checked the same way. In the provenance phrase
gp writes into a generated stub's docstring (``seen 2 time(s) in run <session>/<run
id>.``), the date and time that start the run id are blanked first, since they are
gp's and not captured data; the rest of the phrase is still searched. A captured
digit run written by hand into those 14 digits is therefore not caught.

What it cannot see, which needs a read by eye: a captured lowercase word alone, a
capitalised word copied in lower case on its own, a number reformatted (``12345`` as
``12,345``), digits split across fields, and a copy re-encoded another way (base64).
It searches each form of the fixture once per captured value, so a capture of a
megabyte or more takes tens of seconds.

A string the command branches or selects on (a status, a currency code, a class
name) may be kept on purpose; the skill decides, so this reports and never edits.
It says where each value is, so the skill can name the line it keeps: one line of
output per line of FIXTURE that holds it, at most five, as ``still in the fixture:
'2026' (line 25: ...in a browser on >>2026<<-10-13.)``, the 1-based line number in
the file as read and the match between ``>>`` and ``<<`` with up to 30 characters
each side, then ``(and N more lines)`` past five. A value found only in a decoded
form (escaped, percent-encoded, or HTML-unescaped) has no line in the file, so it
prints once as ``(found decoded: ...>>José<<...)``, cut the same way. No more of
the file than that is printed, since it may hold other captured values.
With SIDECAR, it also prints the names the sidecar lists, since a sidecar is
committed and the guide asks for both lists to be read, and compares SIDECAR, as
parsed JSON, with the sidecar gp wrote beside CAPTURE (``CAPTURE.meta.json``),
naming each key that differs: the skill writes that text with the Write tool, and
a slip in it would quietly weaken the suite's checks.

Exit 0 when nothing survived and the sidecar matches, 1 when something survived or
the sidecar differs, 2 on a usage or read error (a capture that is not UTF-8 text,
such as a PDF, cannot be compared; nor can gp's sidecar when it is not JSON).
Standard library only, so it runs under any Python 3.9 or later.
"""

from __future__ import annotations

import json
import re
import sys
import unicodedata
from bisect import bisect_right
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote_plus

_DIGITS = re.compile(r"\d{3,}")
# Words split at spaces, punctuation, and a URL's separators, so an email in a
# query string (?m=ann%40myshop.example&page=2) is a word of its own.
_WORD = re.compile(r"[^\s,;:()\[\]{}<>\"'?&=/]+")
_IDENTIFIER = re.compile(r"[a-z_][a-z0-9_]*\Z")
_MIN_LENGTH = 3
# A \uXXXX escape as JSON and Python source spell one, decoded wherever it appears,
# and a high and low surrogate escape side by side, which together spell one
# character outside the Basic Multilingual Plane.
_UNICODE_ESCAPE = re.compile(r"\\u([0-9a-fA-F]{4})")
_SURROGATE_PAIR = re.compile(r"\\u(d[89ab][0-9a-f]{2})\\u(d[c-f][0-9a-f]{2})", re.IGNORECASE)
# The provenance phrase gp writes into a generated stub's docstring ("seen 2 time(s)
# in run myshop/20261006-101500-4242."), wrapped at any space. Its run id starts
# with the run's date and time, gp's and not captured data, yet the year matches any
# captured date, so those 14 digits are blanked in place and every other character,
# the count, the session, and the process id included, stays where it was for the
# search. A label too long for its line is cut mid-word, and the wrap can leave its
# closing period alone on the next line, so a candidate may span lines; it is
# blanked only when, with those breaks taken out, it is the session name given to
# `--from-run` and a run id `make_run_id` writes (a real date and time, then the
# process id). A session name with a dot or a space is left alone, and its year
# reported. A captured digit run written by hand into the 14 digits is not caught.
_PROVENANCE = re.compile(
    r"seen\s+\d+\s+time\(s\)\s+in\s+run\s+([A-Za-z0-9@_/-]+(?:\n[ \t]*[A-Za-z0-9@_/-]*)*)\."
)
_RUN_LABEL = re.compile(
    r"[A-Za-z0-9@_-]+/"
    r"(?:19|20)\d\d(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])-"
    r"(?:[01]\d|2[0-3])[0-5]\d[0-5]\d-\d+"
)
_DATE_TIME_DIGITS = 14


def _without_provenance(text: str) -> str:
    """*text* with the date and time of each of gp's run ids blanked in place."""

    def blank(match: re.Match[str]) -> str:
        label = match.group(1)
        if _RUN_LABEL.fullmatch(re.sub(r"\n[ \t]*", "", label)) is None:
            return match.group(0)
        chars = list(label)
        left = _DATE_TIME_DIGITS
        for i in range(label.index("/") + 1, len(chars)):
            if left and chars[i].isdigit():
                chars[i] = " "
                left -= 1
        head = match.group(0)[: match.start(1) - match.start(0)]
        return head + "".join(chars) + "."

    return _PROVENANCE.sub(blank, text)


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


def _folded(text: str) -> tuple[str, list[int]]:
    """*text* case-folded, and for each folded character the index in *text* of the
    character it came from: folding can lengthen a character (``ß`` to ``ss``), and
    a match in the folded text is reported at its place in *text*."""
    folded: list[str] = []
    origin: list[int] = []
    for i, char in enumerate(text):
        fold = char.casefold()
        folded.append(fold)
        origin.extend([i] * len(fold))
    return "".join(folded), origin


def _spans(value: str, text: str, folded: str, origin: list[int]) -> list[tuple[int, int]]:
    """Where *value* is in *text*, as (start, end) pairs, *folded* and *origin* being
    :func:`_folded` of *text*.

    A piece with a space, a digit, or an ``@`` matches in any case. A single word
    matches as written; in capitals as a whole word (``OKONKWO_ID``); and in lower
    case as a whole word joined to an identifier or an address by ``_``, ``@``, or
    ``.`` (``test_okonkwo_order``, ``okonkwo@example.com``). A lowercase word standing
    alone is not matched, so ``Total`` on a page is not found in ``total, shipping``.
    """
    spans = [m.span() for m in re.finditer(re.escape(value), text)]
    if any(c.isspace() or c.isdigit() or c == "@" for c in value):
        folded_patterns = [re.escape(value.casefold())]
    else:
        word = re.escape(value.upper())
        spans += [m.span() for m in re.finditer(rf"{_BEFORE}{word}{_AFTER}", text)]
        low = re.escape(value.casefold())
        folded_patterns = [rf"(?:(?<=[_@.]){low}{_AFTER}|{_BEFORE}{low}(?=[_@.]))"]
    for pattern in folded_patterns:
        # A piece is at least _MIN_LENGTH characters, so a match is never empty.
        for m in re.finditer(pattern, folded):
            spans.append((origin[m.start()], origin[m.end() - 1] + 1))
    return sorted(set(spans))


# How much of a line is shown each side of a match, and the longest match shown
# whole; the file may hold captured values, so no more of it is printed than this.
_CONTEXT = 30
_LONGEST_MATCH = 60
# Each place is printed on a line of its own, at most this many per value.
_MOST_PLACES = 5


def _neighborhood(text: str, start: int, end: int) -> str:
    """The match at *start*:*end* marked ``>>`` ``<<`` within its line, cut to
    :data:`_CONTEXT` characters each side, on one line."""
    line_start = text.rfind("\n", 0, start) + 1
    line_end = text.find("\n", end)
    line_end = len(text) if line_end == -1 else line_end
    left = max(line_start, start - _CONTEXT)
    right = min(line_end, end + _CONTEXT)
    match = text[start:end]
    if len(match) > _LONGEST_MATCH:
        half = _LONGEST_MATCH // 2
        match = f"{match[:half]}...{match[-half:]}"
    shown = (
        ("..." if left > line_start else "")
        + text[left:start]
        + f">>{match}<<"
        + text[end:right]
        + ("..." if right < line_end else "")
    )
    return _printable(shown)


def _printable(text: str) -> str:
    """*text* with every character that is not printable escaped, so a control
    character or line separator from the fixture can neither split the one output
    line nor reach the terminal as an escape sequence. A tab reads as a space."""
    return "".join(
        c if c.isprintable() else " " if c == "\t" else c.encode("unicode_escape").decode("ascii")
        for c in text
    )


class _Form:
    """One form of the fixture, compatibility-normalised, with what a search of it
    needs computed once: its case-folded text and where each line starts."""

    def __init__(self, text: str) -> None:
        self.text = _normal(text)
        self.folded, self.origin = _folded(self.text)
        self.line_starts = [0] + [i + 1 for i, c in enumerate(self.text) if c == "\n"]

    def spans(self, value: str) -> list[tuple[int, int]]:
        return _spans(value, self.text, self.folded, self.origin)

    def line_of(self, offset: int) -> int:
        """The 1-based line number of *offset*."""
        return bisect_right(self.line_starts, offset)


def _places(value: str, raw: _Form, shown: str, decoded: list[_Form]) -> list[str]:
    """Where *value* is: ``line N: <neighborhood>`` for each line of *raw* that holds
    it, the neighborhood taken from *shown* (the same text before gp's run labels
    were blanked), or, when no line does, ``found decoded: <neighborhood>`` from the
    first decoded form that holds it. Empty when it is in neither."""
    lines: dict[int, tuple[int, int]] = {}
    for start, end in raw.spans(value):
        lines.setdefault(raw.line_of(start), (start, end))
    if lines:
        places = [
            f"line {number}: {_neighborhood(shown, *span)}"
            for number, span in sorted(lines.items())[:_MOST_PLACES]
        ]
        if len(lines) > _MOST_PLACES:
            places.append(f"and {len(lines) - _MOST_PLACES} more lines")
        return places
    for form in decoded:
        spans = form.spans(value)
        if spans:
            return [f"found decoded: {_neighborhood(form.text, *spans[0])}"]
    return []


def survivors(capture: str, fixture: str, shown: str | None = None) -> list[tuple[str, list[str]]]:
    """Each captured value that still appears in *fixture*, in any of its forms, with
    the places :func:`_places` gives for it. *shown* is the fixture as read, when
    *fixture* is that text with gp's run labels blanked, so a neighborhood shows the
    file's own text; the blanking keeps every character in its place."""
    raw = _Form(fixture)
    display = _normal(shown) if shown is not None else raw.text
    if len(display) != len(raw.text):
        display = raw.text
    decoded = [_Form(text) for text in _decodings(fixture)[1:]]
    found = []
    for value in captured_values(capture):
        places = _places(_normal(value), raw, display, decoded)
        if places:
            found.append((value, places))
    return found


def main(argv: list[str]) -> int:
    if len(argv) not in (3, 4):
        print("usage: fixture-leaks.py CAPTURE FIXTURE [SIDECAR]", file=sys.stderr)
        return 2
    role, path = "capture", argv[1]
    try:
        capture = Path(path).read_text(encoding="utf-8")
        role, path = "fixture", argv[2]
        fixture = Path(path).read_text(encoding="utf-8")
        role, path = "sidecar", argv[3] if len(argv) == 4 else ""
        sidecar = json.loads(Path(path).read_text(encoding="utf-8")) if len(argv) == 4 else None
    except (OSError, ValueError) as exc:
        print(f"fixture-leaks: cannot read the {role} {path}: {exc}", file=sys.stderr)
        return 2
    if sidecar is not None and not isinstance(sidecar, dict):
        print(f"fixture-leaks: the sidecar {argv[3]} is not a JSON object.", file=sys.stderr)
        return 2
    differs = _keys_differing_from_gps(Path(argv[1]), sidecar) if sidecar is not None else []
    if differs is None:
        return 2
    left = survivors(capture, _without_provenance(fixture), fixture)
    for value, places in left:
        for place in places:
            print(f"still in the fixture: {value!r} ({place})")
    if differs:
        print(
            f"the sidecar differs from the capture's in {', '.join(differs)}: "
            "write the capture's sidecar text unchanged"
        )
    if sidecar is not None:
        for key in ("body_params", "flagged_names"):
            names = sidecar.get(key) or []
            print(f"sidecar {key}: {', '.join(map(str, names)) if names else '(none)'}")
    if not left:
        print("fixture-leaks: no captured value is left in the fixture.")
    return 1 if left or differs else 0


def _keys_differing_from_gps(capture: Path, sidecar: dict) -> list[str] | None:
    """The keys in which *sidecar* differs from the sidecar gp wrote beside
    *capture*, compared as parsed JSON since the suite reads it that way. A
    mistyped ``capture_sha256`` would silently switch off the suite's
    unchanged-copy check, and a dropped flagged name its name check. Empty when no
    gp sidecar sits beside *capture*; None, after saying why, when it cannot be
    read as a JSON object."""
    own = capture.with_name(capture.name + ".meta.json")
    if not own.exists():
        return []
    try:
        theirs = json.loads(own.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"fixture-leaks: cannot read the capture's sidecar {own}: {exc}", file=sys.stderr)
        return None
    if not isinstance(theirs, dict):
        print(f"fixture-leaks: the capture's sidecar {own} is not a JSON object.", file=sys.stderr)
        return None
    return sorted(k for k in theirs.keys() | sidecar.keys() if theirs.get(k) != sidecar.get(k))


if __name__ == "__main__":
    sys.exit(main(sys.argv))
