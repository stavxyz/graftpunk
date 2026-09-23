"""One owner for every versioned payload's schema number (graft skill spec, 2026-09-21,
"Versioned payloads have one owner")."""

from __future__ import annotations

import re
from pathlib import Path
from typing import get_args

import pytest

import graftpunk
from graftpunk import contracts
from graftpunk.contracts import (
    _CURRENT,
    CLI_SURFACES,
    ENDPOINTS_SCHEMA,
    SIDECAR_SCHEMA,
    Surface,
    UnknownSchemaError,
    cli_contracts,
    contract_mismatch,
    current_schema,
    refuse_unknown_schema,
)

_PACKAGE = Path(graftpunk.__file__).parent
# A schema number written as a literal anywhere but contracts.py: a "schema" key
# with a digit, a schema= keyword with a digit, or a *_SCHEMA constant with a digit.
_LITERAL_SCHEMA_RE = re.compile(
    r"""["']schema["']\s*:\s*\d|\bschema\s*=\s*\d|_SCHEMA\s*(?::[^=\n]*)?=\s*\d"""
)


class TestCurrentNumbers:
    def test_one_number_per_surface(self) -> None:
        assert current_schema("endpoints") == ENDPOINTS_SCHEMA == 1
        assert current_schema("sidecar") == SIDECAR_SCHEMA == 1

    def test_cli_contracts_lists_every_surface_a_caller_reads_through_the_cli(self) -> None:
        assert cli_contracts() == {surface: current_schema(surface) for surface in CLI_SURFACES}
        assert "sidecar" not in cli_contracts()

    def test_every_surface_has_a_number_and_the_cli_ones_are_among_them(self) -> None:
        """A surface added to Surface without its number, or listed for the CLI
        without being declared, fails here rather than at a KeyError in a reader."""
        assert set(_CURRENT) == set(get_args(Surface))
        assert set(CLI_SURFACES) <= set(_CURRENT)


class TestRefuseUnknownSchema:
    def test_the_current_number_is_accepted(self) -> None:
        assert refuse_unknown_schema("sidecar", 1) == 1

    def test_a_missing_number_is_refused_with_the_surface_named(self) -> None:
        with pytest.raises(UnknownSchemaError, match="sidecar"):
            refuse_unknown_schema("sidecar", None)

    @pytest.mark.parametrize("value", [0, 2, "1", True, 1.0])
    def test_an_unknown_number_is_refused_with_the_surface_named(self, value: object) -> None:
        with pytest.raises(UnknownSchemaError, match="endpoints"):
            refuse_unknown_schema("endpoints", value)

    def test_the_error_is_a_value_error(self) -> None:
        assert issubclass(UnknownSchemaError, ValueError)


class TestAnUnknownSurface:
    """A surface name outside Surface reaches these at runtime only through a caller
    that ignored the type; each refuses it by name instead of raising KeyError."""

    def test_refuse_unknown_schema(self) -> None:
        with pytest.raises(UnknownSchemaError, match="'info' is not a surface"):
            refuse_unknown_schema("info", 1)

    def test_current_schema(self) -> None:
        with pytest.raises(UnknownSchemaError, match="'info' is not a surface"):
            current_schema("info")

    def test_cli_contracts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(contracts, "CLI_SURFACES", ("endpoints", "info"))
        with pytest.raises(UnknownSchemaError, match="'info' is not a surface"):
            cli_contracts()


class TestContractMismatch:
    """The gp version --contract handshake: equality, with the older side named."""

    def test_the_current_number_matches(self) -> None:
        assert contract_mismatch("endpoints", current_schema("endpoints")) is None

    def test_an_older_caller_is_named(self) -> None:
        message = contract_mismatch("endpoints", current_schema("endpoints") - 1)
        assert message is not None and "the caller is older than graftpunk" in message

    def test_a_newer_caller_is_named(self) -> None:
        message = contract_mismatch("endpoints", current_schema("endpoints") + 1)
        assert message is not None and "graftpunk is older than the caller" in message

    def test_a_surface_no_caller_reads_through_the_cli_is_a_mismatch(self) -> None:
        """The sidecar is versioned but served by no command: neither side is older,
        and the name is spelled right."""
        message = contract_mismatch("sidecar", 1)
        assert message == (
            "sidecar: graftpunk versions it, but it is not read through the gp CLI, "
            "so there is no contract to check."
        )

    def test_an_unknown_surface_names_the_older_side_or_a_misspelling(self) -> None:
        message = contract_mismatch("endpoint", 1)
        assert message is not None
        assert "graftpunk is older than the caller, or the caller misspelled" in message


def test_contracts_is_the_only_module_that_declares_a_schema_number() -> None:
    offenders = [
        f"{path.relative_to(_PACKAGE)}:{number}: {line.strip()}"
        for path in sorted(_PACKAGE.rglob("*.py"))
        if path.name != "contracts.py" or path.parent != _PACKAGE
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if _LITERAL_SCHEMA_RE.search(line)
    ]
    assert offenders == []
