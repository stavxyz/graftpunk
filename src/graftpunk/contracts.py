"""The schema number of every versioned machine payload, and the one check a reader applies.

Payloads a program reads carry a ``schema`` number: the ``gp observe digest
--endpoints-json`` projection (``endpoints``) and the fixture sidecar
(``sidecar``). Within one schema version, fields are added and never renamed or
removed; a rename or a removal is a new version, and a reader accepts every
version from 1 to the current one.

``gp version --json`` is the bootstrap of every other contract, so it carries no
number of its own: its two fields, ``graftpunk`` and ``contracts``, are
permanent, and fields are added to it and never renamed or removed.

This module is the only place in the package the current schema number of a
surface is declared, and the only place one is compared:
:func:`refuse_unknown_schema` for a payload a reader loads (graft skill spec,
2026-09-21).

Adding a surface is these edits, all here: its name in ``Surface``; its
``<NAME>_SCHEMA`` constant; its entry in ``_CURRENT``; and, when a caller reads
it through a ``gp`` command, its name in ``CLI_SURFACES``. The payload's writer
then reads its number through :func:`current_schema` and never writes a
literal. A test holds ``_CURRENT`` to ``Surface`` and ``CLI_SURFACES`` to
``_CURRENT``, so a missed edit fails the suite.
"""

from __future__ import annotations

from typing import Final, Literal

__all__ = [
    "CLI_SURFACES",
    "ENDPOINTS_SCHEMA",
    "SIDECAR_SCHEMA",
    "Surface",
    "UnknownSchemaError",
    "cli_contracts",
    "current_schema",
    "refuse_unknown_schema",
]

Surface = Literal["endpoints", "sidecar"]

ENDPOINTS_SCHEMA: Final = 1
SIDECAR_SCHEMA: Final = 1

_CURRENT: Final[dict[Surface, int]] = {
    "endpoints": ENDPOINTS_SCHEMA,
    "sidecar": SIDECAR_SCHEMA,
}

CLI_SURFACES: Final[tuple[Surface, ...]] = ("endpoints",)
"""The surfaces a caller reads through the CLI, which ``gp version --json`` lists.

The sidecar is not one: it is read by ``graftpunk.testing`` from a plugin's
committed fixtures, never through a ``gp`` command."""


class UnknownSchemaError(ValueError):
    """A payload's ``schema`` is missing or is not a version this graftpunk reads."""


def current_schema(surface: Surface) -> int:
    """The schema number *surface* is written at by this graftpunk."""
    return _CURRENT[surface]


def cli_contracts() -> dict[str, int]:
    """Each CLI-read surface and its current number, as ``gp version --json`` prints them."""
    return {surface: _CURRENT[surface] for surface in CLI_SURFACES}


def refuse_unknown_schema(surface: Surface, value: object) -> int:
    """*value* as a schema number this graftpunk reads for *surface*, or raise.

    Accepts an integer from 1 to the current number. ``True`` is refused although
    it is an ``int`` subclass, and so is ``1.0``: a schema number is written as an
    integer and nothing else.

    Raises:
        UnknownSchemaError: *value* is missing (``None``) or unknown; the message
            names *surface*.
    """
    current = _CURRENT[surface]
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
