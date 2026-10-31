"""The values an observe run recorded under each flagged name, and the check of a
fixture against them.

The fixtures check reads a sidecar's names only, so a recorded value pasted into
a fixture passes it wherever the name is not looked for. From sidecar schema 4
the sidecar names its observe run (``observe_run``), and when that run is on the
machine running the suite, :func:`graftpunk.testing.plugin.check_fixtures_tree`
also reads every value the run recorded for each flagged name
(:func:`recorded_values`) and reports each name whose value is in the fixture
(:func:`recorded_leaks`), by line, never printing the value.

This is the package's one implementation of that comparison. The graft skill's
``skills/graft/scripts/fixture-leaks.py --har`` keeps a copy (``recorded_values``,
``recorded_leaks`` and the readers they use) because it runs with the standard
library alone and cannot import graftpunk; tests hold the two to the same
output. The script also blanks the date and time in the provenance phrase gp
writes into a generated stub, which a fixture never holds, so this does not.

Pytest-free, and it reads only what it is handed: :func:`read_run_har` reads the
run's ``network.har``, and :func:`recorded_values` the files that HAR names
inside the run directory and the run's ``page-source.html``, the files the
digest reads. Nothing here lists a directory.
"""

from __future__ import annotations

import errno
import json
import os
import re
import unicodedata
from collections.abc import Iterable, Iterator, Mapping
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qsl

from graftpunk.har.digest import form_state
from graftpunk.testing.decoding import combined_forms, decoded_forms, value_forms

__all__ = ["RunUnreadableError", "read_run_har", "recorded_leaks", "recorded_values"]

HAR_NAME = "network.har"
PAGE_SOURCE_NAME = "page-source.html"

# A value shorter than this is too common to report: it is in most fixtures by chance.
_MIN_LENGTH = 3
# Lines named per leaked name before the rest are counted.
_MOST_PLACES = 5
_DIGITS = re.compile(r"\d{3,}")
_IDENTIFIER = re.compile(r"[a-z_][a-z0-9_]*\Z")


class RunUnreadableError(Exception):
    """The run is on disk but its HAR cannot be read as one, or a file of the run
    the check reads cannot be read. The message is the reason, names a file by
    its path relative to the run, and names no recorded value."""


# Files named in a RunUnreadableError before the rest are counted.
_MOST_FILES = 3


def _cannot_read(unreadable: list[str]) -> str:
    """The reason a run whose files *unreadable* cannot be read fails, naming the
    first few."""
    files = list(dict.fromkeys(unreadable))
    shown = ", ".join(files[:_MOST_FILES])
    more = f" and {len(files) - _MOST_FILES} more" if len(files) > _MOST_FILES else ""
    return f"cannot read {shown}{more}"


def read_run_har(run_dir: Path) -> object:
    """The parsed ``network.har`` of the run at *run_dir*.

    Raises:
        RunUnreadableError: It is missing, cannot be read, is not JSON, or is not
            a HAR (an object whose ``log.entries`` is a list).
    """
    path = run_dir / HAR_NAME
    try:
        har = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise RunUnreadableError(f"it holds no {HAR_NAME}") from None
    except OSError as exc:
        raise RunUnreadableError(f"{HAR_NAME}: {exc.strerror or type(exc).__name__}") from None
    except (ValueError, RecursionError) as exc:
        raise RunUnreadableError(f"{HAR_NAME} is not JSON ({type(exc).__name__})") from None
    log = har.get("log") if isinstance(har, dict) else None
    if not isinstance(log, dict) or not isinstance(log.get("entries"), list):
        raise RunUnreadableError(f"{HAR_NAME} is not a HAR (no log.entries list)")
    return har


def _normal(text: str) -> str:
    """*text* compatibility-normalised, so a composed and a decomposed accent match."""
    return unicodedata.normalize("NFKC", text)


