"""Print every value from a capture that is still in the fixture derived from it.

Usage: fixture-leaks.py CAPTURE FIXTURE [SIDECAR] [--har HAR]

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
surrogate pair, ``\\x26``, ``\\n``, ``\\/``), its HTML character references decoded
(``&amp;``), and its percent-encoding decoded (``%40``), repeated up to
three times while it keeps changing (``%2540``). A piece with a
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
'2026' (line 25: ...in a browser on »2026«-10-13.)``, the 1-based line number in
the file as read and the match between ``»`` and ``«`` with up to 30 characters
each side, then ``(and N more lines)`` past five. A value
found only in a decoded form (escaped, percent-encoded, or HTML-unescaped) has no
line in the file, so it prints once as ``(found decoded: ...»José«...)``, from
the decoded text in compatibility form, cut the same way. Characters that are not
printable, a backslash (shown doubled), and a ``»`` or ``«`` in the file are shown
escaped (``\\xbb``), so on a console that can print them a mark is always the
script's, and each place is one line. A console that cannot print the marks gets
them escaped too, where the line number still locates the match. A value or match longer
than 60 characters is shown as its first and last 30. No more of the file than
that is printed, since it may hold other captured values. Only the first 50
values are placed; the next 450 are named with ``(not placed)``, so a value kept on
purpose cannot hide the ones behind it, and past 500 only a count prints, with the
fixture not done while it does.
With SIDECAR, it also prints the names the sidecar lists, and for a schema 2
sidecar the reasons each flagged name is flagged, since a sidecar is committed
and the guide asks for both lists to be read, and compares SIDECAR, as
parsed JSON, with the sidecar gp wrote beside CAPTURE (``CAPTURE.meta.json``),
naming each key that differs: the skill writes that text with the Write tool, and
a slip in it would quietly weaken the suite's checks.

With ``--har HAR`` (the run's HAR), it also reads every value of each name in
``flagged_names`` (SIDECAR's, or without SIDECAR, that of gp's sidecar beside
CAPTURE, so a plugin or test module is checked too) that it can find in the
recording: each cookie of that name sent or set (the HAR's cookie arrays,
``Cookie`` and ``Set-Cookie`` headers), each request or response header of that
name in any case, each field of that name a request body sends (``postData``
params, a form-encoded or JSON body, or one kept in a file beside the HAR), and
each meta tag's ``content`` and hidden input's ``value`` of that name in a
response whose ``Content-Type`` header says HTML (a body kept in a file beside
the HAR, ``_bodyFile``, included), in a DOM snapshot gp took at a credential
post (``_domSnapshot``, ``_domAfter``), and in the run's ``page-source.html``
beside the HAR. A file that resolves outside the HAR's directory, through a
symlink or a ``..``, is not read.
It reports a flagged name whose recorded value of three or more characters is in
the fixture, as recorded, percent-decoded, or without the double quotes a cookie
value may carry, case-sensitively, in any of the forms above or two of them
applied one after the other (JSON-escaped inside an HTML attribute), as ``the
recorded value of flagged 'sessionid' is still in the fixture (line 3)``, or
``(found decoded)``. It never prints the value or the text around it: such a
value is a credential. It reports and never decides what the suite checks:
reading one more source can only find more. This catches a value the capture's
body never held, which the fixtures check in the suite does not: it holds names
only, and from sidecar schema 3 it looks for a flagged cookie name only where the
capture held one of that cookie's recorded values or held the name other than
in a name-value pair holding nothing, so a recorded cookie value pasted into a fixture from
elsewhere, even under its cookie's name, passes the suite in CI. From sidecar schema 4
the suite makes this same comparison itself when the sidecar's own observe run is on
the machine running it: graftpunk.testing.recorded is the package's copy of
recorded_values and recorded_leaks, and a test holds the two to the same output.

Exit 0 when nothing survived and the sidecar matches, 1 when something survived,
a recorded value of a flagged name is in the fixture, or the sidecar differs, 2 on
a usage or read error (a HAR that is not JSON included) (a capture that is not UTF-8 text,
such as a PDF, cannot be compared; nor can gp's sidecar when it is not JSON).
Standard library only, so it runs under any Python 3.9 or later.
"""

