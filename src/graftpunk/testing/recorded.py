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
from collections.abc import Callable, Iterable, Iterator, Mapping
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qsl

from graftpunk.testing.decoding import combined_forms, decoded_forms, value_forms

__all__ = ["RunUnreadableError", "read_run_har", "recorded_leaks", "recorded_values"]

HAR_NAME = "network.har"
PAGE_SOURCE_NAME = "page-source.html"

# A recorded value's form shorter than this is too common to report: it is in
# most fixtures by chance (USD, Ann). One that holds a digit is reported from
# the second constant's length up (a PIN, a house number, an area code).
_MIN_RECORDED_LENGTH = 4
_MIN_WITH_DIGIT_LENGTH = 3
# A recorded value that, in any of its forms, is one of these JSON literals (in
# any case) is not looked for: a name flagged by its name alone records them
# (logged_in=true), and a fixture cannot invent the literal away.
_LITERALS = frozenset(("true", "false", "null"))
# Lines named per leaked name before the rest are counted.
_MOST_PLACES = 5
_DIGITS = re.compile(r"\d{3,}")
# A key shaped like a name the site chose for its schema, and without a digit, is
# not account data: lowercase or snake_case (account, created_at), camelCase
# (accountNumber), or SCREAMING_SNAKE (NEXT_DATA), with leading or trailing
# underscores (__typename). Any other key (a surname, Okonkwo; an id, cus_Nf3x; a
# token) is a value. A string under __typename that is an identifier is a
# GraphQL type's name (User, Location). Neither is a value the fixture holds, so
# such a key in the fixture is not searched (_without_keys).
_SCHEMA_KEY = re.compile(
    r"_*(?:[a-z]+(?:_+[a-z]+)*|[a-z]+(?:[A-Z][a-z]+)+|[A-Z]+(?:_+[A-Z]+)*)_*\Z"
)
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_TYPENAME = "__typename"
# A camelCase identifier: a lowercase word, then capitalised words (createdAt).
# One equal to a schema key of its own body is that field's name, held as a value
# (a sort order, "sortBy": "createdAt"). A username is lowercase, so a capture's
# "aokonkwo" stays a value even where the same body uses it as a key.
_CAMEL_CASE = re.compile(r"[a-z]+(?:[A-Z][a-z]+)+\Z")
# The parts of a GraphQL response that describe the request rather than answer it:
# an identifier under either that names a schema key or a type of the same body
# (a tracing resolver's fieldName and parentType, an error's requestedFields) is
# the schema's, not account data, and so is such a name standing as a word of a
# longer string there ("Cannot query field displayName on type User").
_GRAPHQL_META = ("errors", "extensions")
# A word of a longer string that can be a name: an identifier with nothing that
# continues a word, an address or a path on either side, so neither part of
# "ann.okafor@myshop.example" nor of "ORD-4471023" is one.
_NAME_WORD = re.compile(r"(?<![\w.@/-])[A-Za-z_][A-Za-z0-9_]*(?![\w.@/-])")
# A JSON or Python key in single or double quotes, JSON-escaped or not, before
# its colon, where an object key stands: after the object's opening brace or a
# comma. A quoted word before a colon anywhere else (a ternary, a case label,
# quoted prose) is not a key.
_QUOTED_KEY = re.compile(r"""([{,]\s*)(\\?["'])([A-Za-z_][A-Za-z0-9_]*)\2\s*:""")
# Around a single word: no letter or digit on either side. An underscore counts as a
# separator, so OKONKWO_ID holds the word OKONKWO.
_BEFORE = r"(?<![^\W_])"
_AFTER = r"(?![^\W_])"


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


def _schema_key(key: str) -> bool:
    """Whether *key* is shaped like a name the site chose for its schema."""
    return _SCHEMA_KEY.match(key) is not None


