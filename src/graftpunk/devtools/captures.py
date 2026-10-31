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
# After a whole name: an optional closing quote (JSON-escaped included), then the
# ':' or '=' before the value.
_SEPARATOR_RE = re.compile(r"""(?:\\?["'])?\s*[:=]\s*""")
# A value after a separator that is not JSON: a single-quoted string, or a
# double-quoted one JSON-escaped with no other escape in it, else a run up to
# the next delimiter of a query, a form body, an object or a tag.
_QUOTED_RE = re.compile(r"""'((?:[^'\\]|\\.)*)'|\\"([^"\\]*)\\["]""")
_DELIMITER_RE = re.compile(r"""[\s,;&}\]<>"'()]""")
# The characters that can continue a value after a JSON number or literal, so
# ``7abc`` is not read as the number 7.
_CONTINUES_RE = re.compile(r"[\w.%+-]")
# What may follow a value read whole: a delimiter, a closing quote (escaped
# included), or the end. Anything else (``"a" + b``, the rest of a string the
# value only began) means the value was not read whole.
_VALUE_ENDS_RE = re.compile(r"""\s*(?:[,;&}\]<>)"']|\\["']|\Z)""")
# Attributes naming an element whose value or content attribute holds the
# named value: <input name="x" value="...">, <meta name="x" content="...">.
_NAMING_ATTRIBUTES = frozenset({"name", "id", "property", "itemprop", "http-equiv"})
_VALUE_ATTRIBUTES = frozenset({"value", "content"})
# One attribute with a value in a start tag's text.
_ATTRIBUTE_RE = re.compile(r"""([^\s"'<>/=]+)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'<>`=]+))""")


class _Unreadable:
    """A value the reader could not read: it holds whatever the body put there."""


_UNREADABLE = _Unreadable()


class _NamedElements(HTMLParser):
    """Each start tag of an HTML text that a naming attribute names and that has a
    ``value`` or ``content`` attribute, by the span of the naming attribute's
    value in the text: that span, and the values the element holds."""

    def __init__(self, text: str) -> None:
        super().__init__()
        self.line_starts = [0, *(m.end() for m in re.finditer("\n", text))]
        self.spans: dict[tuple[int, int], tuple[str, ...]] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        names = {(value or "").casefold() for key, value in attrs if key in _NAMING_ATTRIBUTES}
        values = tuple(value or "" for key, value in attrs if key in _VALUE_ATTRIBUTES)
        if not names or not values:
            return
        source = self.get_starttag_text() or ""
        line, offset = self.getpos()
        start = self.line_starts[line - 1] + offset
        for match in _ATTRIBUTE_RE.finditer(source, 1 + len(tag)):
            if match.group(1).casefold() not in _NAMING_ATTRIBUTES:
                continue
            group = next(g for g in (2, 3, 4) if match.group(g) is not None)
            if match.group(group).casefold() in names:
                self.spans[(start + match.start(group), start + match.end(group))] = values

    handle_startendtag = handle_starttag


class _Form:
    """One form a body is read in: its text, the text casefolded with the offset
    in the text each casefolded character came from (None when casefolding kept
    every offset), and its named elements, parsed on first use."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.folded = text.casefold()
        self.origin: list[int] | None = None
        if len(self.folded) != len(text):
            self.origin = [i for i, ch in enumerate(text) for _ in ch.casefold()]
        self._named: dict[tuple[int, int], tuple[str, ...]] | None = None

    def occurrences(self, pattern: re.Pattern[str]) -> Iterator[tuple[int, int]]:
        """The span in the text of each match of *pattern* in the casefolded text."""
        for match in pattern.finditer(self.folded):
            start, end = match.span()
            if self.origin is None:
                yield start, end
            else:
                yield self.origin[start], self.origin[end - 1] + 1

    def named(self) -> dict[tuple[int, int], tuple[str, ...]]:
        """:class:`_NamedElements`' spans for the text, parsed once."""
        if self._named is None:
            parser = _NamedElements(self.text)
            if "<" in self.text:
                parser.feed(self.text)
                parser.close()
            self._named = parser.spans
        return self._named


