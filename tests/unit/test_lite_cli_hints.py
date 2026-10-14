"""Browser-dependent commands on graftpunk-lite stop with the install hint.

Each test hides the browser stack the way an install without ``[browser]``
lacks it: a ``None`` entry in ``sys.modules`` makes ``import nodriver`` raise
``ModuleNotFoundError``. graftpunk modules that import the stack at the top are
evicted too, so they are imported again and fail as they would on lite.
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
from importlib.metadata import PackageNotFoundError
from pathlib import Path

import pytest
import requests
import typer
from typer.testing import CliRunner

from graftpunk import _distribution
from graftpunk.cli.observe_commands import observe_app
from graftpunk.cli.session_commands import session_app
from tests.unit.cli_harness import invoke_plugin_app, strip_ansi
from tests.unit.test_cache import _setup_local_env

runner = CliRunner()

_REPO = Path(__file__).parent.parent.parent
_FIX = _REPO / "tests" / "fixtures"

# graftpunk modules that import the browser stack when they load.
_BROWSER_BOUND = ("graftpunk.session", "graftpunk.stealth", "graftpunk.cli.plugin_runtime")


def _as(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    def version(wanted: str) -> str:
        if wanted != name:
            raise PackageNotFoundError(wanted)
        return "1.20.0"

    monkeypatch.setattr(_distribution, "_pkg_version", version)


def _without_browser_stack(monkeypatch: pytest.MonkeyPatch) -> None:
    for module in _distribution.BROWSER_MODULES:
        monkeypatch.setitem(sys.modules, module, None)
    for module in _BROWSER_BOUND:
        monkeypatch.delitem(sys.modules, module, raising=False)


def _flat(text: str) -> str:
    """The output on one line: rich wraps a long message at the console width."""
    return " ".join(strip_ansi(text).split())


def _observe() -> typer.Typer:
    app = typer.Typer()
    app.add_typer(observe_app)
    return app


def _cache_fixture_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Cache the committed fixture as session ``myshop``. Its pickle names
    ``graftpunk.session.BrowserSession``, as every session gp login caches does.
    HOME and HTTPie's config dir point into *tmp_path*, so an export cannot
    write into the real home directory."""
    _setup_local_env(tmp_path, monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HTTPIE_CONFIG_DIR", str(tmp_path / "httpie"))
    # httpie reads HTTPIE_CONFIG_DIR once, when it is first imported.
    monkeypatch.setattr("httpie.context.Environment.config_dir", tmp_path / "httpie")
    (tmp_path / ".session_key").write_bytes((_FIX / "browserfree_session.key").read_bytes())
    session_dir = tmp_path / "sessions" / "myshop"
    session_dir.mkdir(parents=True)
    (session_dir / "session.pickle").write_bytes((_FIX / "browserfree_session.enc").read_bytes())
    (session_dir / "metadata.json").write_text('{"name": "myshop", "domain": "example.com"}')


_OBSERVE_COMMANDS = {
    "go": ["observe", "--no-session", "go", "https://example.com"],
    "go --interactive": ["observe", "--no-session", "go", "--interactive", "https://example.com"],
    "interactive": ["observe", "--no-session", "interactive", "https://example.com"],
}


@pytest.mark.parametrize("distribution", ["graftpunk", "graftpunk-lite"])
@pytest.mark.parametrize("command", sorted(_OBSERVE_COMMANDS))
def test_observe_without_the_browser_stack_prints_the_install_hint(
    monkeypatch: pytest.MonkeyPatch, command: str, distribution: str
) -> None:
    _as(monkeypatch, distribution)
    _without_browser_stack(monkeypatch)

    result = runner.invoke(_observe(), _OBSERVE_COMMANDS[command])

    output = _flat(result.output)
    assert result.exit_code == 1, output
    assert "needs the browser automation stack" in output, output
    assert "no module named 'nodriver'" in output, output
    assert _distribution.install_commands("browser") in output, output
    assert "Traceback" not in output, output
    assert not isinstance(result.exception, ImportError)


@pytest.mark.parametrize("distribution", ["graftpunk", "graftpunk-lite"])
def test_session_export_without_the_browser_stack_prints_the_install_hint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, distribution: str
) -> None:
    _cache_fixture_session(tmp_path, monkeypatch)
    _as(monkeypatch, distribution)
    _without_browser_stack(monkeypatch)

    result = runner.invoke(session_app, ["export", "myshop", "-o", str(tmp_path / "out.json")])

    output = _flat(result.output)
    assert result.exit_code == 1, output
    assert "needs the browser automation stack" in output, output
    assert _distribution.install_commands("browser") in output, output
    assert "Traceback" not in output, output
    assert not isinstance(result.exception, ImportError)
    assert not (tmp_path / "out.json").exists()
    assert not (tmp_path / "httpie").exists()


