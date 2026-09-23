"""The fixture sidecar's one owner: the keys of each schema version, and the loader.

A ``<fixture>.meta.json`` sidecar sits beside every file ``gp observe fixtures``
writes and travels with the fixture into a plugin's committed fixtures tree.
Every field in it is meant to be committed: the capture's URL and time are not
in it (the HAR and the digest hold them), ``capture_sha256`` is the hash of the
captured body, and ``flagged_names`` is every cookie name the recording set and
the token names the digest recorded, never a value. A name that holds an account
value (:func:`graftpunk.har.paths.holds_an_id`) is written in no form, not even
hashed (a hash of a short id is reversed by brute force): ``redacted_names``
counts how many were left out. ``body_params`` is the request's body keys that read as
field names and hold no id by that rule; the rule is lexical, so an account value
in a shape it does not read as an id is kept, which is why the list is read before
it is committed.

The key set of a version is closed: the loader refuses a sidecar with a key
missing or a key outside it, so any change to the keys (added, renamed, or
removed) is a new schema version. ``redacted_names`` joined schema 1 without one
only because schema 1 had not been released when it did.

This module is the only place a key set is spelled. It takes the current
version number from :mod:`graftpunk.contracts`, and a later version adds its
own key set to :data:`SIDECAR_FIELDS` so sidecars committed under an earlier
one still load. Pytest-free, because ``FixtureSession`` reads through it; the
writer, which only ``gp observe fixtures`` runs, lives in
``graftpunk.devtools.captures`` and builds its text here (graft skill spec,
2026-09-21).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from graftpunk.contracts import UnknownSchemaError, current_schema, refuse_unknown_schema
from graftpunk.exceptions import GraftpunkError

__all__ = [
    "SIDECAR_FIELDS",
    "SIDECAR_SUFFIX",
    "Sidecar",
    "SidecarError",
    "is_sidecar",
    "load_sidecar",
    "sidecar_path",
    "sidecar_payload",
    "sidecar_text",
]

SIDECAR_SUFFIX = ".meta.json"

SIDECAR_FIELDS: dict[int, frozenset[str]] = {
    1: frozenset(
        {
            "status",
            "content_type",
            "body_params",
            "capture_sha256",
            "flagged_names",
            "redacted_names",
        }
    ),
}
"""The keys each schema version holds besides ``schema`` itself."""

_CAPTURE_SHA256_RE = re.compile(r"[0-9a-f]{64}")


class SidecarError(GraftpunkError):
    """A sidecar that cannot be read, or that is outside its declared format.

    A ``GraftpunkError``, not a ``ValueError``: plugin command code routinely
    catches ``ValueError`` around a fixture-backed ``.json()`` call in a test,
    and a malformed sidecar must not be swallowed by that catch."""


@dataclass(frozen=True)
class Sidecar:
    """One sidecar's facts, independent of the version it was read from."""

    status: int
    content_type: str
    body_params: tuple[str, ...] = ()
    # None: the fixture came off no capture, which the author declares by
    # writing null; the in-suite sanitisation check counts it as declared.
    capture_sha256: str | None = None
    flagged_names: tuple[str, ...] = ()
    # How many names were left out of flagged_names because they hold an account
    # value; the names themselves are written nowhere.
    redacted_names: int = 0

    def __post_init__(self) -> None:
        """Hold every field to the loader's own type and sign checks, sort and dedupe
        the name tuples, and refuse a ``capture_sha256`` that is not a lowercase
        sha256 hex digest or ``None``, here rather than in the writer: a ``Sidecar``
        built anywhere is fit to serialize."""
        for key in ("status", "redacted_names"):
            value = getattr(self, key)
            if isinstance(value, bool) or not isinstance(value, int):
                raise SidecarError(f"{key} must be an integer, got {value!r}")
        if self.redacted_names < 0:
            raise SidecarError(f"redacted_names must not be negative, got {self.redacted_names}")
        if not isinstance(self.content_type, str):
            raise SidecarError(f"content_type must be a string, got {self.content_type!r}")
        for key in ("body_params", "flagged_names"):
            value = getattr(self, key)
            if not all(isinstance(item, str) for item in value):
                raise SidecarError(f"{key} must hold only strings, got {value!r}")
        if self.capture_sha256 is not None and not _CAPTURE_SHA256_RE.fullmatch(
            self.capture_sha256
        ):
            raise SidecarError(
                "capture_sha256 must be a 64-character lowercase hex sha256 digest, "
                f"or null, got {self.capture_sha256!r}"
            )
        object.__setattr__(self, "body_params", tuple(sorted(set(self.body_params))))
        object.__setattr__(self, "flagged_names", tuple(sorted(set(self.flagged_names))))

    @property
    def declared(self) -> bool:
        """True for a hand-made fixture's sidecar: trusted, not checked against a capture."""
        return self.capture_sha256 is None


