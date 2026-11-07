"""SitePlugin.aliases: more CLI names for one plugin, resolved in one place (#374)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import typer
from typer.testing import CliRunner

from graftpunk.exceptions import PluginError
from graftpunk.plugins.cli_plugin import CommandContext, SitePlugin, build_plugin_config, command
from graftpunk.plugins.names import CORE_COMMAND_NAMES, AliasConflict, plugin_names
from tests.unit.cli_harness import strip_ansi

DISCOVER_ALL = "graftpunk.cli.plugin_commands.discover_all_plugins"
runner = CliRunner()


class StorefrontPlugin(SitePlugin):
    site_name = "myshop-storefront"
    session_name = "myshop"
    help_text = "Commands for myshop"
    base_url = "https://myshop.example"
    aliases = ("shop",)

    @command(help="Show the account")
    def account(self, ctx: CommandContext) -> dict[str, str]:
        return {"plugin": "storefront", "slot": ctx._operating_session_name}


class OtherPlugin(SitePlugin):
    site_name = "othershop"
    help_text = "Commands for othershop"
    base_url = "https://othershop.example"
    requires_session = False

    @command(help="Show the account")
    def account(self, ctx: CommandContext) -> dict[str, str]:
        return {"plugin": "other"}


def _plugin(site_name: str, aliases: Any, *, session_name: str = "") -> SitePlugin:
    """A login-less plugin with one command, built at test time."""

    def account(self: Any, ctx: CommandContext) -> dict[str, str]:
        return {"plugin": site_name}

    attrs: dict[str, Any] = {
        "site_name": site_name,
        "session_name": session_name or site_name,
        "requires_session": False,
        "aliases": aliases,
        "account": command(help="Show the account")(account),
    }
    return type(f"Plugin_{site_name}", (SitePlugin,), attrs)()


@pytest.fixture
def registered(isolated_config: Path) -> Iterator[None]:
    """Leave the CLI's plugin registry empty after the test, whatever it registered."""
    from graftpunk.cli.plugin_commands import register_plugin_commands

    yield
    with patch(DISCOVER_ALL, return_value=()):
        register_plugin_commands(typer.Typer(), notify_errors=False)


def _register(*plugins: SitePlugin, notify_errors: bool = False) -> typer.Typer:
    from graftpunk.cli.plugin_commands import register_plugin_commands

    app = typer.Typer()

    @app.command("http")
    def http() -> None:
        """A core command."""

    with patch(DISCOVER_ALL, return_value=plugins):
        register_plugin_commands(app, notify_errors=notify_errors)
    return app