from __future__ import annotations

import json
import re
import sys
import unicodedata
from array import array
from bisect import bisect_right
from collections.abc import Callable, Iterator
from heapq import merge
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qsl, unquote, unquote_plus

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
# A \xXX escape as JavaScript and Python source spell one.
_HEX_ESCAPE = re.compile(r"\\x([0-9a-fA-F]{2})")
# A one-letter control escape (\n, \r, \t, \b, \f), so a name on the line after
# a JSON or Python string's \n is read as starting that line.
_CONTROL_ESCAPE = re.compile(r"\\([nrtbf])")
_CONTROL_CHARACTERS = {"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f"}
# Percent-decoding is repeated, so a URL nested in another URL's query is read as
# written, until the text stops changing or this many rounds have run.
_PERCENT_ROUNDS = 3
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
    except (ValueError, RecursionError):
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
    the one character it spells, its ``\\xXX`` escapes and its ``\\n``, ``\\r``,
    ``\\t``, ``\\b`` and ``\\f`` escapes decoded, and ``\\/`` read as ``/``."""

    def pair(m: re.Match[str]) -> str:
        high, low = int(m.group(1), 16), int(m.group(2), 16)
        return chr(0x10000 + ((high - 0xD800) << 10) + (low - 0xDC00))

    # Pairs first, each on its own, so a lone surrogate elsewhere in the file
    # leaves every pair still rejoined.
    decoded = _SURROGATE_PAIR.sub(pair, text)
    decoded = _UNICODE_ESCAPE.sub(lambda m: chr(int(m.group(1), 16)), decoded)
    decoded = _HEX_ESCAPE.sub(lambda m: chr(int(m.group(1), 16)), decoded)
    decoded = _CONTROL_ESCAPE.sub(lambda m: _CONTROL_CHARACTERS[m.group(1)], decoded)
    return decoded.replace("\\/", "/")


def _escape_decodings(text: str) -> list[str]:
    """*text* with its backslash escapes decoded, with its HTML character
    references decoded, and percent-decoded (``+`` as a space) once per round
    until it stops changing, at most three rounds. graftpunk's fixtures check reads
    a fixture in the same forms (graftpunk.testing.decoding.decoded_forms), and a
    test holds the two to the same output."""
    forms = [_unescape(text), unescape(text)]
    current = text
    for _ in range(_PERCENT_ROUNDS):
        decoded = unquote_plus(current)
        if decoded == current:
            break
        forms.append(decoded)
        current = decoded
    return forms


def _combined_decodings(text: str) -> list[str]:
    """Each of :func:`_escape_decodings`, and each of those decoded again the same
    ways, without repeats and in order, so a value JSON-escaped inside an HTML
    attribute is read. ``--har`` looks for a recorded value in these too, as gp
    observe fixtures does (graftpunk.testing.decoding.combined_forms; a test holds
    the two to the same output)."""
    forms: dict[str, None] = {}
    for form in _escape_decodings(text):
        forms.setdefault(form)
        for twice in _escape_decodings(form):
            forms.setdefault(twice)
    return list(forms)


def _without_quotes(value: str) -> str:
    """*value* without one pair of surrounding double quotes, as a server may
    quote a cookie value."""
    if len(value) > 2 and value.startswith('"') and value.endswith('"'):
        return value[1:-1]
    return value


def _value_forms(value: str) -> frozenset[str]:
    """The forms a recorded *value* is looked for in: as recorded, percent-decoded
    (``+`` read as a plus and as a space), and each of those without one pair of
    surrounding double quotes, so a quote sent as ``%22`` is taken off too
    (graftpunk.testing.decoding.value_forms; a test holds the two to the same
    output)."""
    forms = {value, unquote(value), unquote_plus(value)}
    return frozenset(forms | {_without_quotes(form) for form in forms})


def _decodings(fixture: str) -> list[str]:
    """*fixture* and the forms a copied value can hide behind in it."""
    return [fixture, "\n".join(body_values(fixture)), *_escape_decodings(fixture)]


# Around a single word: no letter or digit on either side. An underscore counts as a
# separator, so OKONKWO_ID holds the word OKONKWO.
_BEFORE = r"(?<![^\W_])"
_AFTER = r"(?![^\W_])"


def _found(value: str, haystack: str, folded: str) -> bool:
    """Whether *value* is in *haystack*, *folded* being it case-folded.

    A piece with a space, a digit, or an ``@`` matches in any case. A single word
    matches as written; in capitals as a whole word (``OKONKWO_ID``); and in lower
    case as a whole word joined to an identifier or an address by ``_``, ``@``, or
    ``.`` (``test_okonkwo_order``, ``okonkwo@example.com``). A lowercase word standing
    alone is not matched, so ``Total`` on a page is not found in ``total, shipping``.
    """
    if value in haystack:
        return True
    if _any_case(value):
        return value.casefold() in folded
    if re.search(_capitals(value), haystack):
        return True
    return re.search(_joined(value), folded) is not None


def _any_case(value: str) -> bool:
    return any(c.isspace() or c.isdigit() or c == "@" for c in value)


def _capitals(word: str) -> str:
    return rf"{_BEFORE}{re.escape(word.upper())}{_AFTER}"


def _joined(word: str) -> str:
    low = re.escape(word.casefold())
    return rf"(?:(?<=[_@.]){low}{_AFTER}|{_BEFORE}{low}(?=[_@.]))"


def _line_starts(text: str) -> array[int]:
    # An array, not a list, since the joined forms of a large fixture have many
    # lines and a list holds an object per line.
    starts = array("q", [0])
    starts.extend(m.end() for m in re.finditer("\n", text))
    return starts


class _Derived:
    """*derived*, made from *source* line by line and, within a line, chunk by chunk
    by *derive*, with an offset in it mapped back to *source*.

    *chunks* gives the (start, end) of each chunk of a line that *derive* may
    change. Only the chunks it does change are recorded, and only for a line asked
    about; a one-character chunk that stays one character maps to itself. A match
    that starts or ends inside a longer changed chunk is widened to the whole chunk.
    """

    def __init__(
        self,
        source: str,
        derived: str,
        chunks: Callable[[str], Iterator[tuple[int, int]]],
        derive: Callable[[str], str],
    ) -> None:
        self.source, self.derived = source, derived
        self._split, self._derive = chunks, derive
        self._source_starts = _line_starts(source)
        self.starts = _line_starts(derived)
        self._changed: dict[int, tuple[list[int], list[tuple[int, int, int, int]]]] = {}

    def line_of(self, offset: int) -> int:
        """The 0-based line of *derived* that holds *offset*."""
        return bisect_right(self.starts, offset) - 1

    def back(self, offset: int, *, end: bool = False) -> int:
        """*offset* in *derived* as an offset in *source*; *end* when it is a
        match's exclusive end, so it is placed by the character before it."""
        line = self.line_of(offset - 1 if end else offset)
        column = offset - self.starts[line]
        keys, chunks = self._chunks(line)
        i = bisect_right(keys, column - 1 if end else column) - 1
        base = self._source_starts[line]
        if i < 0:
            return base + column
        _start, stop, source_start, source_stop = chunks[i]
        if column < stop or (end and column == stop):
            return base + (source_stop if end else source_start)
        return base + source_stop + column - stop

    def _chunks(self, line: int) -> tuple[list[int], list[tuple[int, int, int, int]]]:
        if line not in self._changed:
            first = self._source_starts[line]
            last = self.source.find("\n", first)
            text = self.source[first : len(self.source) if last == -1 else last]
            chunks: list[tuple[int, int, int, int]] = []
            shift = 0
            for start, end in self._split(text):
                piece = text[start:end]
                derived = self._derive(piece)
                if derived != piece and (len(piece) > 1 or len(derived) > 1):
                    at = start + shift
                    chunks.append((at, at + len(derived), start, end))
                    shift += len(derived) - len(piece)
            # The search ran on the text derived whole; mapping back is right only
            # because deriving chunk by chunk gives the same line.
            stop = self.derived.find("\n", self.starts[line])
            length = (len(self.derived) if stop == -1 else stop) - self.starts[line]
            assert len(text) + shift == length, (line, len(text), shift, length)
            self._changed[line] = ([c[0] for c in chunks], chunks)
        return self._changed[line]