def sidecar_path(fixture: Path) -> Path:
    """Where *fixture*'s sidecar lives: beside it, named ``<fixture name>.meta.json``."""
    return fixture.with_name(fixture.name + SIDECAR_SUFFIX)


def is_sidecar(path: Path) -> bool:
    """True when *path* is a sidecar rather than a fixture."""
    return path.name.endswith(SIDECAR_SUFFIX)


def sidecar_payload(sidecar: Sidecar) -> dict[str, object]:
    """*sidecar* as the current schema's JSON object."""
    return {
        "schema": current_schema("sidecar"),
        "status": sidecar.status,
        "content_type": sidecar.content_type,
        "body_params": list(sidecar.body_params),
        "capture_sha256": sidecar.capture_sha256,
        "flagged_names": list(sidecar.flagged_names),
        "redacted_names": sidecar.redacted_names,
    }


def sidecar_text(sidecar: Sidecar) -> str:
    """*sidecar* as the text a sidecar file holds: indented JSON, sorted keys."""
    return json.dumps(sidecar_payload(sidecar), indent=2, sort_keys=True) + "\n"


def load_sidecar(path: Path) -> Sidecar:
    """Read the sidecar at *path*, holding it to its version's key set.

    Raises:
        SidecarError: The file is unreadable, not UTF-8, or not a JSON object; its ``schema``
            is missing or unknown; it has a key outside its version's set or
            lacks one; a value has the wrong type; or ``capture_sha256`` is set
            and is not a 64-character lowercase hex digest. The message names
            *path*.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError) as exc:
        # ValueError covers a decode error, a JSON syntax error, and an integer
        # past the interpreter's digit limit; RecursionError, nesting too deep.
        raise SidecarError(f"{path}: not a readable JSON sidecar ({type(exc).__name__})") from exc
    if not isinstance(data, dict):
        raise SidecarError(f"{path}: a sidecar is a JSON object")
    try:
        version = refuse_unknown_schema("sidecar", data.get("schema"))
    except UnknownSchemaError as exc:
        raise SidecarError(f"{path}: {exc}") from exc
    fields = SIDECAR_FIELDS.get(version)
    if fields is None:
        # A schema number contracts accepts that has no key set here: a bump
        # that missed its SIDECAR_FIELDS entry. Refused by name, not KeyError.
        raise SidecarError(f"{path}: this graftpunk has no key set for sidecar schema {version}")
    expected = fields | {"schema"}
    extra = sorted(set(data) - expected)
    missing = sorted(expected - set(data))
    if extra:
        raise SidecarError(f"{path}: keys outside sidecar schema {version}: {', '.join(extra)}")
    if missing:
        raise SidecarError(f"{path}: missing keys: {', '.join(missing)}")
    status = _integer(data, "status", path)
    content_type = _text(data, "content_type", path)
    body_params = _names(data, "body_params", path)
    capture_sha256 = _optional_text(data, "capture_sha256", path)
    flagged_names = _names(data, "flagged_names", path)
    redacted_names = _integer(data, "redacted_names", path)
    try:
        # Sidecar's construction-time checks (a negative count, the
        # capture_sha256 format) do not know *path*, so it is added here.
        return Sidecar(
            status=status,
            content_type=content_type,
            body_params=body_params,
            capture_sha256=capture_sha256,
            flagged_names=flagged_names,
            redacted_names=redacted_names,
        )
    except SidecarError as exc:
        raise SidecarError(f"{path}: {exc}") from exc


def _integer(data: dict[str, object], key: str, path: Path) -> int:
    value = data[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise SidecarError(f"{path}: {key} must be an integer, got {value!r}")
    return value


def _text(data: dict[str, object], key: str, path: Path) -> str:
    value = data[key]
    if not isinstance(value, str):
        raise SidecarError(f"{path}: {key} must be a string, got {value!r}")
    return value


def _optional_text(data: dict[str, object], key: str, path: Path) -> str | None:
    value = data[key]
    if value is not None and not isinstance(value, str):
        raise SidecarError(f"{path}: {key} must be a string or null, got {value!r}")
    return value


def _names(data: dict[str, object], key: str, path: Path) -> tuple[str, ...]:
    value = data[key]
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise SidecarError(f"{path}: {key} must be a list of strings, got {value!r}")
    return tuple(value)
