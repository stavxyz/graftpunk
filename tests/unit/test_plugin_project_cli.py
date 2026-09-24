"""gp plugin info, add-command, upgrade, and check through the Typer runner
(graft skill spec, 2026-09-21)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from types import MappingProxyType

import pytest
from typer.testing import CliRunner

from graftpunk.cli.main import app
from graftpunk.devtools.plugin_info import PluginDefectRefusal, info_payload
from graftpunk.devtools.plugin_project import PluginDefect, ProjectView
from graftpunk.devtools.scaffold.policy import PROJECT_REQUIREMENTS

runner = CliRunner()

# The info payload's field sets at schema 1, frozen here so a rename fails the suite.
_INFO_V1 = {"schema", "directory", "plugins"}
_INFO_PLUGIN_V1 = {"entry_point", "module", "site_name", "base_url", "commands"}
_INFO_COMMAND_V1 = {"name", "endpoint"}


def _plain(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def _entry(
    method: str, url: str, *, content_type: str = "application/json", body: str = "{}"
) -> dict:
    return {
        "startedDateTime": "2026-09-10T10:00:00.000Z",
        "time": 1,
        "request": {"method": method, "url": url, "headers": [], "cookies": [], "queryString": []},
        "response": {
            "status": 200,
            "statusText": "OK",
            "headers": [{"name": "Content-Type", "value": content_type}],
            "cookies": [],
            "content": {"mimeType": content_type, "text": body, "size": len(body)},
        },
    }


_LOGIN_PAGE = (
    '<form action="/session" method="post"><input type="email" id="email" name="email">'
    '<input type="password" id="password" name="password"></form>'
)


def _credential_post() -> dict:
    entry = _entry("POST", "https://myshop.example/session")
    entry["request"]["postData"] = {
        "mimeType": "application/json",
        "text": json.dumps({"email": "alice@example.com", "password": "invented"}),
    }
    return entry


def _run_entries() -> list[dict]:
    return [
        _entry("GET", "https://myshop.example/login", content_type="text/html", body=_LOGIN_PAGE),
        _credential_post(),
        _entry("GET", "https://myshop.example/api/orders?page=2", body='{"orders": []}'),
        _entry("GET", "https://myshop.example/api/orders/1001", body='{"id": "1001"}'),
        _entry("GET", "https://myshop.example/api/invoices", body='{"invoices": []}'),
    ]


@pytest.fixture()
def recorded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A recording named myshop, and the directory a project is generated into."""
    observe_base = tmp_path / "observe"
    monkeypatch.setattr("graftpunk.cli.observe_commands.OBSERVE_BASE_DIR", observe_base)
    run_dir = observe_base / "myshop" / "run-1"
    run_dir.mkdir(parents=True)
    har = {"log": {"version": "1.2", "entries": _run_entries()}}
    (run_dir / "network.har").write_text(json.dumps(har))
    project = tmp_path / "project"
    project.mkdir()
    return project


def _new(project: Path, name: str = "myshop", *commands: str) -> None:
    argv = ["plugin", "new", name, "--from-run", "myshop", "--dir", str(project)]
    for command in commands:
        argv += ["--command", command]
    result = runner.invoke(app, argv)
    assert result.exit_code == 0, result.output


def _info(project: Path) -> dict:
    result = runner.invoke(app, ["plugin", "info", "--json", "--dir", str(project)])
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


