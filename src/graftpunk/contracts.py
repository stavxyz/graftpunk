"""The schema number of every versioned machine payload, and the one check a reader applies.

Payloads a program reads carry a ``schema`` number: the ``gp observe digest
--endpoints-json`` projection (``endpoints``), the ``gp plugin info --json``
payload (``info``), and the fixture sidecar (``sidecar``). Within one schema
version, fields are added and never renamed or removed; a rename or a removal
is a new version, and a reader accepts every version from 1 to the current
one. The sidecar is stricter: its loader refuses a
missing key and a key outside its version, so any change to its keys, an added
one included, is a new version (:mod:`graftpunk.testing.sidecar`).

``gp version --json`` is the bootstrap of every other contract, so it carries no
number of its own: its two fields, ``graftpunk`` and ``contracts``, are
permanent, and fields are added to it and never renamed or removed.
:func:`installation_facts` builds it, and the command only prints it.

This module is the only place in the package the current schema number of a
surface is declared, and the only place one is compared:
:func:`refuse_unknown_schema` for a payload a reader loads, and
:func:`contract_mismatch` for the ``gp version --contract`` handshake (graft
skill spec, 2026-09-21).

Adding a surface is these edits, all here: its name in ``Surface``; its
``<NAME>_SCHEMA`` constant; its entry in ``_CURRENT``; and, when a caller reads
it through a ``gp`` command, its name in ``CLI_SURFACES``. The payload's writer
then reads its number through :func:`current_schema` and never writes a
literal. A test holds ``_CURRENT`` to ``Surface`` and ``CLI_SURFACES`` to
``_CURRENT``, so a missed edit fails the suite.

One exception, by design: a payload that holds nothing its current version added
is written as the earlier version, so a reader of that version keeps reading it.
The ``--endpoints-json`` projection of a run with no GraphQL operation row is
schema 1 (:data:`ENDPOINTS_SCHEMA_WITHOUT_OPERATIONS`), and a sidecar naming no
GraphQL operation is schema 4 (:data:`SIDECAR_SCHEMA_WITHOUT_OPERATION`), #367, or
schema 5 when it names one and no endpoint family
(:data:`SIDECAR_SCHEMA_WITHOUT_FAMILY`), #213.
The version handshake reports such a surface's earlier number, the one every run
writes (:func:`handshake_schema`), so a caller written for it keeps working, and
:func:`written_schemas` lists every number a payload of it may carry.
"""

from __future__ import annotations

from typing import Final, Literal

import graftpunk

__all__ = [
    "CLI_SURFACES",
    "ENDPOINTS_SCHEMA",
    "ENDPOINTS_SCHEMA_WITHOUT_OPERATIONS",
    "INFO_SCHEMA",
    "SIDECAR_SCHEMA",
    "SIDECAR_SCHEMA_WITHOUT_FAMILY",
    "SIDECAR_SCHEMA_WITHOUT_OPERATION",
    "Surface",
    "UnknownSchemaError",
    "cli_contracts",
    "contract_mismatch",
    "current_schema",
    "handshake_schema",
    "installation_facts",
    "refuse_unknown_schema",
    "written_schemas",
]

Surface = Literal["endpoints", "info", "sidecar"]

ENDPOINTS_SCHEMA: Final = 2
INFO_SCHEMA: Final = 1
SIDECAR_SCHEMA: Final = 6

ENDPOINTS_SCHEMA_WITHOUT_OPERATIONS: Final = 1
"""The schema an ``--endpoints-json`` projection with no GraphQL operation row is
written as: schema 2 adds only ``operation``, so a reader written for schema 1
keeps reading such a run (#367)."""
SIDECAR_SCHEMA_WITHOUT_OPERATION: Final = 4
"""The schema a sidecar naming no GraphQL operation and no endpoint family is
written as: schema 5 adds only ``graphql_operation``, so a fixture of any other
request stays readable by a graftpunk from 1.35.0 to 1.38.x (#367), and schema 6
adds only ``endpoint_family`` (#213)."""
SIDECAR_SCHEMA_WITHOUT_FAMILY: Final = 5
"""The schema a sidecar naming a GraphQL operation and no endpoint family is
written as: schema 6 adds only ``endpoint_family``, so a GraphQL fixture stays
readable by a graftpunk from 1.39.0 to 1.42.7 (#213)."""

_CURRENT: Final[dict[Surface, int]] = {
    "endpoints": ENDPOINTS_SCHEMA,
    "info": INFO_SCHEMA,
    "sidecar": SIDECAR_SCHEMA,
}

_HANDSHAKE: Final[dict[Surface, int]] = {
    "endpoints": ENDPOINTS_SCHEMA_WITHOUT_OPERATIONS,
}
"""The number the ``gp version --contract`` handshake reports for a surface whose
current schema is written only on some runs: the one every run writes. A GraphQL
run's projection is schema 2, any other run's schema 1, so a caller written for
schema 1 (the graft skill before 0.1.27) passes the handshake and reads every
run that holds no operation row as it always did (#367). A surface not listed
reports its current number."""