def _long_enough(value: str) -> bool:
    """Whether *value* is long enough to report: four characters, or three that
    hold a digit."""
    if len(value) >= _MIN_RECORDED_LENGTH:
        return True
    return len(value) >= _MIN_WITH_DIGIT_LENGTH and any(c.isdigit() for c in value)


def _bounded(value: str) -> str:
    """A pattern for *value* with no letter or digit on either side."""
    return rf"{_BEFORE}{re.escape(value)}{_AFTER}"


def _contains(value: str, text: str) -> bool:
    """Whether *text* holds *value*: anywhere, or, for a value shorter than four
    characters, only where no letter or digit sits on either side, so ``482`` is
    not found in ``14829``."""
    if len(value) >= _MIN_RECORDED_LENGTH:
        return value in text
    return re.search(_bounded(value), text) is not None


def _schema_keys(text: str) -> dict[str, list[tuple[int, int, int]]]:
    """Each schema key quoted in key position in *text*, with the 0-based line,
    start and end of each place it stands, in order."""
    keys: dict[str, list[tuple[int, int, int]]] = {}
    line, at = 0, 0
    for match in _QUOTED_KEY.finditer(text):
        key = match.group(3)
        if _schema_key(key):
            start, end = match.span(3)
            line += text.count("\n", at, start)
            at = start
            keys.setdefault(key, []).append((line, start, end))
    return keys


def _key_places(
    keys: dict[str, list[tuple[int, int, int]]], value: str
) -> list[tuple[int, int, int]]:
    """The places in *keys* of a key that is *value* whole, as written or in
    capitals, the forms a single word is matched in (``Okonkwo`` is the key
    ``"OKONKWO":``), in order."""
    places = list(keys.get(value, ()))
    if value.upper() != value:
        places += keys.get(value.upper(), ())
    return sorted(places)


def _without_keys(text: str) -> str:
    """*text* with each schema key quoted in key position blanked in place, so a
    value is found as a value and never inside a key the site named
    (``"recentOrders":`` does not hold ``Order``). A value equal to a whole key
    is looked for in :func:`_schema_keys` instead."""

    def blank(match: re.Match[str]) -> str:
        if not _schema_key(match.group(3)):
            return match.group(0)
        start, end = match.span(3)
        whole = match.group(0)
        at = start - match.start()
        return whole[:at] + " " * (end - start) + whole[at + end - start :]

    return _QUOTED_KEY.sub(blank, text)


def _graphql_name(value: object) -> bool:
    """Whether *value* is a string a GraphQL name can be: an identifier."""
    return isinstance(value, str) and _IDENTIFIER.match(value) is not None


def _type_name(key: object, value: object) -> bool:
    """Whether *value*, under *key*, is a GraphQL type's name."""
    return key == _TYPENAME and _graphql_name(value)


def _without_path_names(error: object) -> object:
    if not isinstance(error, dict) or not isinstance(error.get("path"), list):
        return error
    return {**error, "path": [step for step in error["path"] if not _graphql_name(step)]}


def _schema_names(node: object, keys: set[str], types: set[str]) -> None:
    """Add each schema key in *node*, at any depth, to *keys*, and each type name
    under ``__typename`` to *types*."""
    if isinstance(node, dict):
        for key, value in node.items():
            if _schema_key(str(key)):
                keys.add(str(key))
            if _type_name(key, value):
                types.add(value)
            else:
                _schema_names(value, keys, types)
    elif isinstance(node, list):
        for value in node:
            _schema_names(value, keys, types)


def _without_names(
    node: object, drop: Callable[[str], bool], words: frozenset[str] = frozenset()
) -> object:
    """*node* without each string value, at any depth, that *drop* holds a name,
    and with each word of another string that is in *words* blanked; keys are
    left as they are."""
    if isinstance(node, dict):
        return {key: _without_names(value, drop, words) for key, value in node.items()}
    if isinstance(node, list):
        return [
            _without_names(value, drop, words)
            for value in node
            if not (isinstance(value, str) and drop(value))
        ]
    if isinstance(node, str):
        if drop(node):
            return None
        if words:
            return _NAME_WORD.sub(
                lambda m: " " * len(m.group(0)) if m.group(0) in words else m.group(0), node
            )
    return node


