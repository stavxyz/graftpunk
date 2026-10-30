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

    *body_values* (a wrong password a user typed as a throwaway, which may be a
    word such as ``password``) are replaced only where one is a whole value of a
    request body, in ``postData`` and in a request body file under ``bodies/``: a
    form field's value, a JSON string that is no object key (one level into a
    string holding JSON too), a ``multipart/form-data`` part's whole text, a
    ``postData.params`` value, or the whole body. Never in a field name, inside
    another value, in a URL, a header, or a response.
    """

    secrets: tuple[str, ...] = ()
    identifiers: tuple[str, ...] = ()
    body_values: tuple[str, ...] = ()

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
        return bool(self.secrets or self.identifiers or self.body_values)


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

    scrubbed = scrub(entries)
    if scrubber.body_values:
        for entry in scrubbed:
            post_data = (
                (entry.get("request") or {}).get("postData") if isinstance(entry, dict) else None
            )
            if isinstance(post_data, dict):
                scrubber.scrub_post_data(post_data)
    return scrubbed


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
    body_values: frozenset[str] = frozenset()

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
            body_values=frozenset(value for value in redaction.body_values if value),
        )

    def __bool__(self) -> bool:
        return bool(self.secret_forms or self.identifier_re or self.body_values)

    def scrub_post_data(self, post_data: dict[str, Any]) -> None:
        """Replace :attr:`body_values` in a HAR ``postData`` (mutated): in its text
        (:meth:`scrub_body`) and as a whole ``params`` value."""
        text = post_data.get("text")
        if isinstance(text, str):
            post_data["text"] = self.scrub_body(text)
        params = post_data.get("params")
        if isinstance(params, list):
            for param in params:
                if isinstance(param, dict) and param.get("value") in self.body_values:
                    param["value"] = REDACTED

    def scrub_body(self, text: str) -> str:
        """*text*, a request body, with each of :attr:`body_values` replaced where it
        is a whole value (see :class:`Redaction`)."""
        if not self.body_values:
            return text
        if text in self.body_values:
            return REDACTED
        if text.lstrip()[:1] in ("{", "["):
            return _scrub_json_values(text, self.body_values, nested=True)
        text = _MULTIPART_PART_RE.sub(
            lambda m: m.group(1) + REDACTED if m.group(2) in self.body_values else m.group(0),
            text,
        )
        return _FORM_FIELD_RE.sub(
            lambda m: (
                m.group(1) + REDACTED
                if urllib.parse.unquote_plus(m.group(2)) in self.body_values
                else m.group(0)
            ),
            text,
        )

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


# A form field, in a URL-encoded body or a text/plain one (a field a line): its
# name and "=", then its value.
_FORM_FIELD_RE = re.compile(r"((?:^|(?<=[&\r\n]))[^=&\r\n]*=)([^&\r\n]*)")
# A multipart/form-data part's text: after its headers' blank line, up to the
# boundary line.
_MULTIPART_PART_RE = re.compile(r"(\r?\n\r?\n)(.*?)(?=\r?\n--)", re.DOTALL)
# A JSON string token.
_JSON_STRING_RE = re.compile(r'"(?:[^"\\]|\\.)*"')
# What follows a JSON string that is an object key.
_KEY_END_RE = re.compile(r"\s*:")


def _scrub_json_values(text: str, values: frozenset[str], *, nested: bool) -> str:
    """*text*, JSON, with each string that is no object key and equals one of
    *values* replaced; with *nested*, a string holding JSON is scrubbed the same way
    one level down and written back when it changed. Each string token is decoded
    on its own, so no depth of nesting is parsed at once."""

    def replace(match: re.Match[str]) -> str:
        if _KEY_END_RE.match(text, match.end()) is not None:
            return match.group(0)
        try:
            value = json.loads(match.group(0))
        except ValueError:
            return match.group(0)
        if value in values:
            return json.dumps(REDACTED)
        if nested and value.lstrip()[:1] in ("{", "["):
            inner = _scrub_json_values(value, values, nested=False)
            if inner != value:
                return json.dumps(inner)
        return match.group(0)

    return _JSON_STRING_RE.sub(replace, text)


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
            scrubbed = scrubber.scrub_body(scrubber.scrub_text(text))
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


async def save_observe_run(
    storage: ObserveStorage,
    backend: Any,
    screenshot_label: str,
    *,
    console: Any | None = None,
    redact: Redaction | Iterable[str] = (),
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
    har_entries = redact_har_entries(backend.get_har_entries(), redact)
    step(
        "request body redaction",
        lambda: redact_request_body_files(har_entries, redact, storage.run_dir),
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
