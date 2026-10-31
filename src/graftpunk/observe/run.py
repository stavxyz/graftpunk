"""Observe run lifecycle helpers shared by the CLI, BrowserSession and the login engine."""

from __future__ import annotations

import base64
import binascii
import datetime
import json
import os
import re
import stat
import tempfile
import urllib.parse
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from requests.utils import requote_uri

from graftpunk.logging import get_logger
from graftpunk.observe.storage import ObserveStorage, write_private_file

# The identifier allowlist lives with the account label, which derives from the
# same definition (#293); it is re-exported here for existing importers.
from graftpunk.session_identity import IDENTIFIER_FIELD_NAMES as IDENTIFIER_FIELD_NAMES
from graftpunk.session_identity import is_identifier_field

LOG = get_logger(__name__)

REDACTED = "***REDACTED***"
# Secrets shorter than this are not redacted: replacing every "ab" in a HAR
# would mangle it beyond use, and such values are not meaningfully secret.
_MIN_REDACT_LEN = 3
# An HTTP Basic credential: the scheme, then base64 of "user:password". A JSON
# body may escape each "/" of the token as "\/"; a URL-encoded one writes the
# space as "%20" or "+" and "+", "/" and "=" as "%2B", "%2F" and "%3D".
_BASIC_RE = re.compile(
    r"(\bBasic(?:\s+|%20|\+))((?:[A-Za-z0-9+/=]|\\/|%2[BbFf]|%3[Dd])+)", re.IGNORECASE
)

# The HAR fields that hold a URL (or its query) rather than a body or header.
_URL_FIELDS = frozenset({"url", "queryString", "redirectURL"})


@dataclass(frozen=True)
class Redaction:
    """What a run is scrubbed of.

    *secrets* are replaced everywhere, wherever they occur. *identifiers* (a
    username, an email) are replaced only where they stand as a whole value,
    and never in the HAR's URL fields (``request.url``,
    ``request.queryString`` and ``response.redirectURL``), so the endpoints
    the digest reads stay readable and a short username does not eat into
    other words.
    """

    secrets: tuple[str, ...] = ()
    identifiers: tuple[str, ...] = ()

    @classmethod
    def from_credentials(cls, credentials: Mapping[str, str]) -> Redaction:
        """Every credential value, an identifier field's as an identifier and the
        rest as secrets, by :func:`is_identifier_field` (an allowlist)."""
        secrets: list[str] = []
        identifiers: list[str] = []
        for name, value in credentials.items():
            if not value:
                continue
            (identifiers if is_identifier_field(name) else secrets).append(str(value))
        return cls(tuple(secrets), tuple(identifiers))

    @property
    def everything(self) -> tuple[str, ...]:
        """Secrets and identifiers together."""
        return self.secrets + self.identifiers

    def __bool__(self) -> bool:
        return bool(self.secrets or self.identifiers)


def as_redaction(redact: Redaction | Iterable[str]) -> Redaction:
    """*redact* as a :class:`Redaction`; plain values are all secrets."""
    if isinstance(redact, Redaction):
        return redact
    return Redaction(tuple(str(v) for v in redact if v))


def make_run_id() -> str:
    """Return a timestamp+pid run id (unique per process per second)."""
    return datetime.datetime.now().strftime("%Y%m%d-%H%M%S") + f"-{os.getpid()}"


def _secret_forms(secret: str) -> set[str]:
    """Every encoding of ``secret`` that can appear on the wire in a login flow."""
    return {
        secret,
        urllib.parse.quote_plus(secret),
        urllib.parse.quote(secret, safe=""),
        # Twice: a return URL inside a return URL.
        urllib.parse.quote(urllib.parse.quote(secret, safe=""), safe=""),
        json.dumps(secret)[1:-1],
        base64.b64encode(secret.encode()).decode(),
        # What requests makes of a value put into the URL string itself (a
        # path segment, or a query written into the URL): "/", "@", "!" and
        # the like stay raw, a space or "|" is percent-encoded.
        requote_uri(secret),
    }


