"""gp plugin info, add-command, upgrade, and check through the Typer runner
(graft skill spec, 2026-09-21)."""

from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
from pathlib import Path
from types import MappingProxyType

import pytest
from typer.testing import CliRunner

from graftpunk.cli.main import app
from graftpunk.devtools.plugin_info import PluginDefectRefusal, info_payload
from graftpunk.devtools.plugin_project import PluginDefect, ProjectView, read_project
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


def _add(project: Path, plugin: str, command: str) -> object:
    return runner.invoke(
        app,
        [
            "plugin",
            "add-command",
            plugin,
            "--from-run",
            "myshop",
            "--command",
            command,
            "--dir",
            str(project),
        ],
    )


def _snapshot(root: Path, *, skip: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(root)): p.read_bytes()
        for p in root.rglob("*")
        if p.is_file() and p != skip
    }


def _ruff_clean(project: Path) -> None:
    for argv in (["check", "."], ["format", "--check", "."]):
        result = subprocess.run(  # noqa: S603 - argv is fixed, not untrusted input
            [sys.executable, "-m", "ruff", *argv], cwd=project, capture_output=True, text=True
        )
        assert result.returncode == 0, result.stdout + result.stderr


_HAND_WRITTEN = """\
\"\"\"myshop plugin.\"\"\"

from __future__ import annotations

import functools

from graftpunk.plugins import SitePlugin


class MyshopPlugin(SitePlugin):
    site_name = "myshop"
    base_url = "https://myshop.example"


@functools.cache
def _helper() -> int:
    return 1


if __name__ == "__main__":
    print(_helper())
"""


def _hand_written_project(root: Path, entry_point: str = "myshop") -> Path:
    (root / "pyproject.toml").write_text(
        '[project]\nname = "graftpunk-myshop"\n\n'
        '[project.entry-points."graftpunk.plugins"]\n'
        f'{entry_point} = "graftpunk_myshop.plugin:MyshopPlugin"\n\n'
        "[tool.ruff]\nline-length = 100\n\n"
        '[tool.ruff.lint]\nselect = ["E", "F", "I", "UP", "B"]\n'
    )
    package = root / "src" / "graftpunk_myshop"
    package.mkdir(parents=True)
    module = package / "plugin.py"
    module.write_text(_HAND_WRITTEN)
    return module