def _shop_plugin(backend: str):  # noqa: ANN202
    from graftpunk.plugins.cli_plugin import LoginConfig, LoginStep, SitePlugin, command

    class ShopPlugin(SitePlugin):
        site_name = "myshop"
        base_url = "https://example.com"
        session_name = "myshop"
        requires_session = True
        login_config = LoginConfig(
            url="/login", steps=[LoginStep(fields={"username": "#user"}, submit="#go")]
        )

        @command(help="List items")
        def items(self, ctx):  # noqa: ANN001, ANN202
            return {"ok": True}

    plugin = ShopPlugin()
    plugin.backend = backend
    return plugin


@pytest.fixture
def _no_ambient_session(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """No GRAFTPUNK_SESSION or .gp-session from the developer's shell or the repo."""
    monkeypatch.delenv("GRAFTPUNK_SESSION", raising=False)
    monkeypatch.chdir(tmp_path)


@pytest.mark.usefixtures("_no_ambient_session")
@pytest.mark.parametrize("backend", ["selenium", "nodriver"])
def test_plugin_login_without_the_browser_stack_prints_the_install_hint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, backend: str
) -> None:
    _setup_local_env(tmp_path, monkeypatch)
    _without_browser_stack(monkeypatch)

    result = invoke_plugin_app(_shop_plugin(backend), ["myshop", "login"], MYSHOP_USERNAME="u")

    output = _flat(result.output)
    assert result.exit_code == 1, output
    assert "myshop login needs the browser automation stack" in output, output
    assert _distribution.install_commands("browser") in output, output
    assert "Traceback" not in output, output


@pytest.mark.usefixtures("_no_ambient_session")
def test_plugin_command_with_a_cached_session_prints_the_install_hint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _cache_fixture_session(tmp_path, monkeypatch)
    _without_browser_stack(monkeypatch)

    result = invoke_plugin_app(_shop_plugin("nodriver"), ["myshop", "items"])

    output = _flat(result.output)
    assert result.exit_code == 1, output
    assert "myshop items needs the browser automation stack" in output, output
    assert _distribution.install_commands("browser") in output, output
    assert "Traceback" not in output, output


@pytest.mark.usefixtures("_no_ambient_session")
def test_http_with_a_cached_session_prints_the_install_hint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from graftpunk.cli.main import app

    _cache_fixture_session(tmp_path, monkeypatch)
    _without_browser_stack(monkeypatch)

    result = runner.invoke(app, ["http", "get", "--session", "myshop", "https://example.com"])

    output = _flat(result.output)
    assert result.exit_code == 1, output
    assert "gp http get with a cached session needs the browser automation stack" in output
    assert _distribution.install_commands("browser") in output, output
    assert "Traceback" not in output, output


def test_an_unrelated_missing_module_still_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    # The browser stack is present; a graftpunk module observe go needs is not.
    monkeypatch.setitem(sys.modules, "graftpunk.observe.capture", None)

    result = runner.invoke(_observe(), _OBSERVE_COMMANDS["go"])

    assert isinstance(result.exception, ModuleNotFoundError)
    assert result.exception.name == "graftpunk.observe.capture"
    assert "needs the browser automation stack" not in _flat(result.output)


def test_plugin_commands_load_without_the_browser_stack(monkeypatch: pytest.MonkeyPatch) -> None:
    """Registering any plugin imports plugin_runtime; on lite it must load."""
    _without_browser_stack(monkeypatch)

    runtime = importlib.import_module("graftpunk.cli.plugin_runtime")

    assert "graftpunk.session" not in sys.modules
    assert runtime._driver_of(requests.Session()) is None


