"""SitePlugin.aliases: more CLI names for one plugin, resolved in one place (#374)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import requests
import typer
from typer.testing import CliRunner

from graftpunk.exceptions import PluginError
from graftpunk.plugins.cli_plugin import (
    CommandContext,
    CommandSpec,
    SitePlugin,
    build_plugin_config,
    command,
)
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


def _plugin(site_name: str, aliases: Any, *, session_name: str = "", **extra: Any) -> SitePlugin:
    """A login-less plugin with one command, built at test time; *extra* adds or
    replaces class attributes."""

    def account(self: Any, ctx: CommandContext) -> dict[str, str]:
        return {"plugin": site_name}

    attrs: dict[str, Any] = {
        "site_name": site_name,
        "session_name": session_name or site_name,
        "requires_session": False,
        "aliases": aliases,
        "account": command(help="Show the account")(account),
        **extra,
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

    def test_an_alias_naming_another_plugins_session_name_is_dropped(self) -> None:
        storefront = _plugin("myshop-storefront", (), session_name="myshop")
        other = _plugin("othershop", ("myshop",))
        for order in ([storefront, other], [other, storefront]):
            names = plugin_names(order)
            assert names.resolve("myshop") is None
            assert names.conflicts == (
                AliasConflict(
                    "othershop", "myshop", "is the session name of plugin 'myshop-storefront'"
                ),
            )

    def test_an_alias_may_be_the_plugins_own_session_name(self) -> None:
        plugin = _plugin("myshop-storefront", ("myshop",), session_name="myshop")
        names = plugin_names([plugin])
        assert names.conflicts == ()
        assert names.resolve("myshop") is plugin

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

    @pytest.mark.parametrize("value", ["shop", ("shop", 3), {"shop": 1}, 3])
    def test_a_refused_value_leaves_no_aliases_and_keeps_the_message(self, value: Any) -> None:
        config = build_plugin_config(site_name="myshop", aliases=value)
        assert config.aliases == ()
        assert config.aliases_error.startswith("Plugin 'myshop': 'aliases' must be a tuple")

    def test_a_command_named_aliases_stays_a_command(self) -> None:
        class Mailbox(SitePlugin):
            site_name = "mailbox"
            requires_session = False

            @command(help="List mail aliases")
            def aliases(self, ctx: CommandContext) -> dict[str, list[str]]:
                return {"aliases": ["info@myshop.example"]}

        assert callable(Mailbox.__dict__["aliases"])
        assert plugin_names([Mailbox()]).aliases == {"mailbox": ()}
        assert plugin_names([Mailbox()]).conflicts == ()
        assert [c.name for c in Mailbox().get_commands()] == ["aliases"]

    def test_a_plugins_own_dict_named_aliases_is_left_as_it_was(self) -> None:
        class Catalog(SitePlugin):
            site_name = "myshop"
            requires_session = False
            aliases = {"sku": "item_id"}

            @command(help="Map a column")
            def column(self, ctx: CommandContext, name: str) -> dict[str, str]:
                return {"column": self.aliases.get(name, name)}

        assert Catalog.aliases == {"sku": "item_id"}
        handler = Catalog().get_commands()[0].handler
        assert handler(MagicMock(), name="sku") == {"column": "item_id"}
        names = plugin_names([Catalog()])
        assert names.aliases == {"myshop": ()}
        assert len(names.conflicts) == 1

    def test_a_cached_property_named_aliases_is_left_as_it_was(self) -> None:
        import functools

        class Catalog(SitePlugin):
            site_name = "myshop"

            @functools.cached_property
            def aliases(self) -> dict[str, str]:
                return {"sku": "item_id"}

        assert isinstance(Catalog.__dict__["aliases"], functools.cached_property)
        assert Catalog().aliases == {"sku": "item_id"}
        assert plugin_names([Catalog()]).aliases == {"myshop": ()}

    def test_a_custom_descriptor_named_aliases_is_left_as_it_was(self) -> None:
        class Mapping:
            def __get__(self, instance: Any, owner: Any = None) -> dict[str, str]:
                return {"sku": "item_id"}

        descriptor = Mapping()

        class Catalog(SitePlugin):
            site_name = "myshop"
            aliases = descriptor

        assert Catalog.__dict__["aliases"] is descriptor
        assert Catalog().aliases == {"sku": "item_id"}
        assert plugin_names([Catalog()]).aliases == {"myshop": ()}

    def test_an_inherited_command_named_aliases_stays_a_command(self) -> None:
        class Mailbox(SitePlugin):
            site_name = "mailbox"
            requires_session = False

            @command(help="List mail aliases")
            def aliases(self, ctx: CommandContext) -> dict[str, list[str]]:
                return {"aliases": []}

        class Staging(Mailbox):
            site_name = "mailbox-staging"

        assert callable(Staging.aliases)
        assert plugin_names([Staging()]).aliases == {"mailbox-staging": ()}

    def test_a_subclass_does_not_inherit_aliases(self) -> None:
        class Staging(StorefrontPlugin):
            site_name = "myshop-staging"
            session_name = "myshop-staging"

        assert Staging.aliases == ()
        assert StorefrontPlugin.aliases == ("shop",)
        names = plugin_names([StorefrontPlugin(), Staging()])
        assert names.conflicts == ()
        assert names.aliases == {"myshop-storefront": ("shop",), "myshop-staging": ()}

    def test_a_subclass_that_declares_aliases_has_its_own(self) -> None:
        class Staging(StorefrontPlugin):
            site_name = "myshop-staging"
            aliases = ("stage",)

        assert Staging.aliases == ("stage",)


def _discovered(plugin_class: type) -> tuple[tuple[Any, ...], list[dict[str, Any]]]:
    """What discover_all_plugins keeps of one entry-point plugin class, and its log."""
    from structlog.testing import capture_logs

    from graftpunk.plugins import PythonDiscoveryResult, discover_all_plugins

    discover_all_plugins.cache_clear()
    try:
        with (
            patch("graftpunk.plugins.discover_site_plugins", return_value={"x": plugin_class}),
            patch("graftpunk.plugins.create_yaml_plugins", return_value=([], [])),
            patch(
                "graftpunk.plugins.discover_python_plugins", return_value=PythonDiscoveryResult()
            ),
            capture_logs() as logs,
        ):
            kept = discover_all_plugins()
    finally:
        discover_all_plugins.cache_clear()
    return kept, logs


REFUSED_VALUES = [{"sku": "item_id"}, "shop", 3]
REFUSED_YAML = ["{sku: item_id}", "shop", "3"]


def _message(value: Any) -> str:
    return (
        f"Plugin 'myshop': 'aliases' must be a tuple (or a YAML list) of strings, "
        f"got {value!r}. One alias is written ('shop',). The plugin loads without aliases."
    )


class _Proto:
    """A plugin written against CLIPluginProtocol, not a SitePlugin."""

    site_name = "myshop"
    session_name = "myshop"
    help_text = "Commands for myshop"
    api_version = 1
    requires_session = False
    backend = "selenium"
    base_url = ""
    login_config = None
    token_config = None
    username_envvar = ""
    password_envvar = ""

    def get_commands(self) -> list[CommandSpec]:
        return [CommandSpec(name="account", handler=lambda ctx: {"plugin": "myshop"})]

    def get_session(self, session_name: str | None = None) -> Any:
        raise AssertionError("not reached: requires_session is False")

    def setup(self) -> None:
        pass

    def teardown(self) -> None:
        pass


def _refused_plugin(kind: str, index: int, config_dir: Path) -> Any:
    """A plugin named myshop of *kind* whose aliases is REFUSED_VALUES[index]."""
    if kind == "site":
        return _plugin("myshop", REFUSED_VALUES[index])
    if kind == "protocol":
        return type("Proto", (_Proto,), {"aliases": REFUSED_VALUES[index]})()
    from graftpunk.plugins.yaml_plugin import create_yaml_plugins

    (config_dir / "plugins").mkdir()
    (config_dir / "plugins" / "myshop.yaml").write_text(
        f"site_name: myshop\nrequires_session: false\naliases: {REFUSED_YAML[index]}\n"
        "commands:\n  account:\n    url: https://myshop.example/api/account\n"
    )
    (plugin,), errors = create_yaml_plugins()
    assert errors == []
    return plugin


@pytest.mark.usefixtures("registered")
@pytest.mark.parametrize("kind", ["site", "protocol", "yaml"])
@pytest.mark.parametrize("index", range(len(REFUSED_VALUES)))
class TestARefusedValueKeepsThePlugin:
    """An aliases value that is not a list of names never costs a plugin its load:
    it loads as on 1.41.0, with no aliases and its own names held, and one warning
    names the plugin and the fix, in the same words for every kind of plugin."""

    def test_it_registers_and_runs_with_one_warning(
        self, kind: str, index: int, isolated_config: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from graftpunk.cli.plugin_commands import registered_aliases

        plugin = _refused_plugin(kind, index, isolated_config)
        response = MagicMock()
        response.json.return_value = {"plugin": "myshop"}
        app = _register(plugin, notify_errors=True)
        assert registered_aliases() == {"myshop": ()}
        with patch("requests.Session.request", return_value=response):
            assert _run(app, "myshop", "account") == {"plugin": "myshop"}
        err = " ".join(capsys.readouterr().err.split())
        assert err.count(_message(REFUSED_VALUES[index])) == 1

    def test_discovery_and_the_client_keep_it(
        self, kind: str, index: int, isolated_config: Path
    ) -> None:
        from graftpunk.client import GraftpunkClient
        from graftpunk.plugins import get_plugin

        plugin = _refused_plugin(kind, index, isolated_config)
        if kind != "yaml":
            kept, _logs = _discovered(type(plugin))
            assert [p.site_name for p in kept] == ["myshop"]
        with patch("graftpunk.plugins.discover_all_plugins", return_value=(plugin,)):
            assert get_plugin("myshop") is plugin
            assert GraftpunkClient("myshop")._plugin is plugin

    def test_its_names_still_hold_against_another_plugins_alias(
        self, kind: str, index: int, isolated_config: Path
    ) -> None:
        plugin = _refused_plugin(kind, index, isolated_config)
        greedy = _plugin("othershop", ("myshop",))
        names = plugin_names([plugin, greedy])
        assert names.resolve("myshop") is plugin
        assert names.aliases["othershop"] == ()

    def test_gp_plugins_lists_it(self, kind: str, index: int, isolated_config: Path) -> None:
        from graftpunk.cli.main import app

        plugin = _refused_plugin(kind, index, isolated_config)
        _register(plugin)
        site = {} if kind == "yaml" else {"myshop": type(plugin)}
        with (
            patch("graftpunk.cli.main.discover_storage_backends", return_value={}),
            patch("graftpunk.cli.main.discover_keepalive_handlers", return_value={}),
            patch("graftpunk.cli.main.discover_site_plugins", return_value=site),
            patch(
                "graftpunk.cli.main.create_yaml_plugins",
                return_value=([plugin] if kind == "yaml" else [], []),
            ),
        ):
            result = runner.invoke(app, ["plugins"], env={"COLUMNS": "200"})
        assert result.exit_code == 0, result.output
        output = strip_ansi(result.output)
        assert "(1 installed)" in output
        assert "✓ myshop\n" in output or "✓ myshop " in output


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

    def test_each_conflict_is_one_line_on_stderr(
        self, captured_logs: list[dict[str, Any]], capsys: pytest.CaptureFixture[str]
    ) -> None:
        _register(
            _plugin("myshop", {"sku": "item_id"}),
            _plugin("othershop", ("http",)),
            notify_errors=True,
        )
        err = capsys.readouterr().err
        assert err.count("Plugin 'myshop'") == 1
        assert err.count("alias 'http'") == 1
        # The printed warning is the one report: the log line is debug, which the
        # CLI's WARNING level keeps off stderr.
        logged = [e for e in captured_logs if e["event"] == "plugin_alias_ignored"]
        assert [e["log_level"] for e in logged] == ["debug", "debug"]
        captured_logs.clear()
        # Without the printed warning (an embedding host), the log line is the one
        # report, and a refused value carries no empty alias field.
        _register(_plugin("myshop", {"sku": "item_id"}), notify_errors=False)
        assert capsys.readouterr().err == ""
        (logged,) = [e for e in captured_logs if e["event"] == "plugin_alias_ignored"]
        assert logged["log_level"] == "warning"
        assert "alias" not in logged

    def test_a_command_named_aliases_is_listed_and_runs(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        class Mailbox(SitePlugin):
            site_name = "mailbox"
            requires_session = False

            @command(help="List mail aliases")
            def aliases(self, ctx: CommandContext) -> dict[str, list[str]]:
                return {"aliases": ["info@myshop.example"]}

        app = _register(Mailbox(), notify_errors=True)
        assert capsys.readouterr().err == ""
        help_text = strip_ansi(
            runner.invoke(app, ["mailbox", "--help"], env={"COLUMNS": "200"}).output
        )
        assert "List mail aliases" in help_text
        assert _run(app, "mailbox", "aliases") == {"aliases": ["info@myshop.example"]}

    def test_a_plugin_that_fails_setup_still_holds_its_name(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from graftpunk.cli.plugin_commands import registered_aliases
        from graftpunk.plugins import get_plugin

        def setup(self: Any) -> None:
            raise RuntimeError("needs a config file")

        broken = _plugin("othershop", (), setup=setup)
        greedy = _plugin("myshop", ("othershop",))
        app = _register(broken, greedy, notify_errors=True)
        assert registered_aliases() == {"myshop": ()}
        result = runner.invoke(app, ["othershop", "account"])
        assert "No such command 'othershop'" in strip_ansi(result.output)
        assert "alias 'othershop' is the site_name of plugin 'othershop'" in capsys.readouterr().err
        with patch("graftpunk.plugins.discover_all_plugins", return_value=(broken, greedy)):
            assert get_plugin("othershop") is broken

    def test_a_subclass_with_its_own_site_name_registers_without_a_warning(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from graftpunk.cli.plugin_commands import registered_aliases

        class Staging(StorefrontPlugin):
            site_name = "myshop-staging"
            session_name = "myshop-staging"

        app = _register(StorefrontPlugin(), Staging(), notify_errors=True)
        assert capsys.readouterr().err == ""
        assert registered_aliases() == {"myshop-storefront": ("shop",), "myshop-staging": ()}
        with patch(
            "graftpunk.cli.plugin_runtime.load_session_for_api_resolved",
            side_effect=lambda name, **_kwargs: (MagicMock(), name),
        ):
            assert _run(app, "shop", "account")["slot"] == "myshop"
            assert _run(app, "myshop-staging", "account")["slot"] == "myshop-staging"

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


def _surface_app(*plugins: SitePlugin) -> typer.Typer:
    """The core session and http commands, with *plugins* registered beside them."""
    from graftpunk.cli.http_commands import http_app
    from graftpunk.cli.plugin_commands import register_plugin_commands
    from graftpunk.cli.session_commands import session_app

    app = typer.Typer()
    app.add_typer(session_app)
    app.add_typer(http_app)
    with patch(DISCOVER_ALL, return_value=plugins):
        register_plugin_commands(app, notify_errors=False)
    return app


@pytest.mark.usefixtures("registered")
class TestSessionSurfaces:
    """`gp session` and `gp http` take an alias for its plugin's session base."""

    SLOTS = ["myshop", "othershop"]

    def _clear(self, app: typer.Typer, name: str) -> list[str]:
        """Run `gp session clear <name> --force`; the slots it removed."""
        cleared: list[str] = []

        def clear(slot: str, **_kwargs: Any) -> bool:
            cleared.append(slot)
            return True

        metadata = [{"name": slot, "domain": "myshop.example"} for slot in self.SLOTS]
        with (
            patch("graftpunk.cli.plugin_commands.list_sessions", return_value=self.SLOTS),
            patch(
                "graftpunk.cli.session_commands.list_sessions_with_metadata",
                return_value=metadata,
            ),
            patch("graftpunk.cli.session_commands.clear_session_cache", side_effect=clear),
        ):
            result = runner.invoke(app, ["session", "clear", name, "--force"])
        assert result.exit_code == 0, result.output
        return cleared

    def test_session_show_by_alias_shows_the_plugins_session(self) -> None:
        app = _surface_app(StorefrontPlugin())
        with (
            patch("graftpunk.cli.plugin_commands.list_sessions", return_value=self.SLOTS),
            patch(
                "graftpunk.cli.session_commands.get_session_metadata",
                side_effect=lambda name, **_kwargs: {"name": name, "status": "active"},
            ),
        ):
            result = runner.invoke(app, ["session", "show", "shop", "--json"])
        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout)["name"] == "myshop"

    def test_session_clear_by_alias_clears_the_plugins_session(self) -> None:
        app = _surface_app(StorefrontPlugin())
        assert self._clear(app, "shop") == ["myshop"]

    def test_another_plugins_session_name_stays_that_plugins(self) -> None:
        storefront = _plugin("myshop-storefront", (), session_name="myshop")
        other = _plugin("othershop", ("myshop",))
        app = _surface_app(storefront, other)
        assert self._clear(app, "myshop") == ["myshop"]

    def test_gp_http_by_alias_loads_the_plugins_session(self) -> None:
        app = _surface_app(StorefrontPlugin())

        def load(name: str, **_kwargs: Any) -> tuple[Any, str]:
            response = MagicMock(spec=requests.Response)
            response.status_code = 200
            response.text = f"loaded {name}"
            session = MagicMock(spec=requests.Session)
            session.headers = {}
            session.request.return_value = response
            return session, name

        with (
            patch("graftpunk.cli.plugin_commands.list_sessions", return_value=self.SLOTS),
            patch("graftpunk.cli.http_commands.load_session_for_api_resolved", side_effect=load),
        ):
            result = runner.invoke(
                app,
                [
                    "http",
                    "get",
                    "https://myshop.example/api/account",
                    "--session",
                    "shop",
                    "--no-observe",
                    "--body-only",
                ],
            )
        assert result.exit_code == 0, result.output
        assert result.stdout == "loaded myshop"


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