CLI_SURFACES: Final[tuple[Surface, ...]] = ("endpoints", "info")
"""The surfaces a caller reads through the CLI, which ``gp version --json`` lists.

The sidecar is not one: it is read by ``graftpunk.testing`` from a plugin's
committed fixtures, never through a ``gp`` command."""


class UnknownSchemaError(ValueError):
    """A payload's ``schema`` is missing or is not a version this graftpunk reads."""


def _current(surface: Surface) -> int:
    """*surface*'s current number. The one lookup of ``_CURRENT``: a name outside
    ``Surface`` gets past the type only at runtime, and is refused by name here
    rather than as a bare ``KeyError``.

    Raises:
        UnknownSchemaError: *surface* is not a surface this graftpunk versions.
    """
    try:
        return _CURRENT[surface]
    except KeyError:
        known = ", ".join(sorted(_CURRENT))
        raise UnknownSchemaError(
            f"{surface!r} is not a surface this graftpunk versions (it versions {known})."
        ) from None


def current_schema(surface: Surface) -> int:
    """The schema number *surface* is written at by this graftpunk.

    Raises:
        UnknownSchemaError: *surface* is not a surface this graftpunk versions.
    """
    return _current(surface)


def handshake_schema(surface: Surface) -> int:
    """The number the version handshake reports for *surface* (:data:`_HANDSHAKE`):
    the schema every payload of it is written at or above.

    Raises:
        UnknownSchemaError: *surface* is not a surface this graftpunk versions.
    """
    current = _current(surface)
    return _HANDSHAKE.get(surface, current)


def written_schemas(surface: Surface) -> tuple[int, ...]:
    """Every schema this graftpunk writes *surface* at, from the handshake's number
    to the current one: ``(1, 2)`` for ``endpoints``. A reader that accepts more
    than the handshake's number checks each payload's own ``schema``.

    Raises:
        UnknownSchemaError: *surface* is not a surface this graftpunk versions.
    """
    return tuple(range(handshake_schema(surface), _current(surface) + 1))


def cli_contracts() -> dict[str, int]:
    """Each CLI-read surface and its handshake number (:func:`handshake_schema`), as
    ``gp version --json`` prints them."""
    return {surface: handshake_schema(surface) for surface in CLI_SURFACES}


def refuse_unknown_schema(surface: Surface, value: object) -> int:
    """*value* as a schema number this graftpunk reads for *surface*, or raise.

    Accepts an integer from 1 to the current number. ``True`` is refused although
    it is an ``int`` subclass, and so is ``1.0``: a schema number is written as an
    integer and nothing else.

    Raises:
        UnknownSchemaError: *surface* is not a surface this graftpunk versions, or
            *value* is missing (``None``) or unknown; the message names *surface*.
    """
    current = _current(surface)
    if value is None:
        raise UnknownSchemaError(
            f"{surface}: no schema number. This payload predates its versioned format "
            f"or was written by hand; regenerate it with this graftpunk."
        )
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= current:
        raise UnknownSchemaError(
            f"{surface}: schema {value!r} is not one this graftpunk reads "
            f"(it reads 1 to {current}). Upgrade graftpunk, or regenerate the payload."
        )
    return value


def installation_facts() -> dict[str, object]:
    """What ``gp version --json`` prints: facts about this installation as data.

    The installed version, and the schema number of every payload a caller reads
    through the CLI. The bootstrap of every other contract, so it carries no
    number of its own; its two fields are permanent (see the module docstring).
    The version is read at call time, so nothing here freezes it at import.
    """
    return {"graftpunk": graftpunk.__version__, "contracts": cli_contracts()}


def contract_mismatch(surface: str, reads: int) -> str | None:
    """Why a caller that reads *surface* at schema *reads* cannot use this graftpunk, or None.

    The ``gp version --contract`` handshake. Equality with the handshake number
    (:func:`handshake_schema`), not a range: a caller is written for one schema of
    a surface and reads that one, and checks a payload's own ``schema`` for any
    later one it also reads (:func:`written_schemas`). The message names the
    older side, so the caller can say which of the two to update.
    """
    current = cli_contracts().get(surface)
    if current is None and surface in _CURRENT:
        # Versioned, but served by no command: neither side is older, and the
        # name is spelled right.
        return (
            f"{surface}: graftpunk versions it, but it is not read through the gp CLI, "
            f"so there is no contract to check."
        )
    if current is None:
        return (
            f"{surface}: this graftpunk serves no surface by that name to a caller; "
            f"graftpunk is older than the caller, or the caller misspelled the surface."
        )
    if reads == current:
        return None
    if reads < current:
        return (
            f"{surface}: this graftpunk writes schema {current} and the caller reads "
            f"{reads}; the caller is older than graftpunk."
        )
    return (
        f"{surface}: the caller reads schema {reads} and this graftpunk writes "
        f"{current}; graftpunk is older than the caller."
    )