@pytest.mark.parametrize(
    ("raised", "expected"),
    [
        (ModuleNotFoundError("No module named 'nodriver'", name="nodriver"), "nodriver"),
        (
            ModuleNotFoundError("No module named 'httpie.sessions'", name="httpie.sessions"),
            "httpie",
        ),
        (ModuleNotFoundError("No module named 'jmespath'", name="jmespath"), None),
        (ModuleNotFoundError("No module named 'graftpunk.x'", name="graftpunk.x"), None),
        (ImportError("cannot import name 'x'"), None),
    ],
)
def test_missing_browser_module_names_only_browser_modules(
    monkeypatch: pytest.MonkeyPatch, raised: ImportError, expected: str | None
) -> None:
    _without_browser_stack(monkeypatch)

    assert _distribution.missing_browser_module(raised) == expected


@pytest.mark.parametrize(
    "name", ["selenium.webdriver.nonexistent", "httpie.nonexistent", "nodriver"]
)
def test_a_module_missing_from_an_installed_browser_package_is_not_the_stack(name: str) -> None:
    """With the stack installed, a missing submodule is version skew: reinstalling
    the extra would not fix it, so its error must reach the user as it is."""
    assert _distribution.has_browser_stack()

    assert _distribution.missing_browser_module(ModuleNotFoundError(name=name)) is None


def test_missing_browser_module_follows_the_cause(monkeypatch: pytest.MonkeyPatch) -> None:
    """load_session re-raises the unpickler's failure under its own ImportError."""
    _without_browser_stack(monkeypatch)
    try:
        try:
            raise ModuleNotFoundError("No module named 'httpie'", name="httpie")
        except ModuleNotFoundError as inner:
            raise ImportError("Loading the cached session requires the browser stack") from inner
    except ImportError as outer:
        assert _distribution.missing_browser_module(outer) == "httpie"


def test_a_missing_submodule_of_an_installed_browser_package_propagates() -> None:
    """Version skew on the full install: the real error and its traceback, not the hint."""
    from graftpunk.cli.errors import needs_browser_stack

    app = typer.Typer()

    @app.command()
    @needs_browser_stack
    def go(ctx: typer.Context) -> None:
        import selenium.webdriver.no_such_submodule  # noqa: F401

    result = runner.invoke(app, [])

    assert isinstance(result.exception, ModuleNotFoundError)
    assert result.exception.name == "selenium.webdriver.no_such_submodule"
    assert "needs the browser automation stack" not in _flat(result.output)


# Neither is the browser stack missing: jmespath is not a browser module, and a
# missing submodule of an installed selenium is version skew.
_NOT_THE_STACK = ["jmespath", "selenium.webdriver.no_such_submodule"]


@pytest.mark.usefixtures("_no_ambient_session")
@pytest.mark.parametrize("module", _NOT_THE_STACK)
def test_plugin_login_keeps_other_import_errors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, module: str
) -> None:
    from graftpunk.plugins.cli_plugin import SitePlugin

    class HandWritten(SitePlugin):
        site_name = "myshop"
        base_url = "https://example.com"
        session_name = "myshop"

        def login(self, credentials: dict[str, str]) -> bool:
            raise ModuleNotFoundError(f"No module named {module!r}", name=module)

    _setup_local_env(tmp_path, monkeypatch)

    result = invoke_plugin_app(
        HandWritten(),
        ["myshop", "login"],
        MYSHOP_USERNAME="u",
        MYSHOP_PASSWORD="p",  # noqa: S106
    )

    output = _flat(result.output)
    assert result.exit_code == 1, output
    assert f"Login failed: No module named '{module}'" in output, output
    assert "needs the browser automation stack" not in output, output


@pytest.mark.usefixtures("_no_ambient_session")
@pytest.mark.parametrize("module", _NOT_THE_STACK)
def test_plugin_session_load_keeps_other_import_errors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, module: str
) -> None:
    # Not "import ... as": other tests here re-import plugin_runtime, which
    # leaves graftpunk.cli's attribute on a copy registration does not use.
    runtime = importlib.import_module("graftpunk.cli.plugin_runtime")

    def load(*_args: object, **_kwargs: object) -> None:
        raise ModuleNotFoundError(f"No module named {module!r}", name=module)

    _cache_fixture_session(tmp_path, monkeypatch)
    monkeypatch.setattr(runtime, "load_session_for_api_resolved", load)

    result = invoke_plugin_app(_shop_plugin("nodriver"), ["myshop", "items"])

    output = _flat(result.output)
    assert result.exit_code == 1, output
    assert f"Failed to load session: No module named '{module}'" in output, output
    assert "needs the browser automation stack" not in output, output