class TestClient:
    def test_a_conflict_is_logged_once_per_process(self) -> None:
        from structlog.testing import capture_logs

        import graftpunk.plugins as plugins_module
        from graftpunk.plugins import get_plugin

        plugin = _plugin("myshop", {"sku": "item_id"})
        plugins_module._reported_conflicts.clear()
        try:
            with (
                patch("graftpunk.plugins.discover_all_plugins", return_value=(plugin,)),
                capture_logs() as logs,
            ):
                for _ in range(3):
                    assert get_plugin("myshop") is plugin
        finally:
            plugins_module._reported_conflicts.clear()
        warned = [e for e in logs if e["event"] == "plugin_alias_ignored"]
        assert len(warned) == 1
        assert "alias" not in warned[0]

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

    def test_a_plugin_with_non_literal_aliases_still_holds_its_names(self, tmp_path: Path) -> None:
        findings = _alias_findings(_project(tmp_path, "tuple(['x'])", '("myshop",)'))
        assert findings[1:] == [
            "src/graftpunk_othershop/plugin.py:7: alias 'myshop' is the site_name of plugin "
            "'myshop'; rename or remove the alias."
        ]

    def test_an_alias_naming_another_plugins_session_name_is_a_finding(
        self, tmp_path: Path
    ) -> None:
        _project(tmp_path, "()", '("myshop-slot",)')
        module = tmp_path / "src" / "graftpunk_myshop" / "plugin.py"
        module.write_text(
            module.read_text().replace(
                'site_name = "myshop"\n', 'site_name = "myshop"\n    session_name = "myshop-slot"\n'
            )
        )
        assert _alias_findings(tmp_path) == [
            "src/graftpunk_othershop/plugin.py:7: alias 'myshop-slot' is the session name of "
            "plugin 'myshop'; rename or remove the alias."
        ]

    def test_an_annotation_without_a_value_is_no_aliases(self, tmp_path: Path) -> None:
        from graftpunk.devtools.plugin_info import info_payload
        from graftpunk.devtools.plugin_project import read_project

        _project(tmp_path, "()")
        module = tmp_path / "src" / "graftpunk_myshop" / "plugin.py"
        module.write_text(module.read_text().replace("aliases = ()", "aliases: tuple[str, ...]"))
        assert _alias_findings(tmp_path) == []
        (mine, _) = info_payload(read_project(tmp_path))["plugins"]
        assert mine["aliases"] == []

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