class TestPluginInfo:
    def test_an_empty_directory(self, tmp_path: Path) -> None:
        assert _info(tmp_path) == {"schema": 1, "directory": "empty", "plugins": []}

    def test_a_foreign_directory(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "other"\n')
        assert _info(tmp_path)["directory"] == "foreign"

    def test_a_standalone_project_lists_one_plugin_with_the_pinned_fields(
        self, recorded: Path
    ) -> None:
        _new(recorded)
        payload = _info(recorded)
        assert set(payload) == _INFO_V1
        assert payload["schema"] == 1
        assert payload["directory"] == "plugin"
        (plugin,) = payload["plugins"]
        assert set(plugin) == _INFO_PLUGIN_V1
        assert plugin["entry_point"] == "myshop"
        assert plugin["module"] == "src/graftpunk_myshop/plugin.py"
        assert (plugin["site_name"], plugin["base_url"]) == ("myshop", "https://myshop.example")
        for command in plugin["commands"]:
            assert set(command) == _INFO_COMMAND_V1
        assert {"name": "api-orders", "endpoint": "GET /api/orders"} in plugin["commands"]

    def test_a_suite_lists_every_entry_point(self, recorded: Path) -> None:
        _new(recorded, "myshop")
        _new(recorded, "widgets")
        plugins = _info(recorded)["plugins"]
        assert [p["entry_point"] for p in plugins] == ["myshop", "widgets"]
        assert [p["site_name"] for p in plugins] == ["myshop", "widgets"]

    def test_an_undeclared_command_reports_null(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "graftpunk-myshop"\n\n'
            '[project.entry-points."graftpunk.plugins"]\n'
            'myshop = "graftpunk_myshop.plugin:MyshopPlugin"\n'
        )
        package = tmp_path / "src" / "graftpunk_myshop"
        package.mkdir(parents=True)
        (package / "plugin.py").write_text(
            "from graftpunk.plugins import SitePlugin, command\n\n\n"
            "class MyshopPlugin(SitePlugin):\n"
            '    site_name = "myshop"\n\n'
            '    @command(help="One order", name="order")\n'
            "    def by_id(self, ctx, order_id: str) -> dict:\n"
            "        return {}\n"
        )
        (plugin,) = _info(tmp_path)["plugins"]
        assert plugin["commands"] == [{"name": "order", "endpoint": None}]
        assert plugin["base_url"] is None

    def test_the_payload_carries_no_installation_facts(self, recorded: Path) -> None:
        _new(recorded)
        payload = _info(recorded)
        assert "contracts" not in payload and "graftpunk" not in payload

    def test_json_is_required(self, tmp_path: Path) -> None:
        result = runner.invoke(app, ["plugin", "info", "--dir", str(tmp_path)])
        assert result.exit_code == 1
        assert "--json" in _plain(result.output)

    def test_a_defective_plugin_module_is_refused_not_left_out(self, recorded: Path) -> None:
        _new(recorded)
        module = recorded / "src" / "graftpunk_myshop" / "plugin.py"
        module.write_text(module.read_text() + "\n\nclass Other(SitePlugin):\n    pass\n")
        result = runner.invoke(app, ["plugin", "info", "--json", "--dir", str(recorded)])
        assert result.exit_code == 1
        output = " ".join(_plain(result.output).split())
        assert "entry point 'myshop':" in output
        assert "exactly one SitePlugin subclass, found 2" in output

    def test_an_unreadable_pyproject_is_a_one_line_refusal(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text("[project\n")
        result = runner.invoke(app, ["plugin", "info", "--json", "--dir", str(tmp_path)])
        assert result.exit_code == 1
        assert "pyproject.toml: not valid TOML" in _plain(result.output)
        assert "Traceback" not in result.output

    def test_a_conftest_that_does_not_parse_does_not_stop_it(self, recorded: Path) -> None:
        """info never reads the conftest: an unreadable requirement file is upgrade's
        and check's business."""
        _new(recorded)
        (recorded / "tests" / "conftest.py").write_text("def (:\n")
        (plugin,) = _info(recorded)["plugins"]
        assert plugin["entry_point"] == "myshop"


def test_info_payload_refuses_a_view_with_a_defect_and_names_every_one() -> None:
    """The refusal is info_payload's own, so no caller can build a payload that
    leaves a defective plugin out."""
    defects = tuple(
        PluginDefect(
            entry_point=name,
            module_path=f"src/graftpunk_{name}/plugin.py",
            message=f"{name} is broken",
        )
        for name in ("myshop", "widgets")
    )
    view = ProjectView(
        directory="plugin",
        plugins=(),
        defects=defects,
        requirements=MappingProxyType({}),
        requirement_set=PROJECT_REQUIREMENTS,
    )
    with pytest.raises(PluginDefectRefusal) as caught:
        info_payload(view)
    assert str(caught.value).splitlines() == [
        "entry point 'myshop': myshop is broken",
        "entry point 'widgets': widgets is broken",
    ]
    assert caught.value.defects == defects