def redact_har_entries(
    entries: list[dict[str, Any]], secrets: Redaction | Iterable[str]
) -> list[dict[str, Any]]:
    """Return ``entries`` with every occurrence of ``secrets`` replaced.

    A login run's HAR carries the credentials verbatim: in the form POST body,
    possibly in a query string, in ``Authorization`` headers, and echoed by
    some sites into response bodies. Replace each secret -- raw, URL-encoded,
    JSON-escaped and base64 -- in every string of every entry, longest forms
    first so a partial encoding does not leave a fragment behind. An
    identifier (see :class:`Redaction`) is replaced in the same forms, but
    only as a whole value and not in the URL fields. Before either, an HTTP
    Basic credential (``Basic <base64 of user:password>``, the scheme in any
    case) whose decoded text contains a credential is replaced whole,
    wherever it appears: a secret's own base64 is found inside
    ``user:password`` only for some username lengths.

    Args:
        entries: HAR 1.2 entry dicts (not mutated).
        secrets: A :class:`Redaction`, or values that are all secrets. Empty
            and very short values are skipped.
    """
    scrubber = _Scrubber.of(secrets)
    if not scrubber:
        return entries

    def scrub(value: Any, in_url: bool = False) -> Any:
        if isinstance(value, str):
            return scrubber.scrub_text(value, in_url=in_url)
        if isinstance(value, dict):
            return {k: scrub(v, in_url or k in _URL_FIELDS) for k, v in value.items()}
        if isinstance(value, list):
            return [scrub(v, in_url) for v in value]
        return value

    return scrub(entries)


def redact_text(text: str, secrets: Redaction | Iterable[str], *, in_url: bool = False) -> str:
    """*text* with *secrets* replaced as :func:`redact_har_entries` replaces them
    in one HAR string; *in_url* keeps identifiers, as in the HAR's URL fields."""
    return text_redactor(secrets, in_url=in_url)(text)


def text_redactor(
    secrets: Redaction | Iterable[str], *, in_url: bool = False
) -> Callable[[str], str]:
    """:func:`redact_text` for *secrets*, compiled once for many strings."""
    scrubber = _Scrubber.of(secrets)
    if not scrubber:
        return lambda text: text
    return lambda text: scrubber.scrub_text(text, in_url=in_url)


def _kept_secrets(secrets: Iterable[str]) -> tuple[str, ...]:
    """The secrets long enough to redact."""
    return tuple(s for s in secrets if s and len(s) >= _MIN_REDACT_LEN)


def _redaction_forms(secrets: Iterable[str]) -> list[str]:
    """Every wire form of each secret long enough to redact, longest first."""
    forms: set[str] = set()
    for secret in _kept_secrets(secrets):
        forms |= _secret_forms(secret)
    return sorted(forms, key=len, reverse=True)


# A whole value: one not inside a longer word. A word is ASCII letters and
# digits: next to anything else (punctuation, or text in another script, such
# as "用户bob" or "bobé"), the value stands alone. Neighbours are read decoded:
# an escape ("%XX", "%25XX" for a double-encoded URL, the JSON escapes "\n",
# "\r", "\t", "\b", "\f" and "\uXXXX") is the character it stands for, so
# "%3Dbob" and "\nbob" are delimited and "%41bob" ("Abob") is not.
_PERCENT_BEFORE = re.compile(r"%(?:25)?([0-9A-Fa-f]{2})$")
_UNICODE_BEFORE = re.compile(r"(\\+)u([0-9A-Fa-f]{4})$")
_CONTROL_BEFORE = re.compile(r"(\\+)[nrtbf]$")
_PERCENT_AFTER = re.compile(r"%(?:25)?([0-9A-Fa-f]{2})")


def _is_word_char(char: str) -> bool:
    """An ASCII letter or digit. The same rule for text and for bytes read as
    latin-1, where a byte over 0x7F is part of a multi-byte character."""
    return char.isascii() and char.isalnum()