def _without_field_names(document: object) -> object:
    """*document*, a JSON body or one of a batch, without the GraphQL field and
    operation names it holds as values: its ``operationName``, its
    ``extensions.operationName``, the names in each ``errors[*].path``, an
    identifier under ``errors`` or ``extensions`` equal to a schema key or a type
    name of the same body (blanked where it is a word of a longer string there,
    such as an error's message), and a camelCase identifier anywhere equal to a
    schema key of the same body."""
    if not isinstance(document, dict):
        return document
    document = dict(document)
    if _graphql_name(document.get("operationName")):
        document["operationName"] = None
    extensions = document.get("extensions")
    if isinstance(extensions, dict) and _graphql_name(extensions.get("operationName")):
        document["extensions"] = {**extensions, "operationName": None}
    errors = document.get("errors")
    if isinstance(errors, list):
        document["errors"] = [_without_path_names(error) for error in errors]
    keys: set[str] = set()
    types: set[str] = set()
    _schema_names(document, keys, types)
    names = frozenset(keys | types)

    def meta(value: str) -> bool:
        return value in names and _graphql_name(value)

    def field(value: str) -> bool:
        return value in keys and _CAMEL_CASE.match(value) is not None

    for part in _GRAPHQL_META:
        if part in document:
            document[part] = _without_names(document[part], meta, names)
    return _without_names(document, field)


def _json_values(node: object) -> list[str]:
    if isinstance(node, dict):
        keys = [str(k) for k in node if not _schema_key(str(k))]
        return keys + [
            leaf
            for key, value in node.items()
            if not _type_name(key, value)
            for leaf in _json_values(value)
        ]
    if isinstance(node, list):
        return [leaf for value in node for leaf in _json_values(value)]
    if isinstance(node, bool) or node is None:
        return []
    return [str(node)]


def _document_values(data: object) -> list[str]:
    documents = data if isinstance(data, list) else [data]
    return [leaf for document in documents for leaf in _json_values(_without_field_names(document))]


def _text_parts(text: str) -> list[str]:
    parser = _TextParts()
    parser.feed(text)
    parser.close()
    return parser.values


def _body_values(text: str) -> list[str]:
    """The filled-in values of a body, decoded: JSON leaves other than a type name
    under ``__typename`` or a GraphQL field or operation name (``errors[*].path``,
    ``operationName``), and keys other than schema keys; or the text parts of
    anything else."""
    try:
        data = json.loads(text)
    except (ValueError, RecursionError):
        return _text_parts(text)
    return _document_values(data)


def _fixture_forms(text: str) -> tuple[str, dict[str, list[tuple[int, int, int]]], list[str]]:
    """*text* with its schema keys blanked, those keys (:func:`_schema_keys`), and
    its filled-in values. The values are read from *text* itself, since blanked
    keys of one length are one key to a JSON parser. Only a fixture that parses as
    JSON has its keys blanked; any other (a Python module, HTML, an inline script)
    is searched as plain text, keys included."""
    try:
        data = json.loads(text)
    except (ValueError, RecursionError):
        return text, {}, _text_parts(text)
    return _without_keys(text), _schema_keys(text), _document_values(data)


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


