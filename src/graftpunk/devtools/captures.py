"""The writing side of the "captures never enter git" rule.

The ``.gitignore`` edit applied to disk, the git queries, and the committable
sidecar written beside each capture through the format
:mod:`graftpunk.testing.sidecar` owns. The rule itself, the directory and the
text edit, lives in :mod:`graftpunk.devtools.captures_rule`, which touches no
file.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import subprocess
from bisect import bisect_right
from collections.abc import Iterable, Iterator, Mapping, Sequence
from html.parser import HTMLParser
from json.scanner import make_scanner
from pathlib import Path

from graftpunk.devtools.captures_rule import with_ignored
from graftpunk.har.digest import form_state
from graftpunk.logging import get_logger
from graftpunk.testing.decoding import combined_forms, value_forms, whole_name_regex
from graftpunk.testing.sidecar import Sidecar, sidecar_path, sidecar_text

LOG = get_logger(__name__)

__all__ = [
    "IgnoreFileReadError",
    "checked_names_of",
    "ensure_ignored",
    "find_repo_root",
    "is_tracked",
    "write_sidecar",
]

_GIT_TIMEOUT_SECONDS = 10


class IgnoreFileReadError(OSError):
    """The ``.gitignore`` could not be read, as opposed to appended to (a bare
    ``OSError`` from :func:`ensure_ignored`). Carries the read's ``errno``,
    ``strerror``, and ``filename``."""


def _nearest_existing(start: Path) -> Path:
    """The closest ancestor of *start* (*start* itself when it exists) that is on disk.

    ``git`` needs a directory that exists to run in. The fixtures command's
    default target (``./tests/captures``) does not exist on a first run, and
    running git there raises ``FileNotFoundError``, which read as "not inside a
    git work tree" and let the command write unscrubbed bodies into a repo with
    nothing ignoring them.
    """
    probe = start
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return probe


def find_repo_root(start: Path) -> Path | None:
    """The git work tree's root containing *start*, or ``None`` outside one
    (including when ``git`` is not on ``PATH``).

    *start* need not exist yet: the search runs from its nearest existing
    ancestor, so a target directory this call is about to create is still
    resolved against the repository that will contain it.
    """
    argv = ["git", "rev-parse", "--show-toplevel"]
    try:
        result = subprocess.run(  # noqa: S603 - argv is a fixed git invocation, not untrusted input
            argv,
            cwd=_nearest_existing(start),
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return Path(result.stdout.strip()).resolve()


def ensure_ignored(repo_root: Path, relative: str) -> bool:
    """Add ``<relative>/`` to the root ``.gitignore`` unless a line already names it.

    The check is :func:`~graftpunk.devtools.captures_rule.with_ignored`'s: each
    line stripped of whitespace and then of trailing slashes, compared with
    *relative* stripped of trailing slashes. It does not ask git whether the path is already
    ignored, so a pattern that covers *relative* some other way (a parent
    directory, a glob, an exclude file) still gets the explicit line.

    Returns:
        True when the line was added, False when it was already present.

    Raises:
        UnicodeDecodeError: The ``.gitignore`` is not UTF-8 text.
        IsADirectoryError: The ``.gitignore`` is a directory.
        IgnoreFileReadError: The ``.gitignore`` cannot be read.
        OSError: The ``.gitignore`` cannot be appended to.
    """
    gitignore = repo_root / ".gitignore"
    if gitignore.is_dir():
        raise IsADirectoryError(errno.EISDIR, os.strerror(errno.EISDIR), str(gitignore))
    # Read as bytes so a CRLF file's text is what is on disk; the append below
    # then leaves every existing byte as it was.
    try:
        raw = gitignore.read_bytes() if gitignore.exists() else b""
    except OSError as exc:
        raise IgnoreFileReadError(exc.errno, exc.strerror, exc.filename) from exc
    existing = raw.decode("utf-8")
    updated = with_ignored(existing, relative)
    if updated == existing:
        return False
    # Binary append: the line ending with_ignored chose reaches the disk as written.
    with gitignore.open("ab") as handle:
        handle.write(updated[len(existing) :].encode("utf-8"))
    LOG.info("captures_gitignore_updated", path=str(gitignore), line=relative.rstrip("/") + "/")
    return True


def is_tracked(path: Path) -> bool:
    """True when *path* is tracked by git (``git ls-files --error-unmatch``)."""
    directory = path.parent if path.parent.exists() else Path.cwd()
    argv = ["git", "ls-files", "--error-unmatch", str(path)]
    try:
        result = subprocess.run(  # noqa: S603 - argv is a fixed git invocation, not untrusted input
            argv,
            cwd=directory,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


# A number is returned as its source text, so it is judged by form_state like a
# cookie value of the same digits.
_DECODER = json.JSONDecoder(parse_int=str, parse_float=str, parse_constant=str)
# typeshed types make_scanner's context as a make_scanner; it takes the decoder whose
# parse_* hooks it reads.
_SCAN = make_scanner(_DECODER)  # ty: ignore[invalid-argument-type]
# Where the JSON reader tries to parse a value: an object, an array, or a string
# whose text may be a JSON object or array.
_JSON_START_RE = re.compile(r"""[{\["]""")
# A JSON string as written. Run over a value already parsed, where every quote
# outside a string opens one, it finds each string in it.
_JSON_STRING_RE = re.compile(r'"[^"\\]*(?:\\[\s\S][^"\\]*)*"')
_JSON_COLON_RE = re.compile(r"\s*:\s*")
# Characters a failed parse may scan, per character of the text, before the JSON
# reader stops looking: an object left open many levels deep is otherwise
# scanned again from each level.
_JSON_FAILURE_BUDGET = 4
# The first slice a value is parsed in, and how near its end a failure means
# the slice cut the value short (a literal, a number or an escape cut in two).
_JSON_FIRST_SLICE = 64
_JSON_CUT_MARGIN = 16
# What may come before a query or form pair: the start of the text, a URL's or
# form body's boundary, or a quote.
_PAIR_BOUNDARIES = frozenset("?&;\"'")
# A query or form pair after the name: "=", a run of URL characters, then "&",
# "#", ";", the end of the text, or a quote (escaped included) that ends its
# string, so neither a token's next character nor a concatenation
# ("csrftoken=" + t) follows it.
_PAIR_VALUE_RE = re.compile(
    r"""=([^\s<>="'&#;\\]*)(?:[&#;]|\Z|\\?["'](?=[\s,;:)}\]>/&?\\]|\Z)(?!\s*[+.]))"""
)
# Elements that hold no text: the start tag of one of these, or a self-closing
# tag, is the whole element.
_VOID_ELEMENTS = frozenset({"input", "meta", "param", "link"})
# Attributes naming an element, and the attributes one must have to be read:
# <input name="x" value="...">, <meta name="x" content="...">.
_NAMING_ATTRIBUTES = frozenset({"name", "id", "property", "itemprop", "http-equiv"})
_VALUE_ATTRIBUTES = frozenset({"value", "content"})
# One attribute in a start tag's text as HTMLParser reads one: a name, then
# optionally "=" and a double-quoted, single-quoted or bare value.
_ATTRIBUTE_RE = re.compile(
    r"""([^\s/>][^\s/=>]*)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|(?!["'])([^>\s]*)))?"""
)

_Attributes = tuple[tuple[str, str], ...]


class _Elements(HTMLParser):
    """Each void element (:data:`_VOID_ELEMENTS`) or self-closing tag of an HTML
    text with a naming and a value or content attribute: the span in the text of
    each of its attribute values, in order, with the attribute's index and the
    element's attributes (values unescaped, an attribute without one as empty)."""

    def __init__(self, text: str) -> None:
        super().__init__()
        self.line_starts = [0, *(m.end() for m in re.finditer("\n", text))]
        self.starts: list[int] = []
        self.spans: list[tuple[int, int, int, _Attributes]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _VOID_ELEMENTS:
            self._read(tag, attrs)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._read(tag, attrs)

    def _read(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        keys = [key for key, _ in attrs]
        if _NAMING_ATTRIBUTES.isdisjoint(keys) or _VALUE_ATTRIBUTES.isdisjoint(keys):
            return
        source = self.get_starttag_text() or ""
        matches = list(_ATTRIBUTE_RE.finditer(source, 1 + len(tag)))
        # A tag this expression reads otherwise than HTMLParser did is not read.
        if [match.group(1).lower() for match in matches] != keys:
            return
        line, offset = self.getpos()
        start = self.line_starts[line - 1] + offset
        element = tuple((key, value or "") for key, value in attrs)
        for index, match in enumerate(matches):
            group = next((g for g in (2, 3, 4) if match.group(g) is not None), None)
            if group is not None:
                self.starts.append(start + match.start(group))
                self.spans.append(
                    (start + match.start(group), start + match.end(group), index, element)
                )


def _opened(value: object) -> _Form | None:
    """A form of *value*, a string read from JSON, when its text is a JSON object
    or array; else None."""
    if not isinstance(value, str) or not value.lstrip().startswith(("{", "[")):
        return None
    try:
        _DECODER.decode(value)
    except (ValueError, RecursionError):
        return None
    return _Form(value)


def _parse_json_at(text: str, start: int) -> tuple[object, int] | int:
    """The JSON value starting at *start* in *text* and where it ends, or, when
    none parses there, how many characters the attempt scanned.

    The value is parsed in a slice of the text, doubled while the parse fails
    only for want of text, so a failure costs what it scanned:
    :class:`json.JSONDecodeError` counts the lines before the failure in the
    string it was given."""
    size = _JSON_FIRST_SLICE
    while True:
        stop = min(len(text), start + size)
        piece = text[start:stop]
        try:
            value, end = _SCAN(piece, 0)
        except StopIteration:
            # Raised from deep inside a value as well as at its start.
            return len(piece)
        except json.JSONDecodeError as exc:
            # An unterminated string reports where it began, not where the
            # slice ran out.
            cut_short = exc.pos >= len(piece) - _JSON_CUT_MARGIN or exc.msg.startswith(
                "Unterminated"
            )
            if stop < len(text) and cut_short:
                size *= 2
                continue
            return len(piece) if cut_short else exc.pos + 1
        except (ValueError, RecursionError):
            return len(text) - start
        return value, start + end


def _json_values(text: str) -> list[tuple[int, int, _Form | None]]:
    """The JSON values *text* holds, in order and not nested, by span: each object
    or array that parses, with None, and each string whose text is a JSON object
    or array, with that text's form (:func:`_opened`). A string that is not
    opened is looked into, so ``"{"a": 1}"`` with its escapes decoded still
    yields the object. Failed parses stop the search once they have scanned
    :data:`_JSON_FAILURE_BUDGET` times the text's length, so the cost stays linear."""
    found: list[tuple[int, int, _Form | None]] = []
    budget = _JSON_FAILURE_BUDGET * len(text)
    pos = 0
    # A quote inside a string already parsed is not a string's start.
    quotes_from = 0
    while budget > 0:
        match = _JSON_START_RE.search(text, pos)
        if match is None:
            break
        start = match.start()
        pos = start + 1
        quote = text[start] == '"'
        if quote and start < quotes_from:
            continue
        parsed = _parse_json_at(text, start)
        if isinstance(parsed, int):
            budget -= parsed
            continue
        value, end = parsed
        if not quote:
            found.append((start, end, None))
            pos = quotes_from = end
            continue
        opened = _opened(value)
        if opened is None:
            quotes_from = end - 1
            continue
        found.append((start, end, opened))
        pos = quotes_from = end
    return found


class _Form:
    """One form a body is read in: its text, the text casefolded with the offset
    in the text each casefolded character came from (None when casefolding kept
    every offset), and what each strict reader finds in it, read on first use."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.folded = text.casefold()
        self.origin: list[int] | None = None
        if len(self.folded) != len(text):
            self.origin = [i for i, ch in enumerate(text) for _ in ch.casefold()]
        self._json: list[tuple[int, int, _Form | None]] | None = None
        self._json_starts: list[int] = []
        self._strings: dict[int, tuple[list[int], list[tuple[int, int]]]] = {}
        self._opened: dict[int, _Form | None] = {}
        self._elements: _Elements | None = None

    def occurrences(self, pattern: re.Pattern[str]) -> Iterator[tuple[int, int]]:
        """The span in the text of each match of *pattern* in the casefolded text."""
        for match in pattern.finditer(self.folded):
            start, end = match.span()
            if self.origin is None:
                yield start, end
            else:
                yield self.origin[start], self.origin[end - 1] + 1

    def json_value_at(self, pos: int) -> tuple[int, int, int, _Form | None] | None:
        """The index, span and opened form of the JSON value (:func:`_json_values`)
        holding *pos*, or None."""
        if self._json is None:
            self._json = _json_values(self.text)
            self._json_starts = [start for start, _, _ in self._json]
        index = bisect_right(self._json_starts, pos) - 1
        if index < 0:
            return None
        start, end, opened = self._json[index]
        return (index, start, end, opened) if pos < end else None

    def string_at(self, index: int, start: int, end: int, pos: int) -> tuple[int, int] | None:
        """The span of the string holding *pos* in the JSON value at *index*, which
        spans *start*..*end*, or None."""
        if index not in self._strings:
            spans = [m.span() for m in _JSON_STRING_RE.finditer(self.text, start, end)]
            self._strings[index] = ([s for s, _ in spans], spans)
        starts, spans = self._strings[index]
        at = bisect_right(starts, pos) - 1
        if at < 0 or pos >= spans[at][1]:
            return None
        return spans[at]

    def opened_string(self, start: int, end: int) -> _Form | None:
        """The form of the JSON string at *start*..*end* when its text is a JSON
        object or array (:func:`_opened`), decoded once."""
        if start not in self._opened:
            try:
                self._opened[start] = _opened(_DECODER.decode(self.text[start:end]))
            except ValueError:
                self._opened[start] = None
        return self._opened[start]

    def element_at(self, pos: int) -> tuple[int, int, int, _Attributes] | None:
        """The attribute value span holding *pos* of an element :class:`_Elements`
        reads, with the attribute's index and the element's attributes, or None."""
        if self._elements is None:
            self._elements = _Elements(self.text)
            if "<" in self.text:
                self._elements.feed(self.text)
                self._elements.close()
        at = bisect_right(self._elements.starts, pos) - 1
        if at < 0:
            return None
        span = self._elements.spans[at]
        return span if pos < span[1] else None


def _holds_nothing(value: object) -> bool:
    """True when *value* holds no credential: a boolean, null, an empty string, a
    string (a number as its text) that is form state by the rule a cookie value
    is judged by (:func:`graftpunk.har.digest.form_state`), or an object whose
    every key and value, or an array whose every item, is one of those."""
    if value is None or isinstance(value, bool):
        return True
    if isinstance(value, str):
        return not value or form_state(value)
    if isinstance(value, dict):
        return all(_holds_nothing(key) and _holds_nothing(item) for key, item in value.items())
    if isinstance(value, list):
        return all(_holds_nothing(item) for item in value)
    return False


def _read(name: str, form: _Form, start: int, end: int) -> tuple[bool | None, int]:
    """How the strict readers read the occurrence of *name* at *start*..*end* in
    *form*, and where the reading ended: True when one read it with a value that
    holds nothing (:func:`_holds_nothing`), False when one read it with any other
    value, None when none read it.

    - JSON: the occurrence is a key of a JSON value :func:`_json_values` parsed,
      read with the key's parsed value; or it is in a string of such a value, or
      is such a string, whose text is a JSON object or array, read as that text
      is (:func:`_is_accounted_for`).
    - A query or form pair: the start of the text or one of ``?&;"'`` before the
      name, then :data:`_PAIR_VALUE_RE`, the value judged as a string.
    - An element attribute: the occurrence is in an attribute value of an
      element :class:`_Elements` reads, and a naming attribute of that element
      is the name; every other attribute value of it is judged."""
    text = form.text
    held = form.json_value_at(start)
    if held is not None:
        index, value_start, value_end, opened = held
        if opened is not None:
            return _is_accounted_for(name, [opened]), value_end
        string = form.string_at(index, value_start, value_end, start)
        if string is not None:
            string_start, string_end = string
            colon = _JSON_COLON_RE.match(text, string_end)
            if string_start == start - 1 and string_end == end + 1 and colon is not None:
                try:
                    value, read_until = _SCAN(text, colon.end())
                except (StopIteration, ValueError):
                    return False, end
                return _holds_nothing(value), read_until
            inner = form.opened_string(string_start, string_end)
            if inner is not None:
                return _is_accounted_for(name, [inner]), string_end
    if start == 0 or text[start - 1] in _PAIR_BOUNDARIES:
        pair = _PAIR_VALUE_RE.match(text, end)
        if pair is not None:
            return _holds_nothing(pair.group(1)), pair.end(1)
    element = form.element_at(start)
    if element is not None:
        _, span_end, _, attributes = element
        folded = name.casefold()
        naming = {
            at
            for at, (key, value) in enumerate(attributes)
            if key in _NAMING_ATTRIBUTES and value.casefold() == folded
        }
        if naming:
            others = (value for at, (_, value) in enumerate(attributes) if at not in naming)
            return all(_holds_nothing(value) for value in others), span_end
    return None, end


def _is_accounted_for(name: str, forms: Sequence[_Form]) -> bool:
    """True when every whole-name occurrence of *name* in every form (found as the
    fixtures check finds a flagged name,
    :func:`graftpunk.testing.decoding.whole_name_regex`) is read by a strict
    reader (:func:`_read`) with a value that holds nothing. An occurrence inside
    a value already read is part of that value, judged whole. Any other
    occurrence (``name: value`` in text that is not JSON, a call's argument, a
    ``{"name": ..., "value": ...}`` pair, an element's text, a table cell, an
    attribute's name, a string the reader cannot open) is read by none, so the
    name is not accounted for."""
    folded = name.casefold()
    pattern = re.compile(whole_name_regex(name))
    for form in forms:
        if folded not in form.folded:
            continue
        read_until = 0
        for start, end in form.occurrences(pattern):
            if start < read_until:
                continue
            verdict, read_until = _read(name, form, start, end)
            if not verdict:
                return False
    return True


def _holds_a_value_under(name: str, forms: Sequence[_Form]) -> bool:
    """True when *name* is not accounted for in *forms* (:func:`_is_accounted_for`),
    or a value under it is too deeply nested to judge."""
    try:
        return not _is_accounted_for(name, forms)
    except RecursionError:
        return True


def checked_names_of(
    flagged_values: Mapping[str, frozenset[str] | None], text: str
) -> tuple[str, ...]:
    """The names in *flagged_values* the fixtures check must look for in a fixture
    derived from *text*, sorted: each name whose values are not all known (None),
    each name one of whose values is in *text*, and each name *text* mentions
    anywhere but in a name-value pair holding nothing.

    A value is looked for in each of its forms
    (:func:`graftpunk.testing.decoding.value_forms`: as recorded, percent-decoded
    with ``+`` read both ways, and without the double quotes a cookie value may
    carry), case-sensitively, in *text* as written, in each form the fixtures
    check reads a fixture in, and in each of those decoded again
    (:func:`graftpunk.testing.decoding.combined_forms`), so a value the body holds
    escaped in a JSON string, HTML-escaped, percent-encoded, or JSON-escaped
    inside an HTML attribute counts. A value of one character that happens to be
    in the body keeps its name looked for: the check then fails on the name as it
    did before sidecar schema 3, never less.

    The recorded values are only what the recording's cookie headers carried
    (:func:`graftpunk.har.digest.flagged_values_of`): a value the cookie took
    after the last request that sent it is in none of them. So a name is left
    out only when every whole-name occurrence of it in *text*, in each of those
    forms, is read by one of three strict readers, and every value read so holds
    nothing:

    - JSON: the name is a key of a JSON object that parses where it starts in
      the text (the whole body, or an object or array within it, such as inline
      state in a script), or of one that is the text of a JSON string there,
      read with the key's parsed value;
    - a query or form pair: ``name=v`` at the start of the text or after one of
      ``?&;"'``, where ``v`` is empty or a run of URL characters (no ``<``,
      ``>``, ``=``, whitespace, quote or backslash) followed by ``&``, ``#``,
      ``;``, the end of the text, or a quote that ends its string, ``v`` judged
      as a string;
    - an element attribute: a void ``input``, ``meta``, ``param`` or ``link``
      element, or a self-closing tag, with a ``value`` or ``content`` attribute,
      whose ``name``, ``id``, ``property``, ``itemprop`` or ``http-equiv`` is
      the name, its attributes in any order; every other attribute value of it
      is judged.

    A value holds nothing when it is a boolean, null, empty, or form state by
    the rule that flags a cookie by its value
    (:func:`graftpunk.har.digest.form_state`), a number judged by its text, an
    object by its keys and values. Any other occurrence (``name: v`` in text
    that is not JSON, an attribute named by the name, an element that holds
    text, a call's argument) keeps the name checked. An honest ``"id": 7`` or
    ``"user": "alice"`` leaves the name unchecked;
    ``"csrftoken": "<an opaque value>"``, ``setCookie('csrftoken', ...)``,
    ``{"name": "csrftoken", ...}`` and ``<li>csrftoken: <code>...</code></li>``
    keep it
    checked."""
    texts = (text, *combined_forms(text))
    forms = [_Form(form) for form in dict.fromkeys(texts)]
    checked: list[str] = []
    for name, values in flagged_values.items():
        if values is None:
            checked.append(name)
            continue
        wanted = {form for value in values for form in value_forms(value)}
        if any(
            value and value in form for value in wanted for form in texts
        ) or _holds_a_value_under(name, forms):
            checked.append(name)
    return tuple(sorted(checked))


def write_sidecar(
    fixture: Path,
    *,
    status: int,
    content_type: str,
    body_params: Iterable[str],
    flagged_reasons: Mapping[str, Iterable[str]],
    flagged_values: Mapping[str, frozenset[str] | None] | None = None,
    redacted_names: int = 0,
) -> Path:
    """Write *fixture*'s committable sidecar beside it and return its path.

    *fixture* must already be on disk: ``capture_sha256`` is the hash of its bytes
    as written, which is what the in-suite check compares a derived fixture to, or
    null when it is empty, since an empty body holds nothing to sanitise.
    *flagged_reasons* maps each flagged name to why it is flagged
    (``graftpunk.har.digest.flagged_reasons_of``); its keys are the flagged
    names. The sort and dedupe is ``Sidecar``'s own job, not this writer's.
    *flagged_values* maps each flagged name to its recorded values
    (``graftpunk.har.digest.flagged_values_of``); ``checked_names`` is the names
    :func:`checked_names_of` finds in *fixture*'s text. Without it every flagged
    name is checked, as a sidecar from before schema 3 is read. A name in it that
    is not flagged is ignored.
    """
    body = fixture.read_bytes()
    names = tuple(flagged_reasons)
    if flagged_values is None:
        checked = names
    else:
        text = body.decode("utf-8", errors="replace")
        # A flagged name the caller has no values for is not known in full.
        known = {name: flagged_values.get(name) for name in names}
        checked = checked_names_of(known, text)
    sidecar = Sidecar(
        status=status,
        content_type=content_type,
        body_params=tuple(body_params),
        # An empty body (a 204, a 3xx with no text) holds nothing to sanitise, and
        # its fixture is empty too, so a recorded hash would fail the suite's
        # unchanged-copy check on a fixture that holds no captured value.
        capture_sha256=hashlib.sha256(body).hexdigest() if body else None,
        flagged_names=names,
        flagged_reasons={name: tuple(why) for name, why in flagged_reasons.items()},
        checked_names=checked,
        redacted_names=redacted_names,
    )
    path = sidecar_path(fixture)
    path.write_text(sidecar_text(sidecar), encoding="utf-8")
    return path