def _delimited_before(text: str, start: int) -> bool:
    """Whether the text before *start* ends a word: nothing, a character that
    is not an ASCII letter or digit, or an escape that decodes to one."""
    if start == 0 or not _is_word_char(text[start - 1]):
        return True
    head = text[max(0, start - 16) : start]
    percent = _PERCENT_BEFORE.search(head)
    if percent:
        return not _is_word_char(chr(int(percent.group(1), 16)))
    unicode = _UNICODE_BEFORE.search(head)
    if unicode and len(unicode.group(1)) % 2 == 1:
        return not _is_word_char(chr(int(unicode.group(2), 16)))
    control = _CONTROL_BEFORE.search(head)
    # An odd run of backslashes is an escape; an even one is escaped backslashes.
    return bool(control and len(control.group(1)) % 2 == 1)


def _delimited_after(text: str, end: int) -> bool:
    """Whether the text from *end* starts a new word: nothing, or a character
    that is not an ASCII letter or digit nor an escape of one."""
    if end == len(text):
        return True
    if _is_word_char(text[end]):
        return False
    percent = _PERCENT_AFTER.match(text, end)
    return not (percent and _is_word_char(chr(int(percent.group(1), 16))))


def _replace_whole(
    text: str, forms: tuple[str, ...], pattern: re.Pattern[str], replacement: Any
) -> str:
    """*text* with each whole-value occurrence of *forms* replaced by
    *replacement* (a string, or a function of the matched text).

    *pattern* finds where a form starts; at each such place every form that
    occurs there is tried, longest first, so when ``bob@example.com`` is
    inside a longer word, ``bob`` before its ``@`` is still replaced.
    """
    out: list[str] = []
    pos = 0
    while True:
        match = pattern.search(text, pos)
        if match is None:
            break
        start = match.start()
        for form in forms:  # longest first
            end = start + len(form)
            if (
                text.startswith(form, start)
                and _delimited_before(text, start)
                and _delimited_after(text, end)
            ):
                out.append(text[pos:start])
                out.append(replacement(form) if callable(replacement) else replacement)
                pos = end
                break
        else:
            out.append(text[pos : start + 1])
            pos = start + 1
    out.append(text[pos:])
    return "".join(out)


@dataclass(frozen=True)
class _Scrubber:
    """A :class:`Redaction` compiled for scrubbing text and bytes."""

    secret_forms: tuple[str, ...]
    identifier_forms: tuple[str, ...]
    identifier_byte_forms: tuple[str, ...]
    identifier_re: re.Pattern[str] | None
    identifier_bytes_re: re.Pattern[str] | None
    basic_values: tuple[str, ...]

    @classmethod
    def of(cls, redact: Redaction | Iterable[str]) -> _Scrubber:
        redaction = as_redaction(redact)
        forms = tuple(_redaction_forms(redaction.identifiers))
        # Bytes are matched as latin-1 text, one character per byte.
        byte_forms = tuple(f.encode("utf-8").decode("latin-1") for f in forms)
        return cls(
            secret_forms=tuple(_redaction_forms(redaction.secrets)),
            identifier_forms=forms,
            identifier_byte_forms=byte_forms,
            identifier_re=re.compile("|".join(re.escape(f) for f in forms)) if forms else None,
            identifier_bytes_re=(
                re.compile("|".join(re.escape(f) for f in byte_forms)) if forms else None
            ),
            basic_values=_kept_secrets(redaction.everything),
        )

    def __bool__(self) -> bool:
        return bool(self.secret_forms or self.identifier_re)

    def scrub_text(self, value: str, *, in_url: bool = False) -> str:
        value = _scrub_basic(value, self.basic_values)
        for form in self.secret_forms:
            if form in value:
                value = value.replace(form, REDACTED)
        if self.identifier_re is not None and not in_url:
            value = _replace_whole(value, self.identifier_forms, self.identifier_re, REDACTED)
        return value

    def scrub_bytes(self, raw: bytes) -> bytes:
        raw = _scrub_bytes(raw, list(self.secret_forms))
        if self.identifier_bytes_re is not None:
            text = _replace_whole(
                raw.decode("latin-1"),
                self.identifier_byte_forms,
                self.identifier_bytes_re,
                lambda m: "*" * len(m),
            )
            raw = text.encode("latin-1")
        return raw