# Compatibility normalisation of a text is that of each run of non-ASCII characters
# with the one character before it, since no ASCII character is the second part of
# a composed one and ASCII text normalises to itself.
_NON_ASCII = re.compile(r"[\x00-\x7f]?[^\x00-\x7f]+")


def _normal_chunks(text: str) -> Iterator[tuple[int, int]]:
    """Each character of a non-ASCII run of *text* with the combining marks after
    it, or the whole run when it does not normalise piece by piece (Hangul jamo,
    which compose without a combining mark)."""
    for m in _NON_ASCII.finditer(text):
        run = m.group()
        cuts = [i for i, c in enumerate(run) if i == 0 or not unicodedata.combining(c)]
        cuts.append(len(run))
        pieces = [(cuts[i], cuts[i + 1]) for i in range(len(cuts) - 1)]
        if "".join(_normal(run[a:b]) for a, b in pieces) != _normal(run):
            pieces = [(0, len(run))]
        yield from ((m.start() + a, m.start() + b) for a, b in pieces)


def _fold_chunks(text: str) -> Iterator[tuple[int, int]]:
    """Each non-ASCII character of *text*: case folding works one character at a
    time and folds an ASCII character to one character."""
    return (m.span() for m in re.finditer(r"[^\x00-\x7f]", text))


