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
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from graftpunk.logging import get_logger
from graftpunk.observe.storage import ObserveStorage, write_private_file

LOG = get_logger(__name__)

REDACTED = "***REDACTED***"
# Secrets shorter than this are not redacted: replacing every "ab" in a HAR
# would mangle it beyond use, and such values are not meaningfully secret.
_MIN_REDACT_LEN = 3
# An HTTP Basic credential: the scheme, then base64 of "user:password". A JSON
# body may escape each "/" of the token as "\/".
_BASIC_RE = re.compile(r"(\bBasic\s+)((?:[A-Za-z0-9+]|/|\\/)+={0,2})", re.IGNORECASE)

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
        rest as secrets, by :func:`graftpunk.session_identity.is_identifier_field`:
        secret words win, so ``login_password`` or ``email_code`` is a secret."""
        from graftpunk.session_identity import is_identifier_field

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
        json.dumps(secret)[1:-1],
        base64.b64encode(secret.encode()).decode(),
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


def _kept_secrets(secrets: Iterable[str]) -> tuple[str, ...]:
    """The secrets long enough to redact."""
    return tuple(s for s in secrets if s and len(s) >= _MIN_REDACT_LEN)


def _redaction_forms(secrets: Iterable[str]) -> list[str]:
    """Every wire form of each secret long enough to redact, longest first."""
    forms: set[str] = set()
    for secret in _kept_secrets(secrets):
        forms |= _secret_forms(secret)
    return sorted(forms, key=len, reverse=True)


# A whole value: not preceded or followed by a letter or digit.
_WHOLE = r"(?<![A-Za-z0-9]){}(?![A-Za-z0-9])"


@dataclass(frozen=True)
class _Scrubber:
    """A :class:`Redaction` compiled for scrubbing text and bytes."""

    secret_forms: tuple[str, ...]
    identifier_re: re.Pattern[str] | None
    identifier_bytes_re: re.Pattern[bytes] | None
    basic_values: tuple[str, ...]

    @classmethod
    def of(cls, redact: Redaction | Iterable[str]) -> _Scrubber:
        redaction = as_redaction(redact)
        forms = _redaction_forms(redaction.identifiers)
        alternation = "|".join(re.escape(f) for f in forms)
        return cls(
            secret_forms=tuple(_redaction_forms(redaction.secrets)),
            identifier_re=re.compile(_WHOLE.format(f"(?:{alternation})")) if forms else None,
            identifier_bytes_re=(
                re.compile(_WHOLE.format(f"(?:{alternation})").encode("utf-8")) if forms else None
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
            value = self.identifier_re.sub(REDACTED, value)
        return value

    def scrub_bytes(self, raw: bytes) -> bytes:
        raw = _scrub_bytes(raw, list(self.secret_forms))
        if self.identifier_bytes_re is not None:
            raw = self.identifier_bytes_re.sub(lambda m: b"*" * len(m.group(0)), raw)
        return raw


def _scrub_basic(value: str, secrets: tuple[str, ...]) -> str:
    """*value* with each Basic credential whose decoded text holds a secret replaced."""

    def replace(match: re.Match[str]) -> str:
        try:
            decoded = base64.b64decode(match.group(2).replace("\\/", "/"), validate=True)
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
