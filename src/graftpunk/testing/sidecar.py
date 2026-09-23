"""The fixture sidecar's one owner: the keys of each schema version, and the loader.

A ``<fixture>.meta.json`` sidecar sits beside every file ``gp observe fixtures``
writes and travels with the fixture into a plugin's committed fixtures tree.
Every field in it may be committed: the capture's URL and time are not in it
(the HAR and the digest hold them), ``capture_sha256`` is the hash of the
captured body, and ``flagged_names`` is the cookie and token names the digest
recorded, never a value.

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
    1: frozenset({"status", "content_type", "body_params", "capture_sha256", "flagged_names"}),
}
"""The keys each schema version holds besides ``schema`` itself."""

_CAPTURE_SHA256_RE = re.compile(r"[0-9a-f]{64}")


class SidecarError(GraftpunkError):
    """A sidecar that cannot be read, or that is outside its declared format.

    A ``GraftpunkError``, not a ``ValueError``: plugin command code routinely
    catches ``ValueError`` around a fixture-backed ``.json()`` call in a test,
    and a malformed sidecar must not be swallowed by that catch (review round
    1, 2026-09-23)."""


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

    def __post_init__(self) -> None:
        """Sort and dedupe the name tuples, and refuse a ``capture_sha256`` that is
        not a lowercase sha256 hex digest or ``None``, here rather than in the
        writer: a ``Sidecar`` built anywhere is fit to serialize."""
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
    }


def sidecar_text(sidecar: Sidecar) -> str:
    """*sidecar* as the text a sidecar file holds: indented JSON, sorted keys."""
    return json.dumps(sidecar_payload(sidecar), indent=2, sort_keys=True) + "\n"


def load_sidecar(path: Path) -> Sidecar:
    """Read the sidecar at *path*, holding it to its version's key set.

    Raises:
        SidecarError: The file is unreadable or not a JSON object; its ``schema``
            is missing or unknown; it has a key outside its version's set or
            lacks one; a value has the wrong type; or ``capture_sha256`` is set
            and is not a 64-character lowercase hex digest. The message names
            *path*.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SidecarError(f"{path}: not a readable JSON sidecar ({exc})") from exc
    if not isinstance(data, dict):
        raise SidecarError(f"{path}: a sidecar is a JSON object")
    try:
        version = refuse_unknown_schema("sidecar", data.get("schema"))
    except UnknownSchemaError as exc:
        raise SidecarError(f"{path}: {exc}") from exc
    expected = SIDECAR_FIELDS[version] | {"schema"}
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
    try:
        # Every type check above already names *path*; only Sidecar's own
        # construction-time check (the capture_sha256 format) can still raise
        # here, and it does not know *path* on its own.
        return Sidecar(
            status=status,
            content_type=content_type,
            body_params=body_params,
            capture_sha256=capture_sha256,
            flagged_names=flagged_names,
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