def _scrub_basic(value: str, secrets: tuple[str, ...]) -> str:
    """*value* with each Basic credential whose decoded text holds a secret replaced."""

    def replace(match: re.Match[str]) -> str:
        try:
            token = urllib.parse.unquote(match.group(2).replace("\\/", "/"))
            decoded = base64.b64decode(token, validate=True)
        except (binascii.Error, ValueError):
            return match.group(0)
        text = decoded.decode("utf-8", errors="replace")
        if any(secret in text for secret in secrets):
            return match.group(1) + REDACTED
        return match.group(0)

    return _BASIC_RE.sub(replace, value)


def _replace_file(path: Path, data: bytes) -> None:
    """Write *data* over *path* atomically: a temporary file in the same directory,
    then a rename, so a failed write leaves the original whole, never truncated.
    The file keeps its mode: mkstemp creates the temporary file as 0600."""
    mode = stat.S_IMODE(path.stat().st_mode)
    fd, temp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.chmod(temp_name, mode)
        os.replace(temp_name, path)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise


def _scrub_bytes(raw: bytes, ordered: list[str]) -> bytes:
    """*raw* with each form's UTF-8 bytes masked by as many ``*`` bytes.

    The same forms the text scrub uses (raw, URL-encoded, JSON-escaped and
    base64), matched as bytes, so a secret inside a binary body (a multipart
    login with a file part, say) is not left in it. The mask keeps the length,
    so a length-prefixed binary format keeps its offsets.
    """
    for form in sorted({f.encode("utf-8") for f in ordered}, key=len, reverse=True):
        if form in raw:
            raw = raw.replace(form, b"*" * len(form))
    return raw


def redact_request_body_files(
    entries: list[dict[str, Any]], secrets: Redaction | Iterable[str], run_dir: Path
) -> None:
    """Scrub *secrets* from the request bodies the recorder wrote to ``bodies/``.

    A request body over the HAR's cap, or one that is not text, is written to
    the run's ``bodies/`` and named by ``request._postDataFile`` instead of
    sitting in ``postData``, where :func:`redact_har_entries` would scrub it. A
    login POST large enough to land there carries the password just the same.
    A file that decodes as UTF-8 is scrubbed with the same forms and the same
    replacement as the HAR, and the entry's ``_postDataSize`` updated (*entries*
    is mutated). A binary file has each form's bytes masked in place
    (:func:`_scrub_bytes`), and the file that changed is logged by name. A file
    is rewritten only when something was found, and atomically. A file that
    cannot be read or rewritten is logged and skipped; the rest are still
    scrubbed. A reference outside *run_dir*, or to a missing file, is skipped.
    Response bodies in ``bodies/`` are not scanned here.
    """
    scrubber = _Scrubber.of(secrets)
    if not scrubber:
        return
    root = run_dir.resolve()
    for entry in entries:
        request = entry.get("request") if isinstance(entry, dict) else None
        if not isinstance(request, dict):
            continue
        ref = request.get("_postDataFile")
        if not isinstance(ref, str):
            continue
        path = (root / ref).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            continue
        try:
            raw = path.read_bytes()
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                masked = scrubber.scrub_bytes(raw)
                if masked != raw:
                    _replace_file(path, masked)
                    LOG.info("request_body_file_scrubbed", file=ref, binary=True)
                continue
            scrubbed = scrubber.scrub_text(text)
            if scrubbed != text:
                data = scrubbed.encode("utf-8")
                _replace_file(path, data)
                request["_postDataSize"] = len(data)
        except OSError as exc:
            LOG.warning(
                "request_body_file_not_scrubbed",
                file=ref,
                reason="error",
                exc_type=type(exc).__name__,
            )