# How much of a line is shown each side of a match, and the longest match or value
# shown whole; the file may hold captured values, so no more of it is printed than
# this.
_CONTEXT = 30
_LONGEST_MATCH = 60
# Each place is printed on a line of its own, at most this many per value, for at
# most this many values.
_MOST_PLACES = 5
_MOST_VALUES = 50
# Survivors past the placed ones are named without places, up to this many;
# the rest are counted, as an unchanged copy of a large capture holds thousands.
_MOST_NAMED = 500
# The marks around a match. The context escapes them where the file holds them, so
# a mark in the output is always the script's.
_OPEN, _CLOSE = "»", "«"


def _cut(text: str) -> str:
    """*text*, or its first and last 30 characters when longer than 60."""
    if len(text) <= _LONGEST_MATCH:
        return text
    half = _LONGEST_MATCH // 2
    return f"{text[:half]}...{text[-half:]}"


def _printable(text: str) -> str:
    """*text* with every character that is not printable escaped, so a control
    character or line separator from the fixture can neither split the one output
    line nor reach the terminal as an escape sequence, and with the match marks
    and backslashes escaped too, so every escape shown is the script's. A tab reads
    as a space."""
    return "".join(
        " "
        if c == "\t"
        else c
        if c.isprintable() and c not in (_OPEN, _CLOSE, "\\")
        else c.encode("unicode_escape").decode("ascii")
        for c in text
    )


def _neighborhood(text: str, start: int, end: int) -> str:
    """The match at *start*:*end* marked within its line, cut to :data:`_CONTEXT`
    characters each side, on one line."""
    line_start = text.rfind("\n", 0, start) + 1
    line_end = text.find("\n", end)
    line_end = len(text) if line_end == -1 else line_end
    left = max(line_start, start - _CONTEXT)
    right = min(line_end, end + _CONTEXT)
    before = ("..." if left > line_start else "") + _printable(text[left:start])
    after = _printable(text[end:right]) + ("..." if right < line_end else "")
    return f"{before}{_OPEN}{_printable(_cut(text[start:end]))}{_CLOSE}{after}"


