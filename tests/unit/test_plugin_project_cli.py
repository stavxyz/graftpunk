"""gp plugin info, add-command, upgrade, and check through the Typer runner
(graft skill spec, 2026-09-21)."""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from types import MappingProxyType

import pytest
from typer.testing import CliRunner, Result

from graftpunk.cli.main import app
from graftpunk.devtools.plugin_info import PluginDefectRefusal, info_payload
from graftpunk.devtools.plugin_project import PluginDefect, ProjectView, read_project
from graftpunk.devtools.scaffold.policy import PROJECT_REQUIREMENTS
from graftpunk.har.parser import HARParseError, parse_har_file

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
        _entry("GET", "https://myshop.example/api/invoices/2001", body='{"id": "2001"}'),
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


@pytest.mark.usefixtures("gp_logging")
class TestPluginInfo:
    def test_an_empty_directory(self, tmp_path: Path) -> None:
        assert _info(tmp_path) == {"schema": 1, "directory": "empty", "plugins": []}

    def test_a_foreign_directory(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "other"\n')
        assert _info(tmp_path)["directory"] == "foreign"

    def test_a_dangling_pyproject_symlink_is_refused_not_read_as_empty(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "pyproject.toml").symlink_to(tmp_path / "nonexistent.toml")
        result = runner.invoke(app, ["plugin", "info", "--json", "--dir", str(tmp_path)])
        assert result.exit_code == 1
        assert "exists but is not a regular file" in _plain(result.output)

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

    def test_a_command_group_is_listed_by_its_registered_name(self, tmp_path: Path) -> None:
        _hand_written_project(tmp_path, module_text=_HAND_WRITTEN_WITH_GROUP)
        (plugin,) = _info(tmp_path)["plugins"]
        assert {"name": "admin", "endpoint": None} in plugin["commands"]

    def test_the_payload_carries_no_installation_facts(self, recorded: Path) -> None:
        _new(recorded)
        payload = _info(recorded)
        assert "contracts" not in payload and "graftpunk" not in payload

    def test_a_dir_that_does_not_exist_is_refused_not_read_as_empty(self, tmp_path: Path) -> None:
        """A mistyped --dir must not read as "empty" (create mode)."""
        missing = tmp_path / "nonexistent"
        result = runner.invoke(app, ["plugin", "info", "--json", "--dir", str(missing)])
        assert result.exit_code == 1
        assert _plain(result.output).strip() == f"{missing}: no such directory."

    def test_a_dir_that_is_a_regular_file_is_refused(self, tmp_path: Path) -> None:
        not_a_dir = tmp_path / "plain_file"
        not_a_dir.write_text("not a directory")
        result = runner.invoke(app, ["plugin", "info", "--json", "--dir", str(not_a_dir)])
        assert result.exit_code == 1
        assert _plain(result.output).strip() == f"{not_a_dir}: not a directory."

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

    @pytest.mark.skipif(os.geteuid() == 0, reason="root reads a 000-mode file")
    def test_an_unreadable_pyproject_by_permission_is_a_one_line_refusal(
        self, tmp_path: Path
    ) -> None:
        """A PermissionError (any OSError), not just a decode error, is a
        one-line refusal."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text('[project]\nname = "x"\n')
        pyproject.chmod(0)
        try:
            result = runner.invoke(app, ["plugin", "info", "--json", "--dir", str(tmp_path)])
        finally:
            pyproject.chmod(0o644)
        assert result.exit_code == 1
        (line,) = _plain(result.output).strip().splitlines()
        assert "pyproject.toml: cannot be read" in line
        assert "Traceback" not in result.output

    @pytest.mark.skipif(os.geteuid() == 0, reason="root reads a 000-mode file")
    def test_an_unreadable_plugin_module_by_permission_is_a_one_line_refusal(
        self, recorded: Path
    ) -> None:
        """Same as above, for the plugin module."""
        _new(recorded)
        module = recorded / "src" / "graftpunk_myshop" / "plugin.py"
        module.chmod(0)
        try:
            result = runner.invoke(app, ["plugin", "info", "--json", "--dir", str(recorded)])
        finally:
            module.chmod(0o644)
        assert result.exit_code == 1
        (line,) = _plain(result.output).strip().splitlines()
        assert "plugin.py: cannot be read" in line
        assert "Traceback" not in result.output

    def test_a_pyproject_with_a_bad_byte_is_a_one_line_refusal(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_bytes(b"[project]\nname = \xff\n")
        result = runner.invoke(app, ["plugin", "info", "--json", "--dir", str(tmp_path)])
        assert isinstance(result.exception, SystemExit)
        assert result.exit_code == 1
        (line,) = _plain(result.output).strip().splitlines()
        assert "pyproject.toml" in line
        assert "UTF-8" in line

    def test_a_plugin_module_with_a_bad_byte_is_a_one_line_refusal(self, recorded: Path) -> None:
        _new(recorded)
        module = recorded / "src" / "graftpunk_myshop" / "plugin.py"
        module.write_bytes(module.read_bytes() + b"\n# \xff\n")
        result = runner.invoke(app, ["plugin", "info", "--json", "--dir", str(recorded)])
        assert isinstance(result.exception, SystemExit)
        assert result.exit_code == 1
        (line,) = _plain(result.output).strip().splitlines()
        assert "plugin.py" in line
        assert "UTF-8" in line

    def test_a_plugin_module_with_a_nul_byte_is_a_one_line_refusal(self, recorded: Path) -> None:
        _new(recorded)
        module = recorded / "src" / "graftpunk_myshop" / "plugin.py"
        module.write_text(module.read_text() + "\nBROKEN = '\x00'\n")
        result = runner.invoke(app, ["plugin", "info", "--json", "--dir", str(recorded)])
        assert isinstance(result.exception, SystemExit)
        assert result.exit_code == 1
        (line,) = _plain(result.output).strip().splitlines()
        assert "plugin.py" in line
        assert "does not parse" in line
        assert "line None" not in line

    def test_a_non_table_project_is_a_one_line_refusal(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text('project = "x"\n')
        result = runner.invoke(app, ["plugin", "info", "--json", "--dir", str(tmp_path)])
        assert isinstance(result.exception, SystemExit)
        assert result.exit_code == 1
        (line,) = _plain(result.output).strip().splitlines()
        assert "pyproject.toml" in line

    def test_a_non_table_entry_points_is_a_one_line_refusal(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text('[project]\nentry-points = "x"\n')
        result = runner.invoke(app, ["plugin", "info", "--json", "--dir", str(tmp_path)])
        assert isinstance(result.exception, SystemExit)
        assert result.exit_code == 1
        (line,) = _plain(result.output).strip().splitlines()
        assert "pyproject.toml" in line

    def test_a_non_table_plugins_group_is_a_one_line_refusal(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            '[project.entry-points]\n"graftpunk.plugins" = "x"\n'
        )
        result = runner.invoke(app, ["plugin", "info", "--json", "--dir", str(tmp_path)])
        assert isinstance(result.exception, SystemExit)
        assert result.exit_code == 1
        (line,) = _plain(result.output).strip().splitlines()
        assert "pyproject.toml" in line

    def test_a_non_string_entry_point_value_is_a_one_line_refusal(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            '[project.entry-points."graftpunk.plugins"]\nmyshop = 1\n'
        )
        result = runner.invoke(app, ["plugin", "info", "--json", "--dir", str(tmp_path)])
        assert isinstance(result.exception, SystemExit)
        assert result.exit_code == 1
        (line,) = _plain(result.output).strip().splitlines()
        assert "pyproject.toml" in line
        assert "myshop" in line

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
        test_markers=(),
        fixtures_tree_present=True,
    )
    with pytest.raises(PluginDefectRefusal) as caught:
        info_payload(view)
    assert str(caught.value).splitlines() == [
        "entry point 'myshop': myshop is broken",
        "entry point 'widgets': widgets is broken",
    ]
    assert caught.value.defects == defects


def _add(project: Path, plugin: str, command: str) -> Result:
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


_HAND_WRITTEN_WITH_HELPER = """\
\"\"\"myshop plugin.\"\"\"

from __future__ import annotations

from graftpunk.plugins import SitePlugin


class MyshopPlugin(SitePlugin):
    site_name = "myshop"
    base_url = "https://myshop.example"

    def orders(self) -> list:
        return []
"""

_HAND_WRITTEN_WITH_ATTRIBUTE = """\
\"\"\"myshop plugin.\"\"\"

from __future__ import annotations

from graftpunk.plugins import SitePlugin


class MyshopPlugin(SitePlugin):
    site_name = "myshop"
    base_url = "https://myshop.example"

    orders = 1
"""

_HAND_WRITTEN_WITH_GROUP = """\
\"\"\"myshop plugin.\"\"\"

from __future__ import annotations

from graftpunk.plugins import CommandContext, SitePlugin, command


class MyshopPlugin(SitePlugin):
    site_name = "myshop"
    base_url = "https://myshop.example"

    @command(help="Admin commands")
    class Admin:
        @command(help="List admins")
        def list(self, ctx: CommandContext) -> dict:
            return {}
"""

_HAND_WRITTEN_WITH_ENDPOINTLESS_COMMAND = """\
\"\"\"myshop plugin.\"\"\"

from __future__ import annotations

from graftpunk.plugins import CommandContext, SitePlugin, command


class MyshopPlugin(SitePlugin):
    site_name = "myshop"
    base_url = "https://myshop.example"

    @command(help="List orders")
    def orders(self, ctx: CommandContext) -> dict:
        return {}
"""

_HAND_WRITTEN_WITH_PINNED_NAME = """\
\"\"\"myshop plugin.\"\"\"

from __future__ import annotations

from graftpunk.plugins import CommandContext, SitePlugin, command


class MyshopPlugin(SitePlugin):
    site_name = "myshop"
    base_url = "https://myshop.example"

    @command(help="One order", name="order")
    def by_id(self, ctx: CommandContext) -> dict:
        return {}
"""

_HAND_WRITTEN_WITH_COMMENTED_IMPORT = """\
\"\"\"myshop plugin.\"\"\"

from __future__ import annotations

from graftpunk.plugins import SitePlugin  # noqa: F401


class MyshopPlugin(SitePlugin):
    site_name = "myshop"
    base_url = "https://myshop.example"
"""

_HAND_WRITTEN_TAB_INDENTED = (
    '"""myshop plugin."""\n'
    "\n"
    "from __future__ import annotations\n"
    "\n"
    "from graftpunk.plugins import SitePlugin\n"
    "\n"
    "\n"
    "class MyshopPlugin(SitePlugin):\n"
    '\tsite_name = "myshop"\n'
    '\tbase_url = "https://myshop.example"\n'
)

_HAND_WRITTEN_ONE_LINE_CLASS = (
    '"""myshop plugin."""\n'
    "\n"
    "from __future__ import annotations\n"
    "\n"
    "from graftpunk.plugins import SitePlugin\n"
    "\n"
    "\n"
    'class MyshopPlugin(SitePlugin): site_name = "myshop"\n'
)

_HAND_WRITTEN_MULTILINE_HEADER_ONE_LINE_CLASS = (
    '"""myshop plugin."""\n'
    "\n"
    "from __future__ import annotations\n"
    "\n"
    "from graftpunk.plugins import SitePlugin\n"
    "\n"
    "\n"
    "class MyshopPlugin(\n"
    "    SitePlugin,\n"
    '): site_name = "myshop"; base_url = "https://myshop.example"\n'
)

_HAND_WRITTEN_MULTILINE_HEADER_INDENTED_ONE_LINE_CLASS = (
    '"""myshop plugin."""\n'
    "\n"
    "from __future__ import annotations\n"
    "\n"
    "from graftpunk.plugins import SitePlugin\n"
    "\n"
    "\n"
    "class MyshopPlugin(\n"
    "    SitePlugin,\n"
    '    ): site_name = "myshop"; base_url = "https://myshop.example"\n'
)

_HAND_WRITTEN_BACKSLASH_CONTINUATION_CLASS = (
    '"""myshop plugin."""\n'
    "\n"
    "from __future__ import annotations\n"
    "\n"
    "from graftpunk.plugins import SitePlugin\n"
    "\n"
    "\n"
    "class MyshopPlugin(SitePlugin): \\\n"
    '    site_name = "myshop"\n'
)

_HAND_WRITTEN_PAREN_BACKSLASH_HEADER_CLASS = (
    '"""myshop plugin."""\n'
    "\n"
    "from __future__ import annotations\n"
    "\n"
    "from graftpunk.plugins import SitePlugin\n"
    "\n"
    "\n"
    "class MyshopPlugin(\\\n"
    "    SitePlugin\n"
    "):\n"
    '    site_name = "myshop"\n'
    '    base_url = "https://myshop.example"\n'
)

_HAND_WRITTEN_COMMENT_BACKSLASH_HEADER_CLASS = (
    '"""myshop plugin."""\n'
    "\n"
    "from __future__ import annotations\n"
    "\n"
    "from graftpunk.plugins import SitePlugin\n"
    "\n"
    "\n"
    "class MyshopPlugin(SitePlugin):  # windows path C:\\\n"
    '    site_name = "myshop"\n'
    '    base_url = "https://myshop.example"\n'
)

_HAND_WRITTEN_WITH_TRAILING_COMMENT = """\
\"\"\"myshop plugin.\"\"\"

from __future__ import annotations

from graftpunk.plugins import CommandContext, SitePlugin, command


class MyshopPlugin(SitePlugin):
    site_name = "myshop"
    base_url = "https://myshop.example"

    @command(help="List orders")
    def orders(self, ctx: CommandContext) -> dict:
        return ctx.request_json("GET", "/api/orders", role="xhr")
        # TODO(alice): pagination

    def _helper(self) -> int:
        return 1
"""

_HAND_WRITTEN_WITH_MODULE_HELPER = """\
\"\"\"myshop plugin.\"\"\"

from __future__ import annotations

from graftpunk.plugins import SitePlugin


def helper() -> int:
    return 1


class MyshopPlugin(SitePlugin):
    site_name = "myshop"
    base_url = "https://myshop.example"
"""


def _hand_written_project(
    root: Path, entry_point: str = "myshop", module_text: str = _HAND_WRITTEN
) -> Path:
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
    module.write_text(module_text)
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

    def test_prints_a_gp_fill_note_instead_of_a_fixture_no_one_writes(self, recorded: Path) -> None:
        """An endpoint gp observe fixtures writes no fixture for (a binary
        response with no captured text) must not be told to write its test against a
        fixture that will never exist."""
        _new(recorded, "myshop", "orders=GET /api/orders")
        har_path = recorded.parent / "observe" / "myshop" / "run-1" / "network.har"
        har = json.loads(har_path.read_text())
        har["log"]["entries"].append(
            {
                "startedDateTime": "2026-09-10T10:00:00.000Z",
                "time": 1,
                "request": {
                    "method": "GET",
                    "url": "https://myshop.example/api/export",
                    "headers": [],
                    "cookies": [],
                    "queryString": [],
                },
                "response": {
                    "status": 200,
                    "statusText": "OK",
                    "headers": [{"name": "Content-Type", "value": "application/octet-stream"}],
                    "cookies": [],
                    "content": {"mimeType": "application/octet-stream", "size": 1024},
                },
            }
        )
        har_path.write_text(json.dumps(har))
        result = _add(recorded, "myshop", "export=GET /api/export")
        assert result.exit_code == 0, result.output
        output = _plain(result.output)
        assert "gp observe fixtures writes no fixture for this endpoint" in output
        assert "write its test against tests/" not in output

    def test_crlf_line_endings_are_kept_throughout(self, recorded: Path) -> None:
        module = _hand_written_project(recorded)
        module.write_bytes(_HAND_WRITTEN.replace("\n", "\r\n").encode())
        result = _add(recorded, "myshop", "orders=GET /api/orders")
        assert result.exit_code == 0, result.output
        written = module.read_bytes()
        assert b"\r\n" in written
        assert b"\n" not in written.replace(b"\r\n", b"")

    def test_a_second_path_parameter_command_does_not_duplicate_the_quote_import(
        self, recorded: Path
    ) -> None:
        """The common enhance-mode case: a second detail endpoint needs the same
        urllib.parse import the first one already placed."""
        _new(recorded, "myshop", "orders=GET /api/orders")
        first = _add(recorded, "myshop", "order=GET /api/orders/{order_id}")
        assert first.exit_code == 0, first.output
        second = _add(recorded, "myshop", "invoice=GET /api/invoices/{invoice_id}")
        assert second.exit_code == 0, second.output
        module = recorded / "src" / "graftpunk_myshop" / "plugin.py"
        imports = [
            node
            for node in ast.parse(module.read_text()).body
            if isinstance(node, ast.ImportFrom) and node.module == "urllib.parse"
        ]
        (imp,) = imports
        names = [a.name if a.asname is None else f"{a.name} as {a.asname}" for a in imp.names]
        assert names == ["quote as _quote_path"]
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

    def test_a_form_feed_above_the_class_does_not_corrupt_the_insertion(
        self, recorded: Path
    ) -> None:
        """str.splitlines() also breaks on a form feed, which desyncs the ast line
        number the stub is spliced at from a str.splitlines() index; the old
        splitter raised an uncaught IndentationError here."""
        module_text = (
            '"""myshop plugin."""\n'
            "\n"
            "from __future__ import annotations\n"
            "\n"
            "from graftpunk.plugins import SitePlugin\n"
            "\n"
            "# page\x0c break\n"
            "\n"
            "\n"
            "class MyshopPlugin(SitePlugin):\n"
            '    site_name = "myshop"\n'
            '    base_url = "https://myshop.example"\n'
        )
        module = _hand_written_project(recorded, module_text=module_text)
        result = _add(recorded, "myshop", "orders=GET /api/orders")
        assert result.exit_code == 0, result.output
        text = module.read_text()
        assert "page\x0c break" in text
        assert "def orders(" in text
        ast.parse(text)
        _ruff_clean(recorded)

    def test_a_trailing_comment_at_the_command_bodys_indentation_stays_with_it(
        self, recorded: Path
    ) -> None:
        """The insertion point was the last command's last statement, so a
        comment immediately below it (at the command body's own indentation)
        read as the end of the new stub's body instead of staying with the
        command it actually follows."""
        module = _hand_written_project(recorded, module_text=_HAND_WRITTEN_WITH_TRAILING_COMMENT)
        result = _add(recorded, "myshop", "invoices=GET /api/invoices")
        assert result.exit_code == 0, result.output
        text = module.read_text()
        assert (
            'return ctx.request_json("GET", "/api/orders", role="xhr")\n'
            "        # TODO(alice): pagination\n"
        ) in text
        assert text.index("# TODO(alice): pagination") < text.index("def invoices(")
        ast.parse(text)
        _ruff_clean(recorded)

    def test_a_tab_indented_class_gets_a_tab_indented_stub(self, recorded: Path) -> None:
        """The stub was always rendered at four spaces and spliced in as-is, so
        a tab-indented class got a mixed-indentation module that does not even
        parse."""
        module = _hand_written_project(recorded, module_text=_HAND_WRITTEN_TAB_INDENTED)
        result = _add(recorded, "myshop", "orders=GET /api/orders")
        assert result.exit_code == 0, result.output
        text = module.read_text()
        assert "\tdef orders(" in text
        assert "    def orders(" not in text
        ast.parse(text)
        namespace: dict[str, object] = {}
        exec(compile(text, "plugin.py", "exec"), namespace)  # noqa: S102
        assert namespace["MyshopPlugin"]().site_name == "myshop"  # type: ignore[operator]
        # ruff format --check would reformat a tab-indented file to spaces on
        # its own (tabs are not this project's own gate's business); ruff
        # check (lint, no reformatting) is what a tab-indented project passes.
        check = subprocess.run(  # noqa: S603 - argv is fixed, not untrusted input
            [sys.executable, "-m", "ruff", "check", "."],
            cwd=recorded,
            capture_output=True,
            text=True,
        )
        assert check.returncode == 0, check.stdout + check.stderr

    def test_a_one_line_class_body_is_refused(self, recorded: Path) -> None:
        """Splicing a stub after a one-line class's body, itself on the `class`
        line, put the stub outside the class."""
        module = _hand_written_project(recorded, module_text=_HAND_WRITTEN_ONE_LINE_CLASS)
        before = module.read_bytes()
        result = _add(recorded, "myshop", "orders=GET /api/orders")
        assert result.exit_code == 1
        output = _plain(result.output).strip()
        assert output.splitlines() == [
            "src/graftpunk_myshop/plugin.py: the stub does not fit this class's "
            "layout (its body starts on the class header's line); add a command "
            "by hand."
        ]
        assert module.read_bytes() == before

    def test_a_multiline_header_with_a_one_line_body_is_refused(self, recorded: Path) -> None:
        """A class whose header spans lines, with its body on the `):` line:
        klass.body[0].lineno != klass.lineno (the header's own last line, not
        the `class` keyword's line), so the body-on-the-header-line guard
        alone misses this shape; the computed indentation unit is empty too
        (the body shares a line with `):`), which is the shared signal both
        shapes refuse on."""
        module = _hand_written_project(
            recorded, module_text=_HAND_WRITTEN_MULTILINE_HEADER_ONE_LINE_CLASS
        )
        before = module.read_bytes()
        result = _add(recorded, "myshop", "orders=GET /api/orders")
        assert result.exit_code == 1
        output = _plain(result.output).strip()
        assert output.splitlines() == [
            "src/graftpunk_myshop/plugin.py: the stub does not fit this class's "
            "layout (its body starts on the class header's line); add a command "
            "by hand."
        ]
        assert module.read_bytes() == before

    def test_a_multiline_header_with_an_indented_one_line_body_is_refused(
        self, recorded: Path
    ) -> None:
        """A class whose header spans lines, with its body on an indented `):`
        line: the body's own line is not empty before the statement starts
        (four spaces of header text precede it), so it is refused the same as
        the other two layouts, not spliced in as a syntax error."""
        module = _hand_written_project(
            recorded, module_text=_HAND_WRITTEN_MULTILINE_HEADER_INDENTED_ONE_LINE_CLASS
        )
        before = module.read_bytes()
        result = _add(recorded, "myshop", "orders=GET /api/orders")
        assert result.exit_code == 1
        output = _plain(result.output).strip()
        assert output.splitlines() == [
            "src/graftpunk_myshop/plugin.py: the stub does not fit this class's "
            "layout (its body starts on the class header's line); add a command "
            "by hand."
        ]
        assert module.read_bytes() == before

    def test_a_backslash_continued_header_with_a_one_line_body_is_refused(
        self, recorded: Path
    ) -> None:
        """A header joined to its body by a backslash continuation
        (`class X(SitePlugin): \\` then an indented next line) puts the body
        on its own physical line with nothing but whitespace before its
        column offset, which the body-on-the-header-line guard alone misses;
        without the backslash check, the stub is spliced in and the written
        module fails to parse ("unexpected indent") instead of refusing."""
        module = _hand_written_project(
            recorded, module_text=_HAND_WRITTEN_BACKSLASH_CONTINUATION_CLASS
        )
        before = module.read_bytes()
        result = _add(recorded, "myshop", "orders=GET /api/orders")
        assert result.exit_code == 1
        output = _plain(result.output).strip()
        assert output.splitlines() == [
            "src/graftpunk_myshop/plugin.py: the stub does not fit this class's "
            "layout (its body starts on the class header's line); add a command "
            "by hand."
        ]
        assert module.read_bytes() == before

    def test_a_redundant_backslash_inside_the_headers_parens_still_inserts(
        self, recorded: Path
    ) -> None:
        """`class MyshopPlugin(\\` then `    SitePlugin` then `):` then an
        indented body on its own line: the backslash is redundant (the open
        paren already continues the line) and never joins the body to the
        header, so this is an ordinary multi-line class the inserter must
        accept, not a one-line-body refusal."""
        module = _hand_written_project(
            recorded, module_text=_HAND_WRITTEN_PAREN_BACKSLASH_HEADER_CLASS
        )
        result = _add(recorded, "myshop", "orders=GET /api/orders")
        assert result.exit_code == 0, result.output
        text = module.read_text()
        assert "def orders(" in text
        # Not _ruff_clean: the redundant backslash inside the header's own
        # parens is this test's point and ruff format would rewrite it away,
        # which has nothing to do with whether the stub was placed correctly.
        ast.parse(text)

    def test_a_comment_ending_in_backslash_still_inserts(self, recorded: Path) -> None:
        """A comment on the header line ending in `\\` (e.g. a Windows path)
        is not a line continuation; the body still begins its own line below
        it, so this must insert, not refuse with a false "body starts on the
        class header's line" message."""
        module = _hand_written_project(
            recorded, module_text=_HAND_WRITTEN_COMMENT_BACKSLASH_HEADER_CLASS
        )
        result = _add(recorded, "myshop", "orders=GET /api/orders")
        assert result.exit_code == 0, result.output
        text = module.read_text()
        assert "def orders(" in text
        ast.parse(text)
        _ruff_clean(recorded)

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

    def test_the_guide_quotes_what_add_command_prints(self, recorded: Path) -> None:
        """The guide's add-command section pastes this command's output; a wording
        change here has to reach it. Every kind of line it quotes is produced here
        and compared with the guide's after the parts that name this test's
        recording (command names, fixture names, the project path) are made
        generic on both sides."""
        from tests.unit.guide_harness import GUIDE_TEXT, blocks, section

        def generic(line: str) -> str:
            line = line.replace(f"{recorded}/", "").replace(f"in {recorded};", "in .;")
            line = re.sub(r"tests/fixtures/\S+", "<fixture>", line)
            line = re.sub(r"^Added \w+ to", "Added <name> to", line)
            # The raised floor is the running release, which moves on every bump; the
            # guide's example names one release, so the number itself is not compared.
            line = re.sub(r"graftpunk>=\d+\.\d+\.\d+ \(", "graftpunk>=<release> (", line)
            return re.sub(r"'\w+'", "'<name>'", line)

        quoted = {
            generic(line)
            for _start, body in blocks(
                section(GUIDE_TEXT, "### Add a command to an existing plugin"), "text"
            )
            for line in body.splitlines()
        }
        _new(recorded, "myshop", "orders=GET /api/orders")
        pyproject = recorded / "pyproject.toml"
        pyproject.write_text(
            re.sub(r"graftpunk(\[\w+\])?>=[0-9.]+", r"graftpunk\1>=1.15.0", pyproject.read_text())
        )
        results = [
            _add(recorded, "myshop", "order=GET /api/orders/{order_id}"),
            _add(recorded, "myshop", "orders=GET /api/orders/{order_id}"),
            _add(recorded, "shop", "status=GET /api/orders/{order_id}"),
        ]
        printed = [
            generic(line)
            for result in results
            for line in _plain(result.output).strip().splitlines()
        ]
        kinds = ("Added ", "Next: ", "pyproject.toml: ", "already has a command", "No plugin has")
        for kind in kinds:
            assert any(kind in line for line in printed), (kind, printed)
        for line in printed:
            assert line in quoted, line

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

    def test_an_undecorated_helper_method_blocks_the_same_name(self, recorded: Path) -> None:
        module = _hand_written_project(recorded, module_text=_HAND_WRITTEN_WITH_HELPER)
        before = module.read_bytes()
        result = _add(recorded, "myshop", "orders=GET /api/orders")
        assert result.exit_code == 1
        assert "the plugin class already defines 'orders'" in _plain(result.output)
        assert module.read_bytes() == before

    def test_a_class_attribute_blocks_the_same_name(self, recorded: Path) -> None:
        module = _hand_written_project(recorded, module_text=_HAND_WRITTEN_WITH_ATTRIBUTE)
        before = module.read_bytes()
        result = _add(recorded, "myshop", "orders=GET /api/orders")
        assert result.exit_code == 1
        assert "the plugin class already defines 'orders'" in _plain(result.output)
        assert module.read_bytes() == before

    def test_a_command_group_blocks_its_registered_name(self, recorded: Path) -> None:
        module = _hand_written_project(recorded, module_text=_HAND_WRITTEN_WITH_GROUP)
        before = module.read_bytes()
        result = _add(recorded, "myshop", "admin=GET /api/orders")
        assert result.exit_code == 1
        assert "a command group already registers 'admin'" in _plain(result.output)
        assert module.read_bytes() == before

    def test_an_endpointless_command_collision_keeps_the_command_text(self, recorded: Path) -> None:
        """A hand-written @command with no endpoint= is still a command, not a
        group: it must be refused with the command text, not the group text."""
        module = _hand_written_project(
            recorded, module_text=_HAND_WRITTEN_WITH_ENDPOINTLESS_COMMAND
        )
        before = module.read_bytes()
        result = _add(recorded, "myshop", "orders=GET /api/orders")
        assert result.exit_code == 1
        assert "already has a command named 'orders'" in _plain(result.output)
        assert module.read_bytes() == before

    def test_a_pinned_name_collision_keeps_the_command_text(self, recorded: Path) -> None:
        module = _hand_written_project(recorded, module_text=_HAND_WRITTEN_WITH_PINNED_NAME)
        before = module.read_bytes()
        result = _add(recorded, "myshop", "order=GET /api/orders")
        assert result.exit_code == 1
        assert "already has a command named 'order'" in _plain(result.output)
        assert module.read_bytes() == before

    def test_the_default_example_command_collision_keeps_the_command_text(
        self, recorded: Path
    ) -> None:
        """gp plugin new with no --from-run writes an example command with no
        endpoint=; it must collide as a command, not a group."""
        result = runner.invoke(
            app,
            ["plugin", "new", "myshop", "--url", "https://myshop.example", "--dir", str(recorded)],
        )
        assert result.exit_code == 0, result.output
        module = recorded / "src" / "graftpunk_myshop" / "plugin.py"
        before = module.read_bytes()
        added = _add(recorded, "myshop", "example=GET /api/orders")
        assert added.exit_code == 1
        assert "already has a command named 'example'" in _plain(added.output)
        assert module.read_bytes() == before

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

    def test_a_name_shadowing_a_generated_import_is_refused(self, recorded: Path) -> None:
        """'command' is the @command decorator every generated module imports; a
        stub named after it would call itself instead."""
        _new(recorded, "myshop", "orders=GET /api/orders")
        module = recorded / "src" / "graftpunk_myshop" / "plugin.py"
        before = module.read_bytes()
        result = _add(recorded, "myshop", "command=GET /api/invoices")
        assert result.exit_code == 1
        assert "shadow" in _plain(result.output)
        assert module.read_bytes() == before

    def test_a_comment_on_the_import_to_merge_into_refuses_instead_of_dropping_it(
        self, recorded: Path
    ) -> None:
        """The stub needs CommandContext and command merged into the existing
        commented graftpunk.plugins import; re-rendering it would drop the
        noqa."""
        module = _hand_written_project(recorded, module_text=_HAND_WRITTEN_WITH_COMMENTED_IMPORT)
        before = module.read_bytes()
        result = _add(recorded, "myshop", "orders=GET /api/orders")
        assert result.exit_code == 1
        assert "holds a comment" in _plain(result.output)
        assert module.read_bytes() == before

    def test_a_module_level_helper_blocks_the_same_name(self, recorded: Path) -> None:
        """A top-level `def helper()` is not in the plugin class body, so
        class_names never sees it; the module-level binds_name check catches it
        instead."""
        module = _hand_written_project(recorded, module_text=_HAND_WRITTEN_WITH_MODULE_HELPER)
        before = module.read_bytes()
        result = _add(recorded, "myshop", "helper=GET /api/orders")
        assert result.exit_code == 1
        assert "already binds 'helper' at top level" in _plain(result.output)
        assert module.read_bytes() == before

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

    def test_a_hand_written_projects_differing_name_is_not_a_suite_member(
        self, recorded: Path
    ) -> None:
        """A one-entry-point project is never a suite, even when its
        [project].name differs from its package, which is exactly the case a
        hand-written project (the one enhance mode targets) is likely to be
        in."""
        (recorded / "pyproject.toml").write_text(
            '[project]\nname = "myshop-plugin"\n\n'
            '[project.entry-points."graftpunk.plugins"]\n'
            'myshop = "graftpunk_myshop.plugin:MyshopPlugin"\n\n'
            "[tool.ruff]\nline-length = 100\n\n"
            '[tool.ruff.lint]\nselect = ["E", "F", "I", "UP", "B"]\n'
        )
        package = recorded / "src" / "graftpunk_myshop"
        package.mkdir(parents=True)
        (package / "plugin.py").write_text(_HAND_WRITTEN)
        result = _add(recorded, "myshop", "orders=GET /api/orders")
        assert result.exit_code == 0, result.output
        output = _plain(result.output)
        assert "tests/fixtures/get_api_orders.json" in output
        assert "tests/fixtures/myshop/" not in output

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

    def test_a_dir_that_does_not_exist_is_refused_not_read_as_empty(self, recorded: Path) -> None:
        """The recording exists but the project directory itself does not."""
        missing = recorded / "nonexistent"
        result = _add(missing, "myshop", "orders=GET /api/orders")
        assert result.exit_code == 1
        assert _plain(result.output).strip() == f"{missing}: no such directory."

    def test_an_empty_run_directory_is_refused_naming_the_har_path(self, recorded: Path) -> None:
        """An interrupted recording: the run directory exists but network.har does
        not, so digest()'s FileNotFoundError must not reach the terminal as a
        traceback. The parser's own message already names the path once; the
        CLI must not name it again."""
        _new(recorded, "myshop", "orders=GET /api/orders")
        har = recorded.parent / "observe" / "myshop" / "run-1" / "network.har"
        har.unlink()
        with pytest.raises(FileNotFoundError) as caught:
            parse_har_file(har)
        result = _add(recorded, "myshop", "invoices=GET /api/invoices")
        assert result.exit_code == 1, result.output
        (line,) = _plain(result.output).strip().splitlines()
        assert line == f"Could not read the recording: {caught.value}"
        assert line.count(str(har)) == 1

    def test_a_non_utf8_har_is_refused_naming_the_har_path(self, recorded: Path) -> None:
        _new(recorded, "myshop", "orders=GET /api/orders")
        har = recorded.parent / "observe" / "myshop" / "run-1" / "network.har"
        har.write_bytes(b"\xff\xfe")
        with pytest.raises(HARParseError) as caught:
            parse_har_file(har)
        result = _add(recorded, "myshop", "invoices=GET /api/invoices")
        assert result.exit_code == 1, result.output
        (line,) = _plain(result.output).strip().splitlines()
        assert line == f"Could not read the recording: {caught.value}"
        assert line.count(str(har)) == 1

    def test_a_har_path_that_is_a_directory_is_refused_naming_it_once(self, recorded: Path) -> None:
        _new(recorded, "myshop", "orders=GET /api/orders")
        har = recorded.parent / "observe" / "myshop" / "run-1" / "network.har"
        har.unlink()
        har.mkdir()
        with pytest.raises(HARParseError) as caught:
            parse_har_file(har)
        result = _add(recorded, "myshop", "invoices=GET /api/invoices")
        assert result.exit_code == 1, result.output
        (line,) = _plain(result.output).strip().splitlines()
        assert line == f"Could not read the recording: {caught.value}"
        assert line.count(str(har)) == 1

    def test_an_invalid_json_har_is_refused_naming_the_har_path(self, recorded: Path) -> None:
        _new(recorded, "myshop", "orders=GET /api/orders")
        har = recorded.parent / "observe" / "myshop" / "run-1" / "network.har"
        har.write_text("not json")
        with pytest.raises(HARParseError) as caught:
            parse_har_file(har)
        result = _add(recorded, "myshop", "invoices=GET /api/invoices")
        assert result.exit_code == 1, result.output
        (line,) = _plain(result.output).strip().splitlines()
        assert line == f"Could not read the recording: {caught.value}"
        assert line.count(str(har)) == 1

    def test_a_har_with_the_wrong_schema_is_refused_naming_the_har_path(
        self, recorded: Path
    ) -> None:
        _new(recorded, "myshop", "orders=GET /api/orders")
        har = recorded.parent / "observe" / "myshop" / "run-1" / "network.har"
        har.write_text("{}")
        with pytest.raises(HARParseError) as caught:
            parse_har_file(har)
        result = _add(recorded, "myshop", "invoices=GET /api/invoices")
        assert result.exit_code == 1, result.output
        (line,) = _plain(result.output).strip().splitlines()
        assert line == f"Could not read the recording: {caught.value}"
        assert line.count(str(har)) == 1


def _record_api_run(project: Path) -> None:
    """A second recording, myshop-api, whose page is on myshop.example and whose JSON
    is on api.myshop.example, beside the one the recorded fixture writes."""
    run_dir = project.parent / "observe" / "myshop-api" / "run-1"
    run_dir.mkdir(parents=True)
    entries = [
        _entry(
            "GET",
            "https://myshop.example/account",
            content_type="text/html",
            body="<html><body>Account</body></html>",
        ),
        _entry("GET", "https://api.myshop.example/api/orders/1001", body='{"id": "1001"}'),
    ]
    (run_dir / "network.har").write_text(
        json.dumps({"log": {"version": "1.2", "entries": entries}})
    )


def _add_from(project: Path, session: str, command: str) -> Result:
    return runner.invoke(
        app,
        [
            "plugin",
            "add-command",
            "myshop",
            "--from-run",
            session,
            "--command",
            command,
            "--dir",
            str(project),
        ],
    )


@pytest.mark.usefixtures("gp_logging")
class TestAddCommandTargetsTheEndpointsHost:
    def test_an_endpoint_on_another_host_is_requested_absolutely(self, recorded: Path) -> None:
        _new(recorded, "myshop", "orders=GET /api/orders")
        _record_api_run(recorded)
        result = _add_from(recorded, "myshop-api", "order=GET /api/orders/{order_id}")
        assert result.exit_code == 0, result.output
        module = (recorded / "src" / "graftpunk_myshop" / "plugin.py").read_text()
        assert 'f"https://api.myshop.example/api/orders/{order_id}",' in module
        assert 'endpoint="GET /api/orders/{order_id}",' in module
        assert (
            "order calls api.myshop.example, not myshop.example; its request is an absolute URL"
            in _plain(result.output).splitlines()
        )
        _ruff_clean(recorded)

    def test_an_endpoint_on_the_base_host_prints_no_host_line(self, recorded: Path) -> None:
        _new(recorded, "myshop", "orders=GET /api/orders")
        result = _add(recorded, "myshop", "order=GET /api/orders/{order_id}")
        assert result.exit_code == 0, result.output
        module = (recorded / "src" / "graftpunk_myshop" / "plugin.py").read_text()
        assert 'f"/api/orders/{order_id}",' in module
        assert " calls " not in _plain(result.output)

    @pytest.mark.parametrize(
        "base_url_line",
        ['base_url = "https://" + "myshop.example"', 'base_url = "myshop.example"'],
    )
    def test_a_plugin_with_no_readable_base_url_gets_an_absolute_url(
        self, recorded: Path, base_url_line: str
    ) -> None:
        module = _hand_written_project(
            recorded,
            module_text=_HAND_WRITTEN.replace('base_url = "https://myshop.example"', base_url_line),
        )
        result = _add(recorded, "myshop", "orders=GET /api/orders")
        assert result.exit_code == 0, result.output
        assert '"https://myshop.example/api/orders",' in module.read_text()
        assert (
            "orders calls myshop.example; the plugin sets no base_url gp can read as a URL, "
            "so its request is an absolute URL"
        ) in " ".join(_plain(result.output).split())
        _ruff_clean(recorded)


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
    def test_a_dir_that_does_not_exist_is_refused_not_read_as_empty(self, tmp_path: Path) -> None:
        """A mistyped --dir must not be read as a fresh, empty project."""
        missing = tmp_path / "nonexistent"
        result = runner.invoke(app, ["plugin", "upgrade", "--dir", str(missing)])
        assert result.exit_code == 1
        assert _plain(result.output).strip() == f"{missing}: no such directory."

    def test_a_form_feed_in_the_conftest_does_not_raise_a_syntax_error(
        self, tmp_path: Path
    ) -> None:
        """As TestAddCommand's form-feed test, for the with_bindings path
        upgrade drives through with_import."""
        conftest = _project_lacking_the_wiring(tmp_path)
        conftest.write_text(
            "from graftpunk.testing.plugin import site_env_scrubber\n"
            "\n"
            "# note\x0c continued\n"
            "\n"
            'scrub_site_env = site_env_scrubber("MYSHOP_")\n'
        )
        result = runner.invoke(app, ["plugin", "upgrade", "--dir", str(tmp_path)])
        assert result.exit_code == 0, result.output
        text = conftest.read_text()
        assert "note\x0c continued" in text
        ast.parse(text)
        _ruff_clean(tmp_path)

    def test_a_conftest_importing_the_projects_own_package_stays_ruff_clean(
        self, tmp_path: Path
    ) -> None:
        """End to end: a conftest that already imports the project's own
        package used to get the new import sorted into the third-party section
        instead of a first-party one of its own."""
        from graftpunk.devtools.scaffold.project import write_scaffold
        from graftpunk.devtools.scaffold.render import ScaffoldSpec

        write_scaffold(
            tmp_path,
            ScaffoldSpec(
                name="myshop",
                mode="new_project",
                backend="nodriver",
                base_url="https://myshop.example",
            ),
        )
        conftest = tmp_path / "tests" / "conftest.py"
        conftest.write_text(
            "import pytest\n"
            "\n"
            "from graftpunk_myshop.plugin import MyshopPlugin\n"
            "\n"
            "\n"
            "@pytest.fixture()\n"
            "def plugin() -> MyshopPlugin:\n"
            "    return MyshopPlugin()\n"
        )
        result = runner.invoke(app, ["plugin", "upgrade", "--dir", str(tmp_path)])
        assert result.exit_code == 0, result.output
        check = subprocess.run(  # noqa: S603 - argv is fixed, not untrusted input
            [sys.executable, "-m", "ruff", "check", "--select", "I", "."],
            cwd=tmp_path,
            capture_output=True,
            text=True,
        )
        assert check.returncode == 0, check.stdout + check.stderr

    def test_a_comment_on_the_conftest_import_refuses_instead_of_dropping_it(
        self, tmp_path: Path
    ) -> None:
        """As TestAddCommand's equivalent test, for the with_bindings path
        upgrade drives."""
        conftest = _project_lacking_the_wiring(tmp_path)
        conftest.write_text(
            "from graftpunk.testing.plugin import site_env_scrubber  # noqa: F401\n"
            "\n"
            'scrub_site_env = site_env_scrubber("MYSHOP_")\n'
        )
        before = conftest.read_bytes()
        result = runner.invoke(app, ["plugin", "upgrade", "--dir", str(tmp_path)])
        assert result.exit_code == 1, result.output
        assert "holds a comment" in _plain(result.output)
        assert conftest.read_bytes() == before

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

    def test_prints_that_it_created_a_missing_fixtures_tree(self, tmp_path: Path) -> None:
        import shutil

        conftest = _project_lacking_the_wiring(tmp_path)
        shutil.rmtree(tmp_path / "tests" / "fixtures")
        result = runner.invoke(app, ["plugin", "upgrade", "--dir", str(tmp_path)])
        assert result.exit_code == 0, result.output
        assert "tests/conftest.py: added FIXTURES_TREE" in _plain(result.output)
        assert "tests/fixtures/: created" in _plain(result.output)
        assert (tmp_path / "tests" / "fixtures" / ".gitkeep").is_file()
        assert conftest.read_text() != ""


def _floor_pyproject(dependencies: str | None) -> str:
    """A hand-written plugin project's pyproject, its [project] dependencies
    holding *dependencies* as one TOML array item, or no dependencies key."""
    deps = "" if dependencies is None else f"dependencies = [\n    {dependencies},\n]\n"
    return (
        '[project]\nname = "graftpunk-myshop"\nversion = "0.1.0"\n'
        f"{deps}\n"
        '[project.entry-points."graftpunk.plugins"]\n'
        'myshop = "graftpunk_myshop.plugin:MyshopPlugin"\n\n'
        "[tool.ruff]\nline-length = 100\n"
    )


def _run_writer(command: str, project: Path, pyproject: str) -> tuple[object, list[str]]:
    """Run *command* (upgrade or add-command) on a project whose pyproject is
    *pyproject*, and return the result with the lines the command prints
    whatever the floor (so a test asserts the exact full output)."""
    if command == "upgrade":
        _project_lacking_the_wiring(project)
        (project / "pyproject.toml").write_text(pyproject)
        result = runner.invoke(app, ["plugin", "upgrade", "--dir", str(project)])
        own = [
            "tests/conftest.py: added FIXTURES_TREE",
            "tests/conftest.py: added sanitised_fixtures",
        ]
    else:
        module = _hand_written_project(project)
        (project / "pyproject.toml").write_text(pyproject)
        result = _add(project, "myshop", "orders=GET /api/orders")
        own = [
            f"Added orders to {module}",
            "Next: write its test against tests/fixtures/get_api_orders.json",
        ]
    return result, own


@pytest.mark.usefixtures("gp_logging")
@pytest.mark.parametrize("command", ["upgrade", "add-command"])
class TestGraftpunkFloor:
    """Both writers add code that needs the running graftpunk (the conftest's
    graftpunk.testing import, a stub's endpoint= keyword), so a write also
    makes the project declare it, or says how to."""

    @pytest.fixture(autouse=True)
    def _running_version(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import graftpunk

        monkeypatch.setattr(graftpunk, "__version__", "1.17.3")

    @pytest.mark.parametrize(
        ("dependency", "raised"),
        [
            ('"graftpunk>=1.0"', '"graftpunk>=1.17.0"'),
            (
                "'graftpunk[browser]>=1.0; python_version >= \"3.11\"'",
                "'graftpunk[browser]>=1.17.0; python_version >= \"3.11\"'",
            ),
        ],
    )
    def test_a_plain_lower_bound_is_raised_and_reported(
        self, command: str, recorded: Path, dependency: str, raised: str
    ) -> None:
        result, own = _run_writer(command, recorded, _floor_pyproject(dependency))
        assert result.exit_code == 0, result.output
        assert (recorded / "pyproject.toml").read_bytes() == _floor_pyproject(raised).encode()
        lines = _plain(result.output).strip().splitlines()
        assert lines == [
            *own,
            "pyproject.toml: graftpunk>=1.17.0 (was >=1.0); reinstall the project",
        ]

    def test_a_pinned_requirement_is_left_and_named_in_a_next_line(
        self, command: str, recorded: Path
    ) -> None:
        pyproject = _floor_pyproject('"graftpunk==1.0"')
        result, own = _run_writer(command, recorded, pyproject)
        assert result.exit_code == 0, result.output
        assert (recorded / "pyproject.toml").read_bytes() == pyproject.encode()
        lines = _plain(result.output).strip().splitlines()
        assert lines == [
            *own,
            "Next: raise graftpunk in pyproject.toml to >=1.17.0 and reinstall; "
            "it reads graftpunk==1.0",
        ]

    def test_no_graftpunk_dependency_is_named_in_a_next_line(
        self, command: str, recorded: Path
    ) -> None:
        pyproject = _floor_pyproject(None)
        result, own = _run_writer(command, recorded, pyproject)
        assert result.exit_code == 0, result.output
        assert (recorded / "pyproject.toml").read_bytes() == pyproject.encode()
        lines = _plain(result.output).strip().splitlines()
        assert lines == [
            *own,
            "Next: add graftpunk>=1.17.0 to pyproject.toml's [project] dependencies "
            "and reinstall; it has no graftpunk requirement",
        ]

    def test_a_project_already_at_the_floor_prints_nothing_extra(
        self, command: str, recorded: Path
    ) -> None:
        pyproject = _floor_pyproject('"graftpunk[browser]>=1.17.0"')
        result, own = _run_writer(command, recorded, pyproject)
        assert result.exit_code == 0, result.output
        assert (recorded / "pyproject.toml").read_bytes() == pyproject.encode()
        assert _plain(result.output).strip().splitlines() == own

    def test_dynamic_dependencies_are_named_in_their_own_next_line(
        self, command: str, recorded: Path
    ) -> None:
        pyproject = _floor_pyproject(None).replace(
            "[project]", '[project]\ndynamic = ["dependencies"]'
        )
        result, own = _run_writer(command, recorded, pyproject)
        assert result.exit_code == 0, result.output
        assert (recorded / "pyproject.toml").read_bytes() == pyproject.encode()
        lines = _plain(result.output).strip().splitlines()
        assert lines == [
            *own,
            "Next: this project's dependencies are dynamic; make sure whatever supplies "
            "them requires graftpunk>=1.17.0, then reinstall.",
        ]

    def test_a_capped_requirement_already_at_the_floor_prints_nothing_extra(
        self, command: str, recorded: Path
    ) -> None:
        """An upper bound does not by itself make the requirement unraisable: a
        requirement that already allows nothing below the floor gets nothing
        extra, capped or not."""
        pyproject = _floor_pyproject('"graftpunk[browser]>=1.17.0,<2"')
        result, own = _run_writer(command, recorded, pyproject)
        assert result.exit_code == 0, result.output
        assert (recorded / "pyproject.toml").read_bytes() == pyproject.encode()
        assert _plain(result.output).strip().splitlines() == own