def _file_text(
    ref: object, base: Path | None, unreadable: list[str], *, optional: bool = False
) -> str | None:
    """The text of the file *ref* names, relative to the HAR's directory *base*,
    read as UTF-8 as graftpunk's HAR parser reads it. A file that resolves outside
    *base* (a ``..``, an absolute path, or a symlink) gives None. A file inside
    *base* that cannot be read (a permission error, a symlink loop, a directory)
    or is not there (a dangling symlink included) gives None and is added to
    *unreadable*, as its path relative to *base* and the reason, unless it is
    *optional* and not there. gp names a file in the HAR only after writing it,
    so a named file that is gone means the run was changed since."""
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
        if not optional:
            unreadable.append(f"{named.relative_to(root)} (missing)")
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
        RunUnreadableError: A file inside *base* that the HAR names cannot be
            read or is missing, or the run's ``page-source.html`` is there and
            cannot be read; the message names each such file by its path
            relative to *base*.
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
    # gp writes page-source.html only when the browser gave the page at stop.
    add_page(_file_text(PAGE_SOURCE_NAME, base, unreadable, optional=True))
    if unreadable:
        raise RunUnreadableError(_cannot_read(unreadable))
    return found


def _is_literal(value: str) -> bool:
    """True when one of *value*'s forms is a JSON literal, in any case."""
    return any(form.strip().casefold() in _LITERALS for form in value_forms(value))


def recorded_leaks(text: str, values: Mapping[str, Iterable[str]]) -> list[str]:
    """One line per name in *values* whose recorded value is in the fixture
    *text*, naming the lines of *text* that hold it (``line 3``, up to five,
    then a count) or ``found decoded``, never the value.

    Each of a value's forms (:func:`graftpunk.testing.decoding.value_forms`) is
    looked for, compatibility-normalised and case-sensitively, in *text*, in its
    filled-in values (JSON leaves, or an HTML body's text and attribute values),
    and in its decoded forms (:func:`graftpunk.testing.decoding.decoded_forms`),
    then in :func:`graftpunk.testing.decoding.combined_forms`.

    A recorded value is not looked for when it is empty or, in any of those
    forms, ``true``, ``false`` or ``null`` in any case: a name flagged by its
    name alone records such values (``logged_in=true``), and a fixture cannot
    invent the literal away. A form shorter than four characters is not looked
    for either, since it is in most fixtures by chance, unless it has three
    characters and holds a digit; such a form matches only where no letter or
    digit sits on either side (``482`` is not in ``14829``). Every other value is
    looked for, a username, a name or a three-digit PIN included.

    When *text* parses as JSON, a quoted key in key position (after ``{`` or
    ``,``) shaped like a schema name, digit-free lowercase, snake_case, camelCase
    or SCREAMING_SNAKE (``"orderId":``), is blanked in the text and its decoded
    forms: a value is not reported for matching part of such a key. A value equal
    to a whole such key, as written or in capitals, is reported, since it is a
    value used as a key. The filled-in values are read from *text* as written.
    Any other fixture (a Python module, HTML) is searched as plain text, keys
    included, so a value inside a key is reported there."""
    blanked, keys, body = _fixture_forms(text)
    haystack = _normal("\n".join([blanked, "\n".join(body), *decoded_forms(blanked)]))
    raw_lines = blanked.split("\n")
    combined: str | None = None
    lines: list[str] = []
    for name in sorted(values):
        wanted = {
            _normal(form)
            for value in values[name]
            if value and not _is_literal(value)
            for form in value_forms(value)
            if _long_enough(form)
        }
        hits = [value for value in wanted if _contains(value, haystack) or _key_places(keys, value)]
        if not hits and wanted:
            if combined is None:
                combined = _normal("\n".join(combined_forms(blanked)))
            hits = [value for value in wanted if _contains(value, combined)]
        if not hits:
            continue
        keyed = {line + 1 for value in hits for line, _s, _e in _key_places(keys, value)}
        at = [
            str(number)
            for number, line in enumerate(raw_lines, 1)
            if number in keyed or any(_contains(value, _normal(line)) for value in hits)
        ]
        where = f"line {', '.join(at[:_MOST_PLACES])}" if at else "found decoded"
        if len(at) > _MOST_PLACES:
            where += f" and {len(at) - _MOST_PLACES} more lines"
        lines.append(f"the recorded value of flagged {name!r} is still in the fixture ({where})")
    return lines