@dataclass(frozen=True)
class CredentialScrub:
    """What :func:`credential_post_redaction` found in a recorded run.

    Attributes:
        redaction: The values to scrub from the whole run, each with its other
            spellings.
        post_only: Password values that are also part of a credential post's
            field name (``password``, ``pass``, ``login``): scrubbing them
            everywhere would rename the fields the digest finds the login by, so
            they are replaced only where they are a whole value under a
            password-like name in a credential post's own body
            (:func:`scrub_credential_post_bodies`).
        post_indices: The HAR entry indices of the credential posts.
        values: How many distinct values are scrubbed from the whole run.
        left: How many distinct values are left in the run: shorter than the
            scrub's minimum (``_MIN_REDACT_LEN``, 3 characters), or an
            identifier that is part of a field name.
        unread: How many POST bodies could not be read, so were not checked.
    """

    redaction: Redaction
    post_only: tuple[str, ...]
    post_indices: tuple[int, ...]
    values: int
    left: int
    unread: int

    @property
    def posts(self) -> int:
        """How many requests were credential posts."""
        return len(self.post_indices)


def _request_body_text(request: dict[str, Any], root: Path) -> tuple[str | None, str, bool]:
    """A HAR request's body as text, its ``Content-Type``, and whether a body file
    it names could not be read as UTF-8 text."""
    content_type = next(
        (
            str(header.get("value", ""))
            for header in request.get("headers") or ()
            if isinstance(header, dict) and str(header.get("name", "")).lower() == "content-type"
        ),
        "",
    )
    post_data = request.get("postData")
    if isinstance(post_data, dict) and isinstance(post_data.get("text"), str):
        return post_data["text"], str(post_data.get("mimeType") or content_type), False
    ref = request.get("_postDataFile")
    if not isinstance(ref, str):
        return None, content_type, False
    path = (root / ref).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        return None, content_type, False
    try:
        return path.read_bytes().decode("utf-8"), content_type, False
    except (OSError, UnicodeDecodeError):
        return None, content_type, True


def credential_post_redaction(entries: Iterable[dict[str, Any]], run_dir: Path) -> CredentialScrub:
    """What to scrub from a run recorded without known credentials (``gp observe
    interactive``, ``gp observe go``): the values of its own credential posts.

    A POST is a credential post by the digest's rule
    (:func:`graftpunk.har.digest.is_credential_body`); its values are the ones its
    DOM snapshot is redacted of (:func:`graftpunk.observe.snapshot.post_redaction_values`):
    a password-like field's as a secret, an identifier field's as an identifier.
    Its body is read from ``postData`` or from its file under ``bodies/``. A body
    file that is not UTF-8 text, or cannot be read, and a body the rule fails on,
    are counted in ``unread``, logged, and not checked; a body the rule reads no
    field names from (malformed JSON, a multipart body) is no credential post.
    The placeholder ``***REDACTED***`` (a post the user marked failed, whose
    password the recorder already replaced) is never a value. A value inside one
    of the credential posts' field names is kept out of the run-wide scrub
    (:class:`CredentialScrub`).
    """
    from graftpunk.har.digest import body_values, is_credential_body
    from graftpunk.observe.snapshot import post_redaction_values, spelled_redaction

    root = run_dir.resolve()
    secrets: dict[str, None] = {}
    identifiers: dict[str, None] = {}
    names: set[str] = set()
    indices: list[int] = []
    unread = 0
    for index, entry in enumerate(entries):
        request = entry.get("request") if isinstance(entry, dict) else None
        if not isinstance(request, dict) or str(request.get("method", "")).upper() != "POST":
            continue
        text, content_type, failed = _request_body_text(request, root)
        if failed:
            unread += 1
            LOG.warning("credential_scrub_body_unread", entry=index, file=request["_postDataFile"])
            continue
        if not text:
            continue
        try:
            if not is_credential_body(text, content_type):
                continue
            post_secrets, post_identifiers = post_redaction_values(text, content_type)
            post_names = {name for path, _value in body_values(text, content_type) for name in path}
        except Exception as exc:  # noqa: BLE001 - one post's body never stops the save
            unread += 1
            LOG.warning("credential_scrub_body_unread", entry=index, exc_type=type(exc).__name__)
            continue
        indices.append(index)
        names |= post_names
        secrets.update(dict.fromkeys(v for v in post_secrets if v != REDACTED))
        identifiers.update(dict.fromkeys(v for v in post_identifiers if v != REDACTED))

    def in_a_name(value: str) -> bool:
        return any(value in name for name in names)

    run_secrets = [v for v in _kept_secrets(secrets) if not in_a_name(v)]
    run_identifiers = [v for v in _kept_secrets(identifiers) if not in_a_name(v)]
    post_only = tuple(v for v in secrets if in_a_name(v))
    every = dict.fromkeys([*secrets, *identifiers])
    return CredentialScrub(
        redaction=spelled_redaction(run_secrets, run_identifiers),
        post_only=post_only,
        post_indices=tuple(indices),
        values=len(dict.fromkeys([*run_secrets, *run_identifiers])),
        left=len(every) - len(dict.fromkeys([*run_secrets, *run_identifiers, *post_only])),
        unread=unread,
    )