def _value_at(form: str, pos: int) -> tuple[object, int]:
    """The value that starts at *pos* in *form* and where it ends, if it is read
    whole (:data:`_VALUE_ENDS_RE` follows it): a JSON value when one starts there
    (a string, an object or array, or a number or literal not run on into a
    longer token, a number as its text), else a quoted string
    (:data:`_QUOTED_RE`) or the run up to the next delimiter.
    :data:`_UNREADABLE` for a value not read whole, a string, object or array
    that is not JSON, a run holding a backslash, and an empty run stopped at a
    single quote that no other closes."""
    value, end = _value_starting_at(form, pos)
    if not _VALUE_ENDS_RE.match(form, end):
        return _UNREADABLE, end
    return value, end


def _value_starting_at(form: str, pos: int) -> tuple[object, int]:
    """:func:`_value_at`'s value and end, before the check that it was read whole."""
    if form.startswith(("{", "[", '"'), pos):
        try:
            return _SCAN(form, pos)
        # Raised once per name and form: the first unreadable value ends the
        # reading, and a JSONDecodeError counts the lines before it.
        except (StopIteration, ValueError):
            return _UNREADABLE, pos
    try:
        value, end = _SCAN(form, pos)
    except StopIteration:
        pass
    else:
        if not _CONTINUES_RE.match(form, end):
            return value, end
    quoted = _QUOTED_RE.match(form, pos)
    if quoted is not None:
        return next(group for group in quoted.groups() if group is not None), quoted.end()
    delimiter = _DELIMITER_RE.search(form, pos)
    end = delimiter.start() if delimiter is not None else len(form)
    bare = form[pos:end]
    if "\\" in bare or (not bare and form.startswith("'", pos)):
        return _UNREADABLE, end
    return bare, end


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


def _is_accounted_for(name: str, forms: Sequence[_Form]) -> bool:
    """True when every whole-name occurrence of *name* in every form (found as the
    fixtures check finds a flagged name,
    :func:`graftpunk.testing.decoding.whole_name_regex`) is read with a value
    that holds nothing (:func:`_holds_nothing`): a name followed by ``:`` or
    ``=`` and a value (``"name": v``, ``name=v``, ``'name': v``, an HTML
    attribute ``name="v"``), or the naming attribute's value of an HTML element
    with a ``value`` or ``content`` (``<input name="name" value="v">``). An
    occurrence inside a value already read under the name is part of a value
    judged to hold nothing. Any other occurrence (a call's argument, a
    ``{"name": ..., "value": ...}`` pair, an element's text, a table cell, a
    string the reader cannot open) is not accounted for, so neither is the
    name."""
    folded = name.casefold()
    pattern = re.compile(whole_name_regex(name))
    for form in forms:
        if folded not in form.folded:
            continue
        read_until = 0
        for start, end in form.occurrences(pattern):
            if start < read_until:
                continue
            separator = _SEPARATOR_RE.match(form.text, end)
            if separator is not None:
                value, read_until = _value_at(form.text, separator.end())
                if not _holds_nothing(value):
                    return False
                continue
            values = form.named().get((start, end))
            if values is None or not all(_holds_nothing(value) for value in values):
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
    forms, is a name-value pair the reader parses (``"name": v`` in JSON,
    ``name=v`` in a query or form body, an HTML element a ``name``, ``id``,
    ``property``, ``itemprop`` or ``http-equiv`` attribute names, with a
    ``value`` or ``content``) and every value read so holds nothing: a boolean,
    null, empty, or form state by the rule that flags a cookie by its value
    (:func:`graftpunk.har.digest.form_state`), a number judged by its text, an
    object by its keys and values. An honest ``"id": 7`` or ``"user": "alice"``
    leaves the name unchecked; ``"csrftoken": "<an opaque value>"``,
    ``setCookie('csrftoken', ...)`` and ``{"name": "csrftoken", ...}`` keep it
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