@pytest.mark.usefixtures("_no_ambient_session")
@pytest.mark.parametrize("module", _NOT_THE_STACK)
def test_http_session_load_keeps_other_import_errors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, module: str
) -> None:
    import graftpunk.cli.http_commands as http_commands
    from graftpunk.cli.main import app

    def load(*_args: object, **_kwargs: object) -> None:
        raise ModuleNotFoundError(f"No module named {module!r}", name=module)

    _cache_fixture_session(tmp_path, monkeypatch)
    monkeypatch.setattr(http_commands, "load_session_for_api_resolved", load)

    result = runner.invoke(app, ["http", "get", "--session", "myshop", "https://example.com"])

    output = _flat(result.output)
    assert result.exit_code == 1, output
    assert f"Failed to load session 'myshop': No module named '{module}'" in output, output
    assert "needs the browser automation stack" not in output, output


@pytest.mark.usefixtures("_no_ambient_session")
def test_plugin_login_without_the_browser_stack_asks_for_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The hint comes before any prompt or workstation env lookup."""
    from graftpunk import workstation_env

    def no_lookup() -> None:
        raise AssertionError("the workstation env file was read")

    _setup_local_env(tmp_path, monkeypatch)
    _without_browser_stack(monkeypatch)
    monkeypatch.setattr(workstation_env, "load", no_lookup)
    monkeypatch.delenv("MYSHOP_USERNAME", raising=False)

    result = invoke_plugin_app(_shop_plugin("selenium"), ["myshop", "login"])

    output = _flat(result.output)
    assert result.exit_code == 1, output
    assert "myshop login needs the browser automation stack" in output, output
    assert "no module named 'selenium'" in output, output
    assert "Username" not in output, output


def test_session_export_writes_where_output_says(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _cache_fixture_session(tmp_path, monkeypatch)
    target = tmp_path / "out" / "myshop-httpie.json"

    result = runner.invoke(session_app, ["export", "myshop", "-o", str(target)])

    output = _flat(result.output)
    assert result.exit_code == 0, output
    assert f"Exported to: {target}" in output, output
    assert f"http --session={target}" in output, output
    assert "cookies" in json.loads(target.read_text())
    assert not (tmp_path / "httpie" / "sessions").exists()


def test_session_export_defaults_to_the_httpie_sessions_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _cache_fixture_session(tmp_path, monkeypatch)
    default = tmp_path / "httpie" / "sessions" / "myshop.json"

    result = runner.invoke(session_app, ["export", "myshop"])

    output = _flat(result.output)
    assert result.exit_code == 0, output
    assert f"Exported to: {default}" in output, output
    assert "http --session=myshop " in output, output
    assert "cookies" in json.loads(default.read_text())


_CAPTURED_STDOUT_RUN = """
import sys

from tests.unit.cli_harness import invoke_plugin_app
from tests.unit.test_lite_cli_hints import _shop_plugin

assert "graftpunk.session" not in sys.modules, "graftpunk.session loaded before registration"
result = invoke_plugin_app(_shop_plugin("nodriver"), ["myshop", "items"])
print("exit", result.exit_code)
print(result.output)
"""


def test_a_plugin_command_runs_under_captured_stdout_on_the_full_install(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A plugin author's CliRunner test, loading a cached session, exits 0.

    Loading the session needs graftpunk.session, whose httpie import calls
    sys.stdout.fileno(), which CliRunner's captured stdout does not have. Run
    in a fresh interpreter, so registration makes the first import, as in a
    plugin author's test process.
    """
    _cache_fixture_session(tmp_path, monkeypatch)
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    monkeypatch.delenv("GRAFTPUNK_SESSION", raising=False)
    monkeypatch.setenv("NO_COLOR", "1")

    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", _CAPTURED_STDOUT_RUN],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(_REPO)},
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "exit 0" in result.stdout, result.stdout
    assert '"ok": true' in result.stdout, result.stdout