@pytest.mark.usefixtures("gp_logging")
class TestAddCommand:
    def test_the_stub_goes_after_the_last_command(self, recorded: Path) -> None:
        _new(recorded, "myshop", "orders=GET /api/orders")
        result = _add(recorded, "myshop", "order=GET /api/orders/{order_id}")
        assert result.exit_code == 0, result.output
        (plugin,) = read_project(recorded).plugins
        assert [c.method for c in plugin.commands] == ["orders", "order"]
        assert plugin.commands[-1].endpoint == "GET /api/orders/{order_id}"
        _ruff_clean(recorded)

    def test_a_first_command_goes_after_the_class_bodys_last_statement(
        self, recorded: Path
    ) -> None:
        module = _hand_written_project(recorded)
        result = _add(recorded, "myshop", "orders=GET /api/orders")
        assert result.exit_code == 0, result.output
        (plugin,) = read_project(recorded).plugins
        (command,) = plugin.commands
        assert command.span.start > module.read_text().splitlines().index(
            '    base_url = "https://myshop.example"'
        )

    def test_a_decorated_helper_and_a_main_block_below_the_class_stay_below_it(
        self, recorded: Path
    ) -> None:
        module = _hand_written_project(recorded)
        result = _add(recorded, "myshop", "orders=GET /api/orders")
        assert result.exit_code == 0, result.output
        tree = ast.parse(module.read_text())
        (klass,) = [n for n in tree.body if isinstance(n, ast.ClassDef)]
        helper = next(n for n in tree.body if isinstance(n, ast.FunctionDef))
        assert any(isinstance(n, ast.FunctionDef) and n.name == "orders" for n in klass.body)
        assert klass.end_lineno is not None and klass.end_lineno < helper.lineno
        _ruff_clean(recorded)

    def test_a_typed_stub_imports_what_it_uses(self, recorded: Path) -> None:
        module = _hand_written_project(recorded)
        result = _add(recorded, "myshop", "orders=GET /api/orders")
        assert result.exit_code == 0, result.output
        imported = {
            alias.name
            for node in ast.parse(module.read_text()).body
            if isinstance(node, ast.ImportFrom) and node.module == "graftpunk.plugins"
            for alias in node.names
        }
        assert {"CommandContext", "PluginParamSpec", "SitePlugin", "command"} <= imported

    def test_an_import_it_cannot_place_is_refused_and_nothing_changes(self, recorded: Path) -> None:
        module = _hand_written_project(recorded)
        module.write_text(
            module.read_text().replace(
                "import functools\n", "import functools\n\nTIMEOUT = 30\n", 1
            )
        )
        before = _snapshot(recorded, skip=recorded / "nothing")
        result = _add(recorded, "myshop", "order=GET /api/orders/{order_id}")
        assert result.exit_code == 1, result.output
        (line,) = _plain(result.output).strip().splitlines()
        assert "from urllib.parse import quote as _quote_path" in line
        assert "ruff check --fix" in line
        assert _snapshot(recorded, skip=recorded / "nothing") == before

    def test_a_duplicate_name_is_refused_and_nothing_changes(self, recorded: Path) -> None:
        _new(recorded, "myshop", "orders=GET /api/orders")
        before = _snapshot(recorded, skip=recorded / "nothing")
        result = _add(recorded, "myshop", "orders=GET /api/orders/{order_id}")
        assert result.exit_code == 1
        assert "already has a command named 'orders'" in _plain(result.output)
        assert _snapshot(recorded, skip=recorded / "nothing") == before

    def test_a_hyphen_underscore_collision_is_refused(self, recorded: Path) -> None:
        _new(recorded, "myshop", "order_list=GET /api/orders")
        result = _add(recorded, "myshop", "order-list=GET /api/orders/{order_id}")
        assert result.exit_code == 1
        assert "already has a command" in _plain(result.output)

    def test_a_login_flow_endpoint_is_refused(self, recorded: Path) -> None:
        _new(recorded, "myshop", "orders=GET /api/orders")
        result = _add(recorded, "myshop", "session=POST /session")
        assert result.exit_code == 1
        assert "login flow" in _plain(result.output)

    def test_a_reserved_name_is_refused(self, recorded: Path) -> None:
        """login is the root command registration adds for every plugin."""
        _new(recorded, "myshop", "orders=GET /api/orders")
        result = _add(recorded, "myshop", "login=GET /api/orders/{order_id}")
        assert result.exit_code == 1
        assert "reserved" in _plain(result.output)

    def test_nothing_but_the_module_is_touched(self, recorded: Path) -> None:
        _new(recorded, "myshop", "orders=GET /api/orders")
        module = recorded / "src" / "graftpunk_myshop" / "plugin.py"
        before = _snapshot(recorded, skip=module)
        assert _add(recorded, "myshop", "invoices=GET /api/invoices").exit_code == 0
        assert _snapshot(recorded, skip=module) == before

    def test_the_fixture_path_for_a_standalone_project(self, recorded: Path) -> None:
        _new(recorded, "myshop", "orders=GET /api/orders")
        result = _add(recorded, "myshop", "order=GET /api/orders/{order_id}")
        assert "tests/fixtures/get_api_orders_{order_id}.json" in _plain(result.output)

    def test_the_fixture_path_for_a_suite_member(self, recorded: Path) -> None:
        _new(recorded, "myshop", "orders=GET /api/orders")
        _new(recorded, "widgets", "orders=GET /api/orders")
        result = _add(recorded, "widgets", "order=GET /api/orders/{order_id}")
        assert result.exit_code == 0, result.output
        assert "tests/fixtures/widgets/get_api_orders_{order_id}.json" in _plain(result.output)

    def test_a_defective_target_is_refused_and_another_plugins_defect_is_not(
        self, recorded: Path
    ) -> None:
        _new(recorded, "myshop", "orders=GET /api/orders")
        _new(recorded, "widgets", "orders=GET /api/orders")
        widgets = recorded / "src" / "graftpunk_widgets" / "plugin.py"
        widgets.write_text(widgets.read_text() + "\n\nclass Other(SitePlugin):\n    pass\n")
        refused = _add(recorded, "widgets", "order=GET /api/orders/{order_id}")
        assert refused.exit_code == 1
        assert "exactly one SitePlugin subclass" in " ".join(_plain(refused.output).split())
        assert _add(recorded, "myshop", "order=GET /api/orders/{order_id}").exit_code == 0

    def test_an_unknown_plugin_is_refused(self, recorded: Path) -> None:
        _new(recorded, "myshop", "orders=GET /api/orders")
        result = _add(recorded, "elsewhere", "order=GET /api/orders/{order_id}")
        assert result.exit_code == 1
        assert "myshop" in _plain(result.output)

    def test_the_plugin_is_addressed_by_its_entry_point_name(self, recorded: Path) -> None:
        """A hand-written project whose entry-point name is not its site_name: the
        entry point is the identity gp plugin info reports and add-command takes."""
        module = _hand_written_project(recorded, entry_point="shop")
        by_site_name = _add(recorded, "myshop", "orders=GET /api/orders")
        assert by_site_name.exit_code == 1
        assert "its entry points are: shop." in " ".join(_plain(by_site_name.output).split())
        assert module.read_text() == _HAND_WRITTEN
        added = _add(recorded, "shop", "orders=GET /api/orders")
        assert added.exit_code == 0, added.output
        (plugin,) = read_project(recorded).plugins
        assert (plugin.entry_point, plugin.site_name) == ("shop", "myshop")
        assert [c.method for c in plugin.commands] == ["orders"]

    def test_a_defect_on_a_plugin_addressed_by_its_entry_point_is_refused(
        self, recorded: Path
    ) -> None:
        module = _hand_written_project(recorded, entry_point="shop")
        module.write_text(_HAND_WRITTEN + "\n\nclass Other(SitePlugin):\n    pass\n")
        before = module.read_bytes()
        refused = _add(recorded, "shop", "orders=GET /api/orders")
        assert refused.exit_code == 1
        assert "exactly one SitePlugin subclass" in " ".join(_plain(refused.output).split())
        assert module.read_bytes() == before

    def test_a_failed_write_is_one_line_exit_1_and_the_original_bytes(
        self, recorded: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from graftpunk.devtools.scaffold import write

        _new(recorded, "myshop", "orders=GET /api/orders")
        before = _snapshot(recorded, skip=recorded / "nothing")

        def failing(path: Path, text: str) -> None:
            raise OSError(28, "No space left on device", str(path))

        monkeypatch.setattr(write, "_write_atomically", failing)
        result = _add(recorded, "myshop", "invoices=GET /api/invoices")
        assert result.exit_code == 1, result.output
        (line,) = _plain(result.output).strip().splitlines()
        assert line.startswith("Could not write ")
        assert "No space left on device" in line
        assert _snapshot(recorded, skip=recorded / "nothing") == before

    @pytest.mark.parametrize("value", ["orders GET /api/orders", "=GET /api/orders", "orders="])
    def test_both_entry_points_refuse_a_malformed_value_with_the_same_text(
        self, recorded: Path, value: str
    ) -> None:
        _new(recorded, "myshop", "orders=GET /api/orders")
        added = _add(recorded, "myshop", value)
        created = runner.invoke(
            app,
            [
                "plugin",
                "new",
                "other",
                "--from-run",
                "myshop",
                "--dir",
                str(recorded / "other"),
                "--command",
                value,
            ],
        )
        assert added.exit_code == created.exit_code == 1
        assert _plain(added.output) == _plain(created.output)

    def test_a_conftest_that_does_not_parse_does_not_stop_it(self, recorded: Path) -> None:
        """add-command edits the plugin module and never reads the conftest."""
        _new(recorded, "myshop", "orders=GET /api/orders")
        (recorded / "tests" / "conftest.py").write_text("def (:\n")
        result = _add(recorded, "myshop", "invoices=GET /api/invoices")
        assert result.exit_code == 0, result.output

    def test_a_directory_that_is_not_a_plugin_project_is_refused(self, recorded: Path) -> None:
        """The recording exists and the project directory is empty."""
        result = _add(recorded, "myshop", "orders=GET /api/orders")
        assert result.exit_code == 1
        assert "not a graftpunk plugin project (empty)" in " ".join(_plain(result.output).split())


def _project_lacking_the_wiring(root: Path) -> Path:
    """A generated project whose conftest predates the sanitisation wiring."""
    from graftpunk.devtools.scaffold.project import write_scaffold
    from graftpunk.devtools.scaffold.render import ScaffoldSpec

    write_scaffold(
        root,
        ScaffoldSpec(
            name="myshop",
            mode="new_project",
            backend="nodriver",
            base_url="https://myshop.example",
        ),
    )
    conftest = root / "tests" / "conftest.py"
    conftest.write_text(
        "from graftpunk.testing.plugin import site_env_scrubber\n\n"
        'scrub_site_env = site_env_scrubber("MYSHOP_")\n'
    )
    return conftest


@pytest.mark.usefixtures("gp_logging")
class TestPluginUpgrade:
    def test_a_failed_write_is_one_line_exit_1_and_the_original_bytes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from graftpunk.devtools.scaffold import write

        conftest = _project_lacking_the_wiring(tmp_path)
        before = conftest.read_bytes()

        def failing(path: Path, text: str) -> None:
            raise OSError(28, "No space left on device", str(path))

        monkeypatch.setattr(write, "_write_atomically", failing)
        result = runner.invoke(app, ["plugin", "upgrade", "--dir", str(tmp_path)])
        assert result.exit_code == 1, result.output
        (line,) = _plain(result.output).strip().splitlines()
        assert line.startswith("Could not write ")
        assert "No space left on device" in line
        assert conftest.read_bytes() == before

    def test_prints_what_it_added_and_then_nothing(self, tmp_path: Path) -> None:
        _project_lacking_the_wiring(tmp_path)
        first = runner.invoke(app, ["plugin", "upgrade", "--dir", str(tmp_path)])
        assert first.exit_code == 0, first.output
        assert "tests/conftest.py: added FIXTURES_TREE" in _plain(first.output)
        assert "tests/conftest.py: added sanitised_fixtures" in _plain(first.output)
        second = runner.invoke(app, ["plugin", "upgrade", "--dir", str(tmp_path)])
        assert second.exit_code == 0
        assert "Nothing to upgrade" in _plain(second.output)