class _Fixture:
    """The fixture searched as one text, its forms compatibility-normalised and
    joined by newlines, with the means to place a match in the file as read."""

    def __init__(self, fixture: str, shown: str | None = None) -> None:
        # *shown* is the file as read when *fixture* is it with gp's run labels
        # blanked; the blanking replaces characters one for one, so an offset in
        # one is the same offset in the other.
        self.shown = fixture if shown is None else shown
        assert len(self.shown) == len(fixture)
        self.text = fixture
        self.haystack = _normal("\n".join(_decodings(fixture)))
        self.folded = self.haystack.casefold()
        self._normal = _Derived(fixture, self.haystack, _normal_chunks, _normal)
        self._fold = _Derived(self.haystack, self.folded, _fold_chunks, str.casefold)
        # The raw form is the haystack's first lines, as many as the file has:
        # neither normalising nor folding makes or removes a newline.
        lines = fixture.count("\n") + 1
        self._raw_end = self._normal.starts[lines] - 1
        self._raw_folded_end = self._fold.starts[lines] - 1

    def holds(self, value: str) -> bool:
        return _found(value, self.haystack, self.folded)

    def _spans(self, value: str, end: int, folded_end: int) -> Iterator[tuple[int, int]]:
        """Where *value* is in the haystack before *end* (*folded_end* in the folded
        text), as :func:`_found` matches it, in order. A value can be in a large
        fixture tens of thousands of times, so the places are made as read."""

        def literal() -> Iterator[tuple[int, int]]:
            at = self.haystack.find(value, 0, end)
            while at != -1:
                yield at, at + len(value)
                at = self.haystack.find(value, at + 1, end)

        def matches(pattern: str, text: str, stop: int) -> Iterator[tuple[int, int]]:
            return (m.span() for m in re.compile(pattern).finditer(text, 0, stop))

        if _any_case(value):
            folded = matches(re.escape(value.casefold()), self.folded, folded_end)
            sources = [literal()]
        else:
            folded = matches(_joined(value), self.folded, folded_end)
            sources = [literal(), matches(_capitals(value), self.haystack, end)]
        # A piece is at least _MIN_LENGTH characters, so a match is never empty.
        sources.append((self._fold.back(s), self._fold.back(e, end=True)) for s, e in folded)
        return merge(*sources)

    def places(self, value: str) -> list[str]:
        """Where *value* is: ``line N: <neighborhood>`` for each line of the file that
        holds it, at most :data:`_MOST_PLACES`, or, when none does, ``found decoded:
        <neighborhood>`` at its first place in a decoded form."""
        first: list[tuple[int, int, int]] = []
        lines, last = 0, -1
        for start, end in self._spans(value, self._raw_end, self._raw_folded_end):
            line = self._normal.line_of(start)
            if line != last:
                lines, last = lines + 1, line
                if len(first) < _MOST_PLACES:
                    first.append((line, start, end))
        if first:
            places = [
                f"line {line + 1}: "
                + _neighborhood(
                    self.shown, self._normal.back(start), self._normal.back(end, end=True)
                )
                for line, start, end in first
            ]
            if lines > _MOST_PLACES:
                places.append(f"and {lines - _MOST_PLACES} more lines")
            return places
        # Not in the raw form, so in a decoded one, or, rarely, only across the
        # newline that joins two forms, which the haystack holds too.
        start, end = next(self._spans(value, len(self.haystack), len(self.folded)))
        return [f"found decoded: {_neighborhood(self.haystack, start, end)}"]