class _TextParts(HTMLParser):
    """Text nodes, attribute values, and comments of a non-JSON body."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.values: list[str] = []

    def handle_data(self, data: str) -> None:
        self.values.append(data)

    def handle_comment(self, data: str) -> None:
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


def _body_values(text: str) -> list[str]:
    """The filled-in values of a body, decoded: JSON leaves and non-identifier keys,
    or the text parts of anything else."""
    try:
        return _json_values(json.loads(text))
    except (ValueError, RecursionError):
        parser = _TextParts()
        parser.feed(text)
        parser.close()
        return parser.values


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


def _object(value: object) -> dict[str, object]:
    """*value* when it is a JSON object, else an empty one."""
    return value if isinstance(value, dict) else {}


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


def _file_text(ref: object, base: Path | None, unreadable: list[str]) -> str | None:
    """The text of the file *ref* names, relative to the HAR's directory *base*,
    read as UTF-8 as graftpunk's HAR parser reads it. A file that resolves outside
    *base* (a ``..``, an absolute path, or a symlink) or is not there gives None.
    A file inside *base* that is there and cannot be read (a permission error, a
    symlink loop, a directory) gives None and is added to *unreadable*, as its
    path relative to *base* and the reason."""
    if base is None or not isinstance(ref, str) or not ref:
        return None
    try:
        root = base.resolve()
    except (OSError, RuntimeError):
        return None
    named = Path(os.path.normpath(root / ref))
    if not named.is_relative_to(root):
        return None
    try:
        # Before Python 3.13, resolve() raises RuntimeError on a symlink loop.
        path = (root / ref).resolve()
        if not path.is_relative_to(root):
            return None
        return path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return None
    except OSError as exc:
        reason = "a symlink loop" if exc.errno == errno.ELOOP else exc.strerror
        unreadable.append(f"{named.relative_to(root)} ({reason or type(exc).__name__})")
    except RuntimeError:
        unreadable.append(f"{named.relative_to(root)} (a symlink loop)")
    return None


def _body_text(content: dict[str, object], base: Path | None, unreadable: list[str]) -> str | None:
    """A response's body text: the HAR's ``content.text``, or, when the HAR keeps
    the body in a file (``content._bodyFile``), that file (:func:`_file_text`)."""
    text = content.get("text")
    if isinstance(text, str):
        return text
    return _file_text(content.get("_bodyFile"), base, unreadable)


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


def _body_fields(
    request: dict[str, object], base: Path | None, unreadable: list[str]
) -> list[tuple[str, str]]:
    """``(name, value)`` for each field a request body sends: the HAR's
    ``postData.params``, and its text (``postData.text``, or the file
    ``_postDataFile`` names) read as JSON, else as a form-encoded body."""
    post = _object(request.get("postData"))
    pairs = _named(post.get("params"))
    text = post.get("text")
    if not isinstance(text, str):
        text = _file_text(request.get("_postDataFile"), base, unreadable)
    if text:
        try:
            pairs += list(_json_pairs(json.loads(text)))
        except (ValueError, RecursionError):
            pairs += parse_qsl(text, keep_blank_values=False)
    return pairs


def recorded_values(
    har: object, names: Iterable[str], base: Path | None = None
) -> dict[str, set[str]]:
    """Every non-empty value *har* records for each of *names* that this reads:
    as a cookie sent or set (the cookie arrays, and the ``Cookie`` and
    ``Set-Cookie`` headers), as a request or response header (any case), as a
    field a request body sends, or as a meta tag's content or a hidden input's
    value in a response whose ``Content-Type`` header says HTML. With *base*, the
    HAR's directory, a body or request body the HAR keeps in a file is read, and
    so are gp's DOM snapshots (``_domSnapshot``, ``_domAfter``) and the run's
    ``page-source.html`` beside the HAR, each only inside *base*.

    Raises:
        RunUnreadableError: A file inside *base* that the HAR names, or the
            run's ``page-source.html``, is there and cannot be read; the message
            names each such file by its path relative to *base*.
    """
    wanted = set(names)
    unreadable: list[str] = []
    found: dict[str, set[str]] = {}
    # Several flagged names can differ in case alone (X-CSRF-Token, x-csrf-token).
    folded: dict[str, list[str]] = {}
    for name in wanted:
        folded.setdefault(name.casefold(), []).append(name)

    def add(name: str, value: str) -> None:
        if name in wanted and value:
            found.setdefault(name, set()).add(value)

    def add_page(html: str | None) -> None:
        for name, value in _page_values(html) if html is not None else ():
            add(name, value)

    log = har.get("log") if isinstance(har, dict) else None
    entries = log.get("entries") if isinstance(log, dict) else None
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            continue
        request = _object(entry.get("request"))
        response = _object(entry.get("response"))
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
        for name, value in _body_fields(request, base, unreadable):
            add(name, value)
        add_page(_file_text(request.get("_domSnapshot"), base, unreadable))
        add_page(_file_text(request.get("_domAfter"), base, unreadable))
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
        content = _object(response.get("content"))
        add_page(_body_text(content, base, unreadable))
    add_page(_file_text(PAGE_SOURCE_NAME, base, unreadable))
    if unreadable:
        raise RunUnreadableError(_cannot_read(unreadable))
    return found


def recorded_leaks(text: str, values: Mapping[str, Iterable[str]]) -> list[str]:
    """One line per name in *values* whose recorded value of three or more
    characters is in the fixture *text*, naming the lines of *text* that hold it
    (``line 3``, up to five, then a count) or ``found decoded``, never the value.

    Each of a value's forms (:func:`graftpunk.testing.decoding.value_forms`) is
    looked for, compatibility-normalised and case-sensitively, in *text*, in its
    filled-in values (JSON leaves, or an HTML body's text and attribute values),
    and in its decoded forms (:func:`graftpunk.testing.decoding.decoded_forms`),
    then in :func:`graftpunk.testing.decoding.combined_forms`.

    A recorded value that holds nothing is not looked for: an empty one, or form
    state by the rule a cookie value is judged by
    (:func:`graftpunk.har.digest.form_state`), the rule
    :func:`graftpunk.devtools.captures.checked_names_of` judges a value by. A
    name flagged by its name alone records such values (``logged_in=true``,
    ``currency=USD``), and a fixture cannot invent ``true`` away."""
    haystack = _normal("\n".join([text, "\n".join(_body_values(text)), *decoded_forms(text)]))
    raw_lines = text.split("\n")
    combined: str | None = None
    lines: list[str] = []
    for name in sorted(values):
        wanted = {
            _normal(form)
            for value in values[name]
            if value and not form_state(value)
            for form in value_forms(value)
            if len(form) >= _MIN_LENGTH
        }
        hits = [value for value in wanted if value in haystack]
        if not hits and wanted:
            if combined is None:
                combined = _normal("\n".join(combined_forms(text)))
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