def _run(app: typer.Typer, *argv: str) -> Any:
    result = runner.invoke(app, [*argv, "--format", "json"], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


class TestResolver:
    def test_a_plugin_without_aliases_resolves_by_its_name_alone(self) -> None:
        names = plugin_names([OtherPlugin()])
        assert names.aliases == {"othershop": ()}
        assert names.conflicts == ()
        assert names.resolve("othershop").site_name == "othershop"
        assert names.resolve("shop") is None

    def test_an_alias_resolves_to_the_same_object(self) -> None:
        plugin = StorefrontPlugin()
        names = plugin_names([plugin])
        assert names.resolve("shop") is plugin
        assert names.resolve("myshop-storefront") is plugin

    def test_an_alias_naming_another_plugin_loses_to_that_name(self) -> None:
        other = OtherPlugin()
        greedy = _plugin("myshop", ("othershop", "ms"))
        for order in ([greedy, other], [other, greedy]):
            names = plugin_names(order)
            assert names.resolve("othershop") is other
            assert names.resolve("ms") is greedy
            assert names.conflicts == (
                AliasConflict("myshop", "othershop", "is the site_name of plugin 'othershop'"),
            )

    def test_an_alias_two_plugins_declare_resolves_to_neither(self) -> None:
        first = _plugin("myshop", ("shop",))
        second = _plugin("othershop", ("shop", "os"))
        names = plugin_names([first, second])
        assert names.resolve("shop") is None
        assert names.resolve("os") is second
        assert names.aliases == {"myshop": (), "othershop": ("os",)}
        assert {(c.site_name, c.alias) for c in names.conflicts} == {
            ("myshop", "shop"),
            ("othershop", "shop"),
        }

    def test_core_commands_duplicates_own_name_and_bad_names_are_dropped(self) -> None:
        plugin = _plugin("myshop", ("http", "shop", "shop", "myshop", "my shop", "-x"))
        names = plugin_names([plugin])
        assert names.aliases == {"myshop": ("shop",)}
        reasons = {c.alias: c.reason for c in names.conflicts}
        assert reasons["http"] == "is a core gp command"
        assert reasons["shop"] == "is listed twice"
        assert reasons["myshop"] == "is the plugin's own site_name"
        assert reasons["my shop"].startswith("is not a name the CLI can register")
        assert reasons["-x"].startswith("is not a name the CLI can register")

    def test_a_plugin_class_written_without_the_attribute_has_no_aliases(self) -> None:
        class Bare:
            site_name = "bare"

        names = plugin_names([Bare()])
        assert names.aliases == {"bare": ()}

    def test_the_core_names_match_the_ones_the_cli_reserves(self) -> None:
        import graftpunk.cli.main  # noqa: F401 - registers the CLI and takes the snapshot
        from graftpunk.cli.scaffold_commands import reserved_cli_names

        assert reserved_cli_names() == CORE_COMMAND_NAMES


class TestConfig:
    def test_a_list_becomes_a_tuple(self) -> None:
        config = build_plugin_config(site_name="myshop", aliases=["shop", "ms"])
        assert config.aliases == ("shop", "ms")

    def test_none_declared_is_an_empty_tuple(self) -> None:
        assert build_plugin_config(site_name="myshop").aliases == ()
        assert SitePlugin.aliases == ()

    @pytest.mark.parametrize("value", ["shop", ("shop", 3), {"shop": 1}])
    def test_anything_but_a_tuple_or_list_of_strings_is_refused(self, value: Any) -> None:
        with pytest.raises(PluginError, match="'aliases' must be a tuple"):
            build_plugin_config(site_name="myshop", aliases=value)

    def test_a_subclass_keeps_its_parents_aliases(self) -> None:
        class Child(StorefrontPlugin):
            site_name = "myshop-storefront"

        assert Child.aliases == ("shop",)


@pytest.mark.usefixtures("registered")
class TestCli:
    def test_the_alias_runs_the_same_plugin_on_the_same_session_slot(self) -> None:
        app = _register(StorefrontPlugin())
        with patch(
            "graftpunk.cli.plugin_runtime.load_session_for_api_resolved",
            side_effect=lambda name, **_kwargs: (MagicMock(), name),
        ):
            by_alias = _run(app, "shop", "account")
            by_name = _run(app, "myshop-storefront", "account")
        assert by_alias == by_name == {"plugin": "storefront", "slot": "myshop"}

    def test_help_lists_the_plugin_once_with_its_alias_beside_it(self) -> None:
        app = _register(StorefrontPlugin(), OtherPlugin())
        output = strip_ansi(runner.invoke(app, ["--help"], env={"COLUMNS": "200"}).output)
        commands = output.split("─ Commands ─")[1].splitlines()[1:]
        listed = [row.strip("│ ").split()[0] for row in commands if row.startswith("│")]
        assert listed == ["http", "myshop-storefront", "othershop"]
        assert "Commands for myshop (alias: shop)" in output

    def test_session_resolution_and_credentials_use_the_canonical_names(self) -> None:
        from graftpunk.cli.login_commands import credential_envvar
        from graftpunk.cli.plugin_commands import (
            get_plugin_for_session,
            is_registered_site_name,
            resolve_session_name,
        )

        _register(StorefrontPlugin())
        assert resolve_session_name("shop") == resolve_session_name("myshop-storefront")
        assert resolve_session_name("shop") == "myshop"
        assert is_registered_site_name("shop") is True
        plugin = get_plugin_for_session("myshop")
        assert plugin is not None
        assert credential_envvar(plugin, "username") == "MYSHOP_STOREFRONT_USERNAME"

    def test_another_plugins_name_wins_and_is_reported_once(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        app = _register(_plugin("myshop", ("othershop",)), OtherPlugin(), notify_errors=True)
        assert _run(app, "othershop", "account") == {"plugin": "other"}
        err = capsys.readouterr().err
        assert err.count("alias 'othershop'") == 1
        assert "is the site_name of plugin 'othershop'" in err

    def test_a_core_command_wins_over_an_alias(self, capsys: pytest.CaptureFixture[str]) -> None:
        from graftpunk.cli.plugin_commands import registered_aliases

        app = _register(_plugin("myshop", ("http", "shop")), notify_errors=True)
        assert registered_aliases() == {"myshop": ("shop",)}
        names = [g.name for g in app.registered_groups]
        assert names.count("http") == 0
        assert [c.name for c in app.registered_commands] == ["http"]
        err = capsys.readouterr().err
        assert err.count("alias 'http' is a core gp command") == 1

    def test_an_alias_two_plugins_declare_is_mounted_for_neither(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        app = _register(
            _plugin("myshop", ("shop",)), _plugin("othershop", ("shop",)), notify_errors=True
        )
        result = runner.invoke(app, ["shop", "account"])
        assert result.exit_code != 0
        assert "No such command 'shop'" in strip_ansi(result.output)
        assert capsys.readouterr().err.count("alias 'shop' is also an alias of") == 2

    def test_a_reload_does_not_collide_with_its_own_aliases(self) -> None:
        from graftpunk.cli.plugin_commands import register_plugin_commands, registered_aliases

        app = typer.Typer()
        for _ in range(2):
            with patch(DISCOVER_ALL, return_value=(StorefrontPlugin(),)):
                assert register_plugin_commands(app, notify_errors=False) == {
                    "myshop-storefront": "Commands for myshop"
                }
        assert registered_aliases() == {"myshop-storefront": ("shop",)}

    def test_gp_plugins_lists_the_plugin_once_with_its_aliases(self) -> None:
        from graftpunk.cli.main import app

        _register(StorefrontPlugin())
        with (
            patch("graftpunk.cli.main.discover_storage_backends", return_value={}),
            patch("graftpunk.cli.main.discover_keepalive_handlers", return_value={}),
            patch(
                "graftpunk.cli.main.discover_site_plugins",
                return_value={"myshop": StorefrontPlugin},
            ),
            patch("graftpunk.cli.main.create_yaml_plugins", return_value=([], [])),
        ):
            result = runner.invoke(app, ["plugins"], env={"COLUMNS": "200"})
        assert result.exit_code == 0, result.output
        output = strip_ansi(result.output)
        assert "(1 installed)" in output
        assert "myshop (alias: shop)" in output


@pytest.mark.usefixtures("registered")
class TestYamlPlugin:
    YAML = """\
site_name: myshop-storefront
session_name: myshop
base_url: https://myshop.example
requires_session: false
aliases: [shop, ms]
commands:
  account:
    help: Show the account
    url: /api/account
"""

    def test_a_yaml_plugin_declares_aliases_as_a_list(self, isolated_config: Path) -> None:
        from graftpunk.plugins.yaml_plugin import create_yaml_plugins

        (isolated_config / "plugins").mkdir()
        (isolated_config / "plugins" / "myshop.yaml").write_text(self.YAML)
        (plugin,), errors = create_yaml_plugins()
        assert errors == []
        assert plugin.aliases == ("shop", "ms")

        app = _register(plugin)
        response = MagicMock()
        response.json.return_value = {"id": 1}
        with patch("requests.Session.request", return_value=response):
            assert _run(app, "ms", "account") == {"id": 1}
        help_text = strip_ansi(runner.invoke(app, ["--help"], env={"COLUMNS": "200"}).output)
        assert "(aliases: shop, ms)" in help_text

    def test_a_yaml_string_is_refused(self, isolated_config: Path) -> None:
        from graftpunk.plugins.yaml_plugin import create_yaml_plugins

        (isolated_config / "plugins").mkdir()
        text = self.YAML.replace("aliases: [shop, ms]", "aliases: shop")
        (isolated_config / "plugins" / "myshop.yaml").write_text(text)
        plugins, errors = create_yaml_plugins()
        assert plugins == []
        assert "'aliases' must be a tuple" in errors[0].error


class TestClient:
    def test_the_client_resolves_an_alias_to_the_same_plugin(self) -> None:
        from graftpunk.client import GraftpunkClient

        plugin = StorefrontPlugin()
        with patch("graftpunk.plugins.discover_all_plugins", return_value=(plugin, OtherPlugin())):
            assert GraftpunkClient("shop")._plugin is plugin
            assert GraftpunkClient("shop")._session_name == "myshop"

    def test_the_client_refuses_an_alias_the_cli_ignores(self) -> None:
        from graftpunk.client import GraftpunkClient

        greedy = _plugin("myshop", ("othershop", "http"))
        other = OtherPlugin()
        with patch("graftpunk.plugins.discover_all_plugins", return_value=(greedy, other)):
            assert GraftpunkClient("othershop")._plugin is other
            with pytest.raises(PluginError, match="Plugin 'http' is unknown"):
                GraftpunkClient("http")


_PYPROJECT = """\
[project]
name = "graftpunk-myshop"

[project.entry-points."graftpunk.plugins"]
myshop = "graftpunk_myshop.plugin:MyshopPlugin"
othershop = "graftpunk_othershop.plugin:OthershopPlugin"
"""

_MODULE = """\
from graftpunk.plugins import SitePlugin


class {cls}(SitePlugin):
    site_name = "{site_name}"
    base_url = "https://myshop.example"
    aliases = {aliases}
"""


def _project(root: Path, mine: str, theirs: str = "()") -> Path:
    (root / "pyproject.toml").write_text(_PYPROJECT)
    for package, cls, site_name, aliases in (
        ("graftpunk_myshop", "MyshopPlugin", "myshop", mine),
        ("graftpunk_othershop", "OthershopPlugin", "othershop", theirs),
    ):
        (root / "src" / package).mkdir(parents=True)
        (root / "src" / package / "__init__.py").write_text("")
        module = _MODULE.format(cls=cls, site_name=site_name, aliases=aliases)
        (root / "src" / package / "plugin.py").write_text(module)
    return root


def _alias_findings(root: Path) -> list[str]:
    from graftpunk.devtools.plugin_check import check_project

    return [str(f) for f in check_project(root) if "alias" in f.message]


class TestCheck:
    def test_accepted_aliases_are_no_finding(self, tmp_path: Path) -> None:
        assert _alias_findings(_project(tmp_path, '("shop", "ms")', '["os"]')) == []

    def test_each_collision_is_a_finding_at_the_assignment(self, tmp_path: Path) -> None:
        findings = _alias_findings(
            _project(tmp_path, '("othershop", "http", "myshop", "os", "ms", "ms")', '("os",)')
        )
        mine = "src/graftpunk_myshop/plugin.py:7"
        theirs = "src/graftpunk_othershop/plugin.py:7"
        fix = "; rename or remove the alias."
        assert findings == [
            f"{mine}: alias 'othershop' is the site_name of plugin 'othershop'{fix}",
            f"{mine}: alias 'http' is a core gp command{fix}",
            f"{mine}: alias 'myshop' is the plugin's own site_name{fix}",
            f"{mine}: alias 'ms' is listed twice{fix}",
            f"{mine}: alias 'os' is also an alias of 'othershop'{fix}",
            f"{theirs}: alias 'os' is also an alias of 'myshop'{fix}",
        ]

    def test_an_alias_is_validated_as_a_plugin_name(self, tmp_path: Path) -> None:
        (finding,) = _alias_findings(_project(tmp_path, '("shop_",)'))
        assert finding.startswith("src/graftpunk_myshop/plugin.py:7: alias 'shop_': ")
        assert "must not end with a hyphen or underscore" in finding

    def test_aliases_that_are_not_a_literal_are_a_finding(self, tmp_path: Path) -> None:
        (finding,) = _alias_findings(_project(tmp_path, "tuple(['shop'])"))
        assert "aliases is not a literal tuple of strings" in finding

    def test_gp_plugin_check_prints_them(self, tmp_path: Path) -> None:
        from graftpunk.cli.main import app

        _project(tmp_path, '("http",)')
        result = runner.invoke(
            app, ["plugin", "check", "--dir", str(tmp_path)], env={"COLUMNS": "200"}
        )
        assert result.exit_code == 1
        assert "alias 'http' is a core gp command" in strip_ansi(result.output)


class TestInfo:
    def test_info_reports_aliases_as_a_list(self, tmp_path: Path) -> None:
        from graftpunk.devtools.plugin_info import info_payload
        from graftpunk.devtools.plugin_project import read_project

        _project(tmp_path, '("shop", "ms")')
        plugins = {p["site_name"]: p for p in info_payload(read_project(tmp_path))["plugins"]}
        assert plugins["myshop"]["aliases"] == ["shop", "ms"]
        assert plugins["othershop"]["aliases"] == []

    def test_info_reports_null_for_aliases_it_cannot_read(self, tmp_path: Path) -> None:
        from graftpunk.devtools.plugin_info import info_payload
        from graftpunk.devtools.plugin_project import read_project

        _project(tmp_path, "tuple(['shop'])")
        (mine, _) = info_payload(read_project(tmp_path))["plugins"]
        assert mine["aliases"] is None