def scrub_credential_post_bodies(
    entries: list[dict[str, Any]], found: CredentialScrub, run_dir: Path
) -> None:
    """Replace ``found.post_only`` in each credential post's own body (its
    ``postData`` text, or its file under ``bodies/``) where a value is whole under
    a password-like name (:func:`graftpunk.observe.failure_text.scrub_failed_password`,
    the rule a post marked failed is scrubbed by). *entries* is mutated. A body
    file that cannot be read or rewritten is logged and left."""
    if not found.post_only:
        return
    from graftpunk.observe.failure_text import scrub_failed_password

    root = run_dir.resolve()
    for index in found.post_indices:
        request = entries[index].get("request") if index < len(entries) else None
        if not isinstance(request, dict):
            continue
        text, content_type, _failed = _request_body_text(request, root)
        if not text:
            continue
        scrubbed = scrub_failed_password(text, content_type, found.post_only)
        if scrubbed == text:
            continue
        post_data = request.get("postData")
        if isinstance(post_data, dict) and isinstance(post_data.get("text"), str):
            post_data["text"] = scrubbed
            continue
        ref = str(request["_postDataFile"])
        try:
            data = scrubbed.encode("utf-8")
            _replace_file((root / ref).resolve(), data)
            request["_postDataSize"] = len(data)
        except OSError as exc:
            LOG.warning("credential_post_body_not_scrubbed", file=ref, exc_type=type(exc).__name__)


def redact_run_text_files(run_dir: Path, redact: Redaction | Iterable[str]) -> int:
    """Scrub *redact* from the run's ``page-source.html`` and its DOM snapshots
    (``snapshots/*.html``), as :func:`redact_text` scrubs one HAR string. A file is
    rewritten only when something was found, atomically and keeping its mode; one
    that cannot be read or rewritten is logged and left. Returns how many files
    changed."""
    scrub = text_redactor(redact)
    paths = [run_dir / "page-source.html"]
    snapshots = run_dir / "snapshots"
    if snapshots.is_dir():
        paths.extend(sorted(snapshots.glob("*.html")))
    changed = 0
    for path in paths:
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
            scrubbed = scrub(text)
            if scrubbed != text:
                _replace_file(path, scrubbed.encode("utf-8"))
                changed += 1
        except (OSError, UnicodeDecodeError) as exc:
            LOG.warning(
                "run_text_file_not_scrubbed",
                file=path.relative_to(run_dir).as_posix(),
                exc_type=type(exc).__name__,
            )
    return changed