class _TokenValues(HTMLParser):
    """Each meta tag's content and each hidden input's value, by name."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.pairs: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key: value for key, value in attrs if value is not None}
        if tag == "meta":
            self.pairs.append((values.get("name", ""), values.get("content", "")))
        elif tag == "input" and values.get("type", "").lower() == "hidden":
            self.pairs.append((values.get("name", ""), values.get("value", "")))


def _cookie_pair(text: str) -> tuple[str, str]:
    name, _, value = text.partition("=")
    return name.strip(), value.strip()


def _named(items: object) -> list[tuple[str, str]]:
    """The (name, value) of each object in a HAR array whose name and value are
    strings."""
    if not isinstance(items, list):
        return []
    return [
        (item["name"], item["value"])
        for item in items
        if isinstance(item, dict)
        and isinstance(item.get("name"), str)
        and isinstance(item.get("value"), str)
    ]


def _header(headers: list[tuple[str, str]], name: str) -> str | None:
    """The value of the header *name* in *headers*, as graftpunk's HAR parser
    reads ``Content-Type``: the last header spelled ``name`` exactly, else the
    last spelled in lower case."""
    spelled = dict(headers)
    return spelled.get(name) or spelled.get(name.lower())


def _file_text(ref: object, base: Path | None) -> str | None:
    """The text of the file *ref* names, relative to the HAR's directory *base*,
    read as UTF-8 as graftpunk's HAR parser reads it. A file that resolves outside
    *base* (a ``..`` or a symlink), or one that cannot be read, gives None."""
    if base is None or not isinstance(ref, str) or not ref:
        return None
    root = base.resolve()
    path = (root / ref).resolve()
    if not path.is_relative_to(root):
        return None
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _body_text(content: dict, base: Path | None) -> str | None:
    """A response's body text: the HAR's ``content.text``, or, when the HAR keeps
    the body in a file (``content._bodyFile``), that file (:func:`_file_text`)."""
    text = content.get("text")
    if isinstance(text, str):
        return text
    return _file_text(content.get("_bodyFile"), base)


def _page_values(html: str) -> list[tuple[str, str]]:
    parser = _TokenValues()
    parser.feed(html)
    parser.close()
    return parser.pairs


def _json_pairs(data: object) -> Iterator[tuple[str, str]]:
    """``(key, value)`` for each string value under a key at any depth of *data*."""
    if isinstance(data, dict):
        for key, value in data.items():
            if isinstance(value, str):
                yield str(key), value
            else:
                yield from _json_pairs(value)
    elif isinstance(data, list):
        for item in data:
            yield from _json_pairs(item)


def _body_fields(request: dict, base: Path | None) -> list[tuple[str, str]]:
    """``(name, value)`` for each field a request body sends: the HAR's
    ``postData.params``, and its text (``postData.text``, or the file
    ``_postDataFile`` names) read as JSON, else as a form-encoded body."""
    post = request.get("postData") if isinstance(request.get("postData"), dict) else {}
    pairs = _named(post.get("params"))
    text = post.get("text")
    if not isinstance(text, str):
        text = _file_text(request.get("_postDataFile"), base)
    if text:
        try:
            pairs += list(_json_pairs(json.loads(text)))
        except (ValueError, RecursionError):
            pairs += parse_qsl(text, keep_blank_values=False)
    return pairs


def recorded_values(har: object, names: set[str], base: Path | None = None) -> dict[str, set[str]]:
    """Every non-empty value *har* records for each of *names* that this reads:
    as a cookie sent or set (the cookie arrays, and the ``Cookie`` and
    ``Set-Cookie`` headers), as a request or response header (any case), as a
    field a request body sends, or as a meta tag's content or a hidden input's
    value in a response whose ``Content-Type`` header says HTML. With *base*, the
    HAR's directory, a body or request body the HAR keeps in a file is read, and
    so are gp's DOM snapshots (``_domSnapshot``, ``_domAfter``) and the run's
    ``page-source.html`` beside the HAR, each only inside *base*."""
    found: dict[str, set[str]] = {}
    # Several flagged names can differ in case alone (X-CSRF-Token, x-csrf-token).
    folded: dict[str, list[str]] = {}
    for name in names:
        folded.setdefault(name.casefold(), []).append(name)

    def add(name: str, value: str) -> None:
        if name in names and value:
            found.setdefault(name, set()).add(value)

    def add_page(html: str | None) -> None:
        for name, value in _page_values(html) if html is not None else ():
            add(name, value)

    log = har.get("log") if isinstance(har, dict) else None
    entries = log.get("entries") if isinstance(log, dict) else None
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            continue
        request = entry.get("request") if isinstance(entry.get("request"), dict) else {}
        response = entry.get("response") if isinstance(entry.get("response"), dict) else {}
        for name, value in _named(request.get("cookies")) + _named(response.get("cookies")):
            add(name, value)
        for name, value in _named(request.get("headers")):
            if name.casefold() == "cookie":
                for line in value.splitlines():
                    for pair in line.split(";"):
                        add(*_cookie_pair(pair))
            else:
                for flagged in folded.get(name.casefold(), ()):
                    add(flagged, value)
        for name, value in _body_fields(request, base):
            add(name, value)
        add_page(_file_text(request.get("_domSnapshot"), base))
        add_page(_file_text(request.get("_domAfter"), base))
        response_headers = _named(response.get("headers"))
        for name, value in response_headers:
            if name.casefold() == "set-cookie":
                for line in value.splitlines():
                    add(*_cookie_pair(line.split(";", 1)[0]))
            else:
                for flagged in folded.get(name.casefold(), ()):
                    add(flagged, value)
        if "html" not in (_header(response_headers, "Content-Type") or "").lower():
            continue
        content = response.get("content") if isinstance(response.get("content"), dict) else {}
        add_page(_body_text(content, base))
    add_page(_file_text("page-source.html", base))
    return found


def recorded_leaks(fixture: _Fixture, values: dict[str, set[str]]) -> list[str]:
    """One line per flagged name whose recorded value is in *fixture*, naming the
    lines of the file that hold it (or ``found decoded``), never the value. Each
    of a value's :func:`_value_forms` is looked for in the fixture's forms and in
    :func:`_combined_decodings` of it."""
    lines: list[str] = []
    raw_lines = fixture.shown.split("\n")
    combined: str | None = None
    for name in sorted(values):
        wanted = {
            _normal(form)
            for value in values[name]
            for form in _value_forms(value)
            if len(form) >= _MIN_LENGTH
        }
        hits = [value for value in wanted if value in fixture.haystack]
        if not hits and wanted:
            if combined is None:
                combined = _normal("\n".join(_combined_decodings(fixture.text)))
            hits = [value for value in wanted if value in combined]
        if not hits:
            continue
        at = [
            str(number)
            for number, line in enumerate(raw_lines, 1)
            if any(value in _normal(line) for value in hits)
        ]
        where = f"line {', '.join(at[:_MOST_PLACES])}" if at else "found decoded"
        if len(at) > _MOST_PLACES:
            where += f" and {len(at) - _MOST_PLACES} more lines"
        lines.append(f"the recorded value of flagged {name!r} is still in the fixture ({where})")
    return lines


def survivors(capture: str, fixture: _Fixture) -> list[str]:
    """Each captured value that still appears in *fixture*, in any of its forms."""
    return [v for v in captured_values(capture) if fixture.holds(_normal(v))]


def main(argv: list[str]) -> int:
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(errors="backslashreplace")
    har_path = None
    if "--har" in argv:
        at = argv.index("--har")
        har_path = argv[at + 1] if at + 1 < len(argv) else None
        argv = argv[:at] + argv[at + 2 :]
        if har_path is None:
            print("fixture-leaks: --har needs the run's HAR.", file=sys.stderr)
            return 2
    if len(argv) not in (3, 4):
        print("usage: fixture-leaks.py CAPTURE FIXTURE [SIDECAR] [--har HAR]", file=sys.stderr)
        return 2
    role, path = "capture", argv[1]
    try:
        capture = Path(path).read_text(encoding="utf-8")
        role, path = "fixture", argv[2]
        fixture = Path(path).read_text(encoding="utf-8")
        role, path = "sidecar", argv[3] if len(argv) == 4 else ""
        sidecar = json.loads(Path(path).read_text(encoding="utf-8")) if len(argv) == 4 else None
        role, path = "HAR", har_path or ""
        har = json.loads(Path(path).read_text(encoding="utf-8")) if har_path else None
    except (OSError, ValueError, RecursionError) as exc:
        print(f"fixture-leaks: cannot read the {role} {path}: {exc}", file=sys.stderr)
        return 2
    if sidecar is not None and not isinstance(sidecar, dict):
        print(f"fixture-leaks: the sidecar {argv[3]} is not a JSON object.", file=sys.stderr)
        return 2
    differs = _keys_differing_from_gps(Path(argv[1]), sidecar) if sidecar is not None else []
    if differs is None:
        return 2
    found = _Fixture(_without_provenance(fixture), fixture)
    left = survivors(capture, found)
    leaked: list[str] = []
    if har is not None:
        names = _flagged_names(Path(argv[1]), sidecar)
        if names is None:
            return 2
        leaked = recorded_leaks(found, recorded_values(har, names, Path(har_path).parent))
    for value in left[:_MOST_VALUES]:
        for place in found.places(_normal(value)):
            print(f"still in the fixture: {_cut(value)!r} ({place})")
    # Past the placed values each survivor is still named, without its places: a
    # value kept on purpose is never rewritten, so a count alone would hide the
    # values behind it on every run.
    for value in left[_MOST_VALUES:_MOST_NAMED]:
        print(f"still in the fixture: {_cut(value)!r} (not placed)")
    if len(left) > _MOST_NAMED:
        print(
            f"fixture-leaks: {len(left) - _MOST_NAMED} more values not shown; "
            "the fixture is not done while this line prints."
        )
    for line in leaked:
        print(line)
    if differs:
        print(
            f"the sidecar differs from the capture's in {', '.join(differs)}: "
            "write the capture's sidecar text unchanged"
        )
    if sidecar is not None:
        for key in ("body_params", "flagged_names"):
            names = sidecar.get(key) or []
            print(f"sidecar {key}: {', '.join(map(str, names)) if names else '(none)'}")
        # Schema 2 says why each name is flagged; a schema 1 sidecar has no key.
        reasons = sidecar.get("flagged_reasons")
        if isinstance(reasons, dict):
            for name, why in sorted(reasons.items()):
                shown = ", ".join(map(str, why)) if isinstance(why, list) else repr(why)
                print(f"sidecar flagged {name}: {shown}")
    if not left:
        print("fixture-leaks: no captured value is left in the fixture.")
    return 1 if left or leaked or differs else 0


def _flagged_names(capture: Path, sidecar: dict | None) -> set[str] | None:
    """The ``flagged_names`` of *sidecar*, or, without one (a plugin module or a
    test module checked in place of a fixture), of the sidecar gp wrote beside
    *capture*. None, after saying why, when neither can be read."""
    if sidecar is None:
        own = capture.with_name(capture.name + ".meta.json")
        try:
            sidecar = json.loads(own.read_text(encoding="utf-8"))
        except (OSError, ValueError, RecursionError) as exc:
            print(
                f"fixture-leaks: --har reads the flagged names from SIDECAR or from the "
                f"capture's sidecar {own}, which cannot be read: {exc}",
                file=sys.stderr,
            )
            return None
    flagged = sidecar.get("flagged_names") if isinstance(sidecar, dict) else None
    return {n for n in flagged if isinstance(n, str)} if isinstance(flagged, list) else set()


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
