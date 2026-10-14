"""gp version --json and --at-least (graft skill spec, 2026-09-21)."""

from __future__ import annotations

import json
from importlib.metadata import PackageNotFoundError

import pytest
from typer.testing import CliRunner

import graftpunk
from graftpunk import _distribution, contracts
from graftpunk.cli.main import app
from graftpunk.contracts import cli_contracts, installation_facts

runner = CliRunner()

# gp version --json is the bootstrap surface: these fields are permanent.
_VERSION_JSON_FIELDS = {"graftpunk", "contracts"}


def test_json_prints_the_installation_facts_with_exactly_the_pinned_fields() -> None:
    result = runner.invoke(app, ["version", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert set(payload) == _VERSION_JSON_FIELDS
    assert payload == {"graftpunk": graftpunk.__version__, "contracts": cli_contracts()}
    assert payload == installation_facts()


def test_json_is_one_line_with_sorted_keys() -> None:
    result = runner.invoke(app, ["version", "--json"])
    assert result.stdout.strip() == json.dumps(installation_facts(), sort_keys=True)


@pytest.mark.parametrize(
    ("installed", "floor", "exit_code"),
    [
        ("1.17.0", "1.17.0", 0),
        ("1.17.1", "1.17.0", 0),
        ("1.16.9", "1.17.0", 1),
        ("1.10.0", "1.9.0", 0),
        ("1.9.0", "1.10.0", 1),
        ("1.17.0rc1", "1.17.0", 1),
        ("1.17.0", "1.17.0rc1", 0),
    ],
)
def test_at_least_orders_by_version_rules(
    monkeypatch: pytest.MonkeyPatch, installed: str, floor: str, exit_code: int
) -> None:
    monkeypatch.setattr(graftpunk, "__version__", installed)
    result = runner.invoke(app, ["version", "--json", "--at-least", floor])
    assert result.exit_code == exit_code, result.output
    assert json.loads(result.stdout)["graftpunk"] == installed


def test_at_least_without_json_prints_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(graftpunk, "__version__", "1.17.0")
    result = runner.invoke(app, ["version", "--at-least", "1.17.0"])
    assert result.exit_code == 0
    assert result.stdout == ""


def test_an_unreadable_floor_exits_1_with_a_message() -> None:
    """Exit 2 is kept for an option gp does not know, so a caller can tell the two apart."""
    result = runner.invoke(app, ["version", "--at-least", "not-a-version"])
    assert result.exit_code == 1
    assert "'not-a-version' is not a version" in result.output


def _contract_args(contracts: dict[str, int]) -> list[str]:
    return [
        arg
        for surface, number in contracts.items()
        for arg in ("--contract", f"{surface}={number}")
    ]


def test_matching_contracts_exit_0_and_still_print_the_facts() -> None:
    result = runner.invoke(app, ["version", "--json", *_contract_args(cli_contracts())])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == installation_facts()


def test_a_caller_reading_an_older_schema_is_named_the_older_side(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(contracts._CURRENT, "endpoints", 2)
    result = runner.invoke(app, ["version", "--json", "--contract", "endpoints=1"])
    assert result.exit_code == 3
    assert "endpoints" in result.output
    assert "the caller is older than graftpunk" in result.output
    assert json.loads(result.stdout)["contracts"]["endpoints"] == 2


def test_a_caller_reading_a_newer_schema_names_graftpunk_the_older_side() -> None:
    result = runner.invoke(app, ["version", "--contract", "endpoints=2"])
    assert result.exit_code == 3
    assert "graftpunk is older than the caller" in result.output


def test_a_surface_graftpunk_does_not_serve_is_a_mismatch() -> None:
    result = runner.invoke(app, ["version", "--contract", "nosuch=1"])
    assert result.exit_code == 3
    assert "nosuch" in result.output


@pytest.mark.parametrize("value", ["endpoints", "endpoints=", "endpoints=one", "=1"])
def test_a_malformed_contract_exits_1_with_a_message(value: str) -> None:
    result = runner.invoke(app, ["version", "--contract", value])
    assert result.exit_code == 1
    assert "SURFACE=N" in result.output


def test_a_floor_not_met_exits_1_before_the_contracts_are_compared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(graftpunk, "__version__", "1.16.0")
    result = runner.invoke(
        app, ["version", "--json", "--at-least", "1.17.0", "--contract", "endpoints=2"]
    )
    assert result.exit_code == 1


@pytest.mark.parametrize(
    ("installed", "exit_code"), [("0.0.0+unknown", 1), ("1.17.0.dev3+g1234abc", 1)]
)
def test_a_dev_or_local_version_is_ordered_not_rejected(
    monkeypatch: pytest.MonkeyPatch, installed: str, exit_code: int
) -> None:
    """A local checkout reports a local or dev version; it orders below the release."""
    monkeypatch.setattr(graftpunk, "__version__", installed)
    result = runner.invoke(app, ["version", "--json", "--at-least", "1.17.0"])
    assert result.exit_code == exit_code, result.output


def test_two_mismatches_in_one_call_print_one_stderr_line_each() -> None:
    result = runner.invoke(app, ["version", "--contract", "endpoints=2", "--contract", "nosuch=1"])
    assert result.exit_code == 3
    lines = result.stderr.splitlines()
    assert len(lines) == 2
    assert lines[0].startswith("endpoints:")
    assert "graftpunk is older than the caller" in lines[0]
    assert lines[1].startswith("nosuch:")


def test_a_matching_contract_alone_exits_0_and_prints_nothing() -> None:
    result = runner.invoke(app, ["version", *_contract_args(cli_contracts())])
    assert result.exit_code == 0, result.output
    assert result.stdout == ""


@pytest.mark.parametrize(
    "extra",
    [["--at-least", "not-a-version"], ["--contract", "endpoints=one"]],
    ids=["unreadable-floor", "malformed-contract"],
)
def test_json_with_an_unreadable_argument_exits_1_before_printing_any_json(
    extra: list[str],
) -> None:
    result = runner.invoke(app, ["version", "--json", *extra])
    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr != ""


def _only(installed: dict[str, str]):
    """A metadata lookup that sees exactly *installed*, since one test environment
    can hold only the distribution it was built with."""

    def version(name: str) -> str:
        if name not in installed:
            raise PackageNotFoundError(name)
        return installed[name]

    return version


@pytest.mark.parametrize("name", ["graftpunk", "graftpunk-lite"])
def test_the_panel_names_the_installed_distribution(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    monkeypatch.setattr(_distribution, "_pkg_version", _only({name: "1.20.0"}))
    monkeypatch.setattr(graftpunk, "__version__", "1.20.0")
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0, result.output
    assert f"{name} 1.20.0" in result.stdout
    assert result.stderr == ""


@pytest.mark.parametrize("args", [[], ["--json"], ["--at-least", "1.0"]])
def test_both_distributions_installed_warns_on_stderr_naming_both_and_the_fix(
    monkeypatch: pytest.MonkeyPatch, args: list[str]
) -> None:
    monkeypatch.setattr(
        _distribution, "_pkg_version", _only({"graftpunk": "1.20.0", "graftpunk-lite": "1.19.0"})
    )
    result = runner.invoke(app, ["version", *args])
    assert result.exit_code == 0, result.output
    warning = " ".join(result.stderr.split())
    assert "graftpunk 1.20.0 and graftpunk-lite 1.19.0 are both installed" in warning
    assert "pip uninstall graftpunk-lite" in warning
    assert "pip install --force-reinstall graftpunk" in warning
    if args == ["--json"]:
        assert json.loads(result.stdout) == installation_facts()


def test_one_distribution_prints_no_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_distribution, "_pkg_version", _only({"graftpunk-lite": "1.20.0"}))
    result = runner.invoke(app, ["version", "--json"])
    assert result.exit_code == 0
    assert result.stderr == ""