def write_capture_data(
    storage: ObserveStorage, capture: Any, redact: Redaction | Iterable[str] = ()
) -> int:
    """Write a stopped capture's HAR, and its console logs when it has any.

    The HAR and the request bodies in ``bodies/`` are scrubbed of *redact* first
    (:func:`redact_har_entries`, :func:`redact_request_body_files`). Each write
    is guarded on its own and logged at error level when it fails, since the
    user opted into this run: a failure here never reaches the caller.

    A capture with ``iter_har_entries`` (the HTTP capture, which spools its
    entries to disk) is read, scrubbed and written one entry at a time, and
    its spool removed once the HAR is written.

    Returns:
        The number of HAR entries written, 0 when the HAR could not be.
    """
    from graftpunk.observe.http_capture import HttpCaptureBackend

    redact = as_redaction(redact)
    written = 0

    def scrubbed(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        clean = redact_har_entries(entries, redact)
        # Scrub the body files first, since that can update an entry's
        # _postDataSize; a failure there must not cost the HAR.
        try:
            redact_request_body_files(clean, redact, storage.run_dir)
        except Exception as exc:  # noqa: BLE001 - each write is best-effort
            LOG.error(
                "observe_request_body_redaction_failed",
                error=str(exc),
                exc_type=type(exc).__name__,
            )
        return clean

    def one_at_a_time(entries: Iterable[dict[str, Any]]) -> Iterator[dict[str, Any]]:
        for entry in entries:
            yield from scrubbed([entry])

    try:
        if isinstance(capture, HttpCaptureBackend):
            try:
                written = storage.write_har(one_at_a_time(capture.iter_har_entries()))
            finally:
                # On every exit, success or not: the spool holds the traffic
                # before the scrub, and the capture is stopped, so nothing
                # recreates it (HttpCaptureBackend drops a late response).
                capture.discard_spool()
        else:
            entries = scrubbed(capture.get_har_entries())
            storage.write_har(entries)
            written = len(entries)
    except Exception as exc:  # noqa: BLE001 - each write is best-effort
        LOG.error("observe_har_write_failed", error=str(exc), exc_type=type(exc).__name__)
    try:
        console_logs = capture.get_console_logs()
        if console_logs:
            storage.write_console_logs(console_logs)
    except Exception as exc:  # noqa: BLE001 - each write is best-effort
        LOG.error("observe_console_log_write_failed", error=str(exc), exc_type=type(exc).__name__)
    return written


def _report_credential_scrub(found: CredentialScrub, say: Callable[[str], None]) -> None:
    """Log and print how many values the scrub of a run's credential posts found,
    never a value."""
    LOG.info(
        "observe_credential_scrub",
        credential_posts=found.posts,
        values_scrubbed=found.values,
        values_post_only=len(found.post_only),
        values_left=found.left,
        bodies_unread=found.unread,
    )
    if found.values:
        say(
            f"[green]Scrubbed {found.values} typed credential "
            f"value{'' if found.values == 1 else 's'} from the run[/green] "
            f"({found.posts} credential post{'' if found.posts == 1 else 's'})"
        )
    if found.post_only:
        say(
            f"[yellow]{len(found.post_only)} password "
            f"value{' is' if len(found.post_only) == 1 else 's are'} also part of a field "
            "name: scrubbed from the login's own body only[/yellow]"
        )
    if found.left:
        say(
            f"[yellow]{found.left} credential value{' is' if found.left == 1 else 's are'} "
            f"left in the run (under {_MIN_REDACT_LEN} characters, or a username that is "
            "part of a field name)[/yellow]"
        )
    if found.unread:
        say(
            f"[yellow]{found.unread} POST "
            f"bod{'y' if found.unread == 1 else 'ies'} could not be read, so "
            f"{'it was' if found.unread == 1 else 'they were'} not checked for "
            "credentials[/yellow]"
        )


async def save_observe_run(
    storage: ObserveStorage,
    backend: Any,
    screenshot_label: str,
    *,
    console: Any | None = None,
    redact: Redaction | Iterable[str] = (),
    scrub_credential_posts: bool = False,
) -> None:
    """Persist a nodriver observe run: screenshot, page source, HAR, console logs.

    ``backend.stop_capture_async()`` runs after the screenshot/page-source
    grab so the late body fetch happens while the browser is still alive.
    Before that grab, the backend's reads after a credential post (when it
    has them, ``close_after_reads``) are cancelled and waited for.
    Each write is guarded on its own so one failure (a full disk, an
    unserialisable body) cannot take the others down or mask the exception
    the caller may be unwinding. Progress lines go to ``console`` (a rich
    Console) when given; the log always carries them.

    Args:
        storage: Run directory owner.
        backend: An async-capable capture backend (nodriver).
        screenshot_label: Label baked into the screenshot filename.
        console: Optional rich console for user-facing progress.
        redact: Secret values (e.g. the submitted credentials) to scrub from
            the HAR before it is written, and from the request bodies written
            to ``bodies/`` (:func:`redact_request_body_files`). Response bodies
            on disk are not scanned. A generator is read once and kept.
        scrub_credential_posts: For a run recorded without known credentials (a
            login typed by hand): also scrub the values of the run's own
            credential posts (:func:`credential_post_redaction`) from the HAR,
            the request bodies in ``bodies/``, the page source and the DOM snapshots
            (:func:`redact_run_text_files`). How many values were scrubbed is
            logged and printed, never a value.
    """

    def say(msg: str) -> None:
        if console is not None:
            console.print(msg)

    def step(name: str, fn: Any) -> Any:
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 — each write is best-effort
            LOG.error(
                "observe_run_write_failed", step=name, error=str(exc), exc_type=type(exc).__name__
            )
            say(f"[red]{name} failed:[/red] {exc}")
            return None

    # The reads the recorder takes after a credential post: none may land after
    # stop, nor read the page while the page source is read.
    close_after_reads = getattr(backend, "close_after_reads", None)
    if close_after_reads is not None:
        try:
            await close_after_reads()
        except Exception as exc:  # noqa: BLE001 - the stop below closes them again
            LOG.error(
                "observe_close_after_reads_failed", error=str(exc), exc_type=type(exc).__name__
            )
    screenshot_data = await backend.take_screenshot()
    page_source = await backend.get_page_source()

    if screenshot_data is None and page_source is None:
        say("[yellow]Browser disconnected — screenshot and page source unavailable[/yellow]")
    else:
        if screenshot_data:
            path = step(
                "screenshot", lambda: storage.save_screenshot(1, screenshot_label, screenshot_data)
            )
            if path is not None:
                say(f"[green]Screenshot saved:[/green] {path}")
        else:
            say("[yellow]Screenshot capture failed[/yellow]")

        if page_source:
            source_path = storage.run_dir / "page-source.html"
            if (
                step("page source", lambda: write_private_file(source_path, page_source))
                is not None
            ):
                say(f"[green]Page source saved:[/green] {source_path}")
        else:
            say("[yellow]Page source capture failed[/yellow]")

    try:
        await backend.stop_capture_async()
    except Exception as exc:  # noqa: BLE001 — still write what was captured
        LOG.error("observe_stop_capture_failed", error=str(exc), exc_type=type(exc).__name__)

    redact = as_redaction(redact)
    raw_entries = backend.get_har_entries()
    found: CredentialScrub | None = None
    if scrub_credential_posts:
        found = step(
            "credential scrub",
            lambda: credential_post_redaction(raw_entries, storage.run_dir),
        )
        if found is not None:
            redact = Redaction(
                redact.secrets + found.redaction.secrets,
                redact.identifiers + found.redaction.identifiers,
            )
            step(
                "page source and snapshot redaction",
                lambda: redact_run_text_files(storage.run_dir, found.redaction),
            )
            _report_credential_scrub(found, say)
    har_entries = redact_har_entries(raw_entries, redact)
    step(
        "request body redaction",
        lambda: redact_request_body_files(har_entries, redact, storage.run_dir),
    )
    if scrub_credential_posts and found is not None:
        step(
            "credential post scrub",
            lambda: scrub_credential_post_bodies(har_entries, found, storage.run_dir),
        )
    if har_entries and step("HAR", lambda: storage.write_har(har_entries)) is not None:
        say(f"[green]HAR data saved:[/green] {len(har_entries)} entries")

    console_logs = backend.get_console_logs()
    if (
        console_logs
        and step("console logs", lambda: storage.write_console_logs(console_logs)) is not None
    ):
        say(f"[green]Console logs saved:[/green] {len(console_logs)} entries")

    LOG.info(
        "observe_run_saved",
        run_dir=str(storage.run_dir),
        har_entries=len(har_entries),
        console_logs=len(console_logs),
    )
    say(f"\n[bold]Observe run:[/bold] {storage.run_dir}")
