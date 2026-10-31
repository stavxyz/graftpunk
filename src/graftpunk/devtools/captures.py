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


_JSON = json.JSONDecoder()
# A value written after a name and a ':' or '=' that is not JSON: a quoted
# string, or a run up to the next delimiter of a query, a form body, an object
# or a tag.
_BARE_VALUE_RE = re.compile(r"""'((?:[^'\\]|\\.)*)'|"([^"]*)"|([^\s,;&}\]<>"'()]*)""")
# The characters that can continue a value after a JSON number or literal, so
# ``7abc`` is not read as the number 7.
_CONTINUES_RE = re.compile(r"[\w.%+-]")
# Attributes naming an element whose value or content attribute holds the
# named value: <input name="x" value="...">, <meta name="x" content="...">.
_NAMING_ATTRIBUTES = frozenset({"name", "id", "property", "itemprop", "http-equiv"})
_VALUE_ATTRIBUTES = frozenset({"value", "content"})


class _HTMLValuesUnder(HTMLParser):
    """The values an HTML text holds under one name as an element's name: the
    ``value`` or ``content`` of an element a naming attribute gives that name. An
    attribute of that name is read as ``name="value"`` by :func:`_values_under`."""

    def __init__(self, folded: str) -> None:
        super().__init__()
        self.folded = folded
        self.values: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        named = any(
            key in _NAMING_ATTRIBUTES and (value or "").casefold() == self.folded
            for key, value in attrs
        )
        if named:
            self.values.extend(value or "" for key, value in attrs if key in _VALUE_ATTRIBUTES)

    handle_startendtag = handle_starttag


def _value_at(form: str, pos: int) -> object:
    """The value that starts at *pos* in *form*: a JSON value when one starts
    there (a string, a number or literal not run on into a longer token, an
    object or an array), else the quoted or bare run :data:`_BARE_VALUE_RE`
    reads."""
    try:
        value, end = _JSON.raw_decode(form, pos)
    except ValueError:
        pass
    else:
        if isinstance(value, (str, dict, list)) or not _CONTINUES_RE.match(form, end):
            return value
    match = _BARE_VALUE_RE.match(form, pos)
    if match is None:
        return ""
    return next(group for group in match.groups() if group is not None)


def _values_under(name: str, forms: Sequence[str]) -> Iterator[object]:
    """Every value *forms* (a body in each form it is read in) hold under *name*,
    matched without regard to case: written after the name as a JSON key, a
    form field or query parameter, an HTML attribute or a script's assignment
    (``"name": v``, ``name=v``, ``'name': v``), and the ``value`` or ``content``
    of an HTML element named so."""
    folded = name.casefold()
    # The name where the fixtures check would find it, in or out of quotes,
    # JSON-escaped ones included, then the ':' or '=' before its value.
    after = re.compile(
        rf"""(?:\\?["'])?{whole_name_regex(name)}(?:\\?["'])?\s*[:=]\s*""", re.IGNORECASE
    )
    # A JSON text inside a JSON or script string is read with the escapes off
    # its quotes too, as the inner text is written.
    unescaped = [form.replace('\\"', '"') for form in forms if '\\"' in form]
    for form in (*forms, *unescaped):
        if folded not in form.casefold():
            continue
        for match in after.finditer(form):
            yield _value_at(form, match.end())
        parser = _HTMLValuesUnder(folded)
        parser.feed(form)
        parser.close()
        yield from parser.values


def _holds_nothing(value: object) -> bool:
    """True when *value* holds no credential: a number, a boolean, null, an empty
    string, a string that is form state by the rule a cookie value is judged by
    (:func:`graftpunk.har.digest.form_state`), or an object or array whose every
    value is one of those."""
    if value is None or isinstance(value, (bool, int, float)):
        return True
    if isinstance(value, str):
        return not value or form_state(value)
    if isinstance(value, dict):
        return all(_holds_nothing(item) for item in value.values())
    if isinstance(value, list):
        return all(_holds_nothing(item) for item in value)
    return False


def _holds_a_value_under(name: str, forms: Sequence[str]) -> bool:
    """True when *forms* hold a value under *name* (:func:`_values_under`) that
    may be a credential (:func:`_holds_nothing` is False), or one too deeply
    nested to judge."""
    try:
        return not all(_holds_nothing(value) for value in _values_under(name, forms))
    except RecursionError:
        return True


def checked_names_of(
    flagged_values: Mapping[str, frozenset[str] | None], text: str
) -> tuple[str, ...]:
    """The names in *flagged_values* the fixtures check must look for in a fixture
    derived from *text*, sorted: each name whose values are not all known (None),
    each name one of whose values is in *text*, and each name *text* holds a
    value under that is not form state.

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
    after the last request that sent it is in none of them. So a name also stays
    checked when *text*, in any of those forms, holds a value under it (as a
    JSON key, a form field or query parameter, an HTML attribute, or an HTML
    element's name) that is not a number, a boolean, null, empty, or form state
    by the rule that flags a cookie by its value
    (:func:`graftpunk.har.digest.form_state`), an object or array being judged by
    every value in it. An honest ``"id": 7`` or ``"user": "alice"`` leaves the
    name unchecked; ``"csrftoken": "<an opaque value>"`` keeps it checked."""
    forms = (text, *combined_forms(text))
    checked: list[str] = []
    for name, values in flagged_values.items():
        if values is None:
            checked.append(name)
            continue
        wanted = {form for value in values for form in value_forms(value)}
        if any(
            value and value in form for value in wanted for form in forms
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
