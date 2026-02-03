# Plugin Polish: Declarative Login & Rich Terminal Output — Implementation Plan

**Goal:** Minimize plugin boilerplate by absorbing browser lifecycle into the framework via declarative login, and make all terminal output polished with Rich.

**Architecture:** Three layers: (1) a `console` module wrapping Rich for all user-facing output (stderr for status, stdout for data), (2) declarative login attributes on `SitePlugin` + YAML `login:` block that auto-generate the `login()` method, (3) a `browser_session()` context manager escape hatch for complex flows.

**Tech Stack:** Python 3.12, Rich, structlog, asyncio, nodriver, selenium

---

### Task 1: Create `graftpunk.console` Module

**Files:**
- Create: `src/graftpunk/console.py`
- Test: `tests/unit/test_console.py`

**Step 1: Write the failing test**

Create `tests/unit/test_console.py`:

```python
"""Tests for the console output module."""

import io

from rich.console import Console

from graftpunk.console import (
    error,
    info,
    status,
    success,
    warn,
)


class TestConsoleHelpers:
    """Tests for console helper functions."""

    def test_success_outputs_green_check(self) -> None:
        """Test success prints green checkmark to stderr."""
        buf = io.StringIO()
        test_console = Console(file=buf, stderr=True, no_color=True)
        success("Done", console=test_console)
        output = buf.getvalue()
        assert "Done" in output

    def test_error_outputs_red_x(self) -> None:
        """Test error prints red X to stderr."""
        buf = io.StringIO()
        test_console = Console(file=buf, stderr=True, no_color=True)
        error("Failed", console=test_console)
        output = buf.getvalue()
        assert "Failed" in output

    def test_warn_outputs_yellow(self) -> None:
        """Test warn prints yellow message to stderr."""
        buf = io.StringIO()
        test_console = Console(file=buf, stderr=True, no_color=True)
        warn("Careful", console=test_console)
        output = buf.getvalue()
        assert "Careful" in output

    def test_info_outputs_dim(self) -> None:
        """Test info prints dim message to stderr."""
        buf = io.StringIO()
        test_console = Console(file=buf, stderr=True, no_color=True)
        info("Note", console=test_console)
        output = buf.getvalue()
        assert "Note" in output
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_console.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'graftpunk.console'`

**Step 3: Write minimal implementation**

Create `src/graftpunk/console.py`:

```python
"""Centralized terminal output for graftpunk.

All user-facing output goes through this module. Plugins never import Rich
directly — the framework owns all terminal output.

Key principle: stderr for status/progress, stdout for data.
"""

from __future__ import annotations

from rich.console import Console

# stderr console for status messages (spinners, success/error)
err_console = Console(stderr=True)

# stdout console for data output (JSON, tables)
out_console = Console()


def success(message: str, *, console: Console | None = None) -> None:
    """Print a success message (green checkmark) to stderr."""
    c = console or err_console
    c.print(f"[green]  ✓ {message}[/green]")


def error(message: str, *, console: Console | None = None) -> None:
    """Print an error message (red X) to stderr."""
    c = console or err_console
    c.print(f"[red]  ✗ {message}[/red]")


def warn(message: str, *, console: Console | None = None) -> None:
    """Print a warning message (yellow) to stderr."""
    c = console or err_console
    c.print(f"[yellow]  ⚠ {message}[/yellow]")


def info(message: str, *, console: Console | None = None) -> None:
    """Print an info message (dim) to stderr."""
    c = console or err_console
    c.print(f"[dim]  {message}[/dim]")
```

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_console.py -v`
Expected: PASS

**Step 5: Run full test suite**

Run: `uv run pytest tests/ -v`
Expected: All tests pass

**Step 6: Commit**

```bash
git add src/graftpunk/console.py tests/unit/test_console.py
git commit -m "feat: add console module for centralized Rich terminal output"
```

---

### Task 2: Wire Console Into Login Command

**Files:**
- Modify: `src/graftpunk/cli/plugin_commands.py` (lines 207-277, the `_create_login_command` function)
- Test: `tests/unit/test_plugin_commands.py` (add new tests)

**Step 1: Write the failing test**

Add to `tests/unit/test_plugin_commands.py`:

```python
class TestLoginCommandOutput:
    """Tests for Rich login command output."""

    def test_login_success_uses_console_success(self) -> None:
        """Test that successful login uses console.success."""
        from unittest.mock import patch

        from graftpunk.cli.plugin_commands import _create_login_command

        class PluginWithLogin(SitePlugin):
            site_name = "richtest"
            session_name = "test"
            help_text = "Test"

            def login(self, username: str, password: str) -> bool:
                return True

        plugin = PluginWithLogin()
        cmd = _create_login_command(plugin)

        with patch("graftpunk.cli.plugin_commands.gp_console") as mock_console:
            from click.testing import CliRunner
            runner = CliRunner()
            result = runner.invoke(cmd, ["--username", "u", "--password", "p"])
            mock_console.success.assert_called_once()

    def test_login_failure_uses_console_warn(self) -> None:
        """Test that failed login uses console.warn."""
        from unittest.mock import patch

        from graftpunk.cli.plugin_commands import _create_login_command

        class PluginWithLogin(SitePlugin):
            site_name = "richtest"
            session_name = "test"
            help_text = "Test"

            def login(self, username: str, password: str) -> bool:
                return False

        plugin = PluginWithLogin()
        cmd = _create_login_command(plugin)

        with patch("graftpunk.cli.plugin_commands.gp_console") as mock_console:
            from click.testing import CliRunner
            runner = CliRunner()
            result = runner.invoke(cmd, ["--username", "u", "--password", "p"])
            mock_console.warn.assert_called_once()

    def test_login_exception_uses_console_error(self) -> None:
        """Test that login exception uses console.error."""
        from unittest.mock import patch

        from graftpunk.cli.plugin_commands import _create_login_command

        class PluginWithLogin(SitePlugin):
            site_name = "richtest"
            session_name = "test"
            help_text = "Test"

            def login(self, username: str, password: str) -> bool:
                raise RuntimeError("Connection failed")

        plugin = PluginWithLogin()
        cmd = _create_login_command(plugin)

        with patch("graftpunk.cli.plugin_commands.gp_console") as mock_console:
            from click.testing import CliRunner
            runner = CliRunner()
            result = runner.invoke(cmd, ["--username", "u", "--password", "p"])
            mock_console.error.assert_called_once()
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_plugin_commands.py::TestLoginCommandOutput -v`
Expected: FAIL (no `gp_console` import yet)

**Step 3: Modify `plugin_commands.py` to use console module**

In `src/graftpunk/cli/plugin_commands.py`:

1. Add import: `from graftpunk import console as gp_console`
2. Replace the login callback's `console.print(f"[green]...")` calls with `gp_console.success(...)`, `gp_console.warn(...)`, `gp_console.error(...)`
3. Add Rich `Status` spinner around the login call:

Replace lines 243-264 of `_create_login_command` callback:

```python
    def callback(username: str, password: str) -> None:
        login_method = plugin.login  # type: ignore[attr-defined]
        try:
            from rich.status import Status

            with Status("Logging in...", console=gp_console.err_console):
                if asyncio.iscoroutinefunction(login_method):
                    result = asyncio.run(login_method(username=username, password=password))
                else:
                    result = login_method(username=username, password=password)

            if result:
                gp_console.success(f"Logged in to {plugin.site_name} (session cached)")
            else:
                gp_console.warn(f"Login returned False for {plugin.site_name}")
        except Exception as exc:
            LOG.exception(
                "plugin_login_failed",
                plugin=plugin.site_name,
                error=str(exc),
                exc_type=type(exc).__name__,
            )
            gp_console.error(f"Login failed: {exc}")
            raise SystemExit(1) from None
```

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_plugin_commands.py::TestLoginCommandOutput -v`
Expected: PASS

**Step 5: Run full test suite**

Run: `uv run pytest tests/ -v`
Expected: All tests pass

**Step 6: Commit**

```bash
git add src/graftpunk/cli/plugin_commands.py tests/unit/test_plugin_commands.py
git commit -m "feat: wire console module into login command with spinner"
```

---

### Task 3: Wire Console Into Command Execution & Error Handling

**Files:**
- Modify: `src/graftpunk/cli/plugin_commands.py` (lines 140-189, the command callback and `_notify_plugin_errors`)

**Step 1: Write the failing test**

Add to `tests/unit/test_plugin_commands.py`:

```python
class TestCommandOutputConsole:
    """Tests for command execution using console module."""

    def test_session_not_found_uses_console_error(self, isolated_config: Path) -> None:
        """Test that session-not-found uses console.error."""
        from unittest.mock import patch

        from graftpunk.cli.plugin_commands import _create_click_command
        from graftpunk.exceptions import SessionNotFoundError

        mock_plugin = MockPlugin()
        mock_plugin.get_session = MagicMock(  # type: ignore[method-assign]
            side_effect=SessionNotFoundError("mocksession")
        )

        cmd_spec = CommandSpec(
            name="test",
            handler=lambda session: {"ok": True},
            help_text="Test",
            params=[],
        )
        click_cmd = _create_click_command(mock_plugin, cmd_spec)

        with patch("graftpunk.cli.plugin_commands.gp_console") as mock_con:
            from click.testing import CliRunner
            runner = CliRunner()
            result = runner.invoke(click_cmd, [])
            mock_con.error.assert_called_once()
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_plugin_commands.py::TestCommandOutputConsole -v`
Expected: FAIL

**Step 3: Update command callback to use console module**

In `_create_click_command`'s callback, replace `console.print(f"[red]...")` calls with `gp_console.error(...)`. Also replace `_notify_plugin_errors` to use `gp_console.warn` and `gp_console.info`.

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_plugin_commands.py::TestCommandOutputConsole -v`
Expected: PASS

**Step 5: Run full test suite**

Run: `uv run pytest tests/ -v`
Expected: All tests pass

**Step 6: Commit**

```bash
git add src/graftpunk/cli/plugin_commands.py tests/unit/test_plugin_commands.py
git commit -m "feat: use console module for all command error output"
```

---

### Task 4: Add Declarative Login Attributes to SitePlugin

**Files:**
- Modify: `src/graftpunk/plugins/cli_plugin.py` (add class attributes to `SitePlugin`)
- Test: `tests/unit/test_plugin_commands.py` (new test class)

**Step 1: Write the failing test**

Add to `tests/unit/test_plugin_commands.py`:

```python
class TestDeclarativeLogin:
    """Tests for declarative login spec on SitePlugin."""

    def test_declarative_attrs_exist(self) -> None:
        """Test SitePlugin has declarative login class attributes."""
        assert hasattr(SitePlugin, "backend")
        assert hasattr(SitePlugin, "login_url")
        assert hasattr(SitePlugin, "login_fields")
        assert hasattr(SitePlugin, "login_submit")
        assert hasattr(SitePlugin, "login_failure")
        assert hasattr(SitePlugin, "login_success")

    def test_declarative_attrs_defaults(self) -> None:
        """Test declarative login attributes default to empty/None."""
        assert SitePlugin.backend == "selenium"
        assert SitePlugin.login_url == ""
        assert SitePlugin.login_fields == {}
        assert SitePlugin.login_submit == ""
        assert SitePlugin.login_failure == ""
        assert SitePlugin.login_success == ""

    def test_declarative_login_detected(self) -> None:
        """Test that a plugin with declarative login attrs is detected."""
        from graftpunk.plugins.cli_plugin import has_declarative_login

        class DeclarativePlugin(SitePlugin):
            site_name = "decl"
            session_name = "decl"
            help_text = "Test"
            base_url = "https://example.com"
            backend = "nodriver"
            login_url = "/login"
            login_fields = {"username": "#user", "password": "#pass"}
            login_submit = "#submit"
            login_failure = "Bad login."

        plugin = DeclarativePlugin()
        assert has_declarative_login(plugin) is True

    def test_declarative_login_not_detected_without_fields(self) -> None:
        """Test that plugin without login_fields is not declarative."""
        from graftpunk.plugins.cli_plugin import has_declarative_login

        class NoFieldsPlugin(SitePlugin):
            site_name = "nofields"
            session_name = "nofields"
            help_text = "Test"
            login_url = "/login"

        plugin = NoFieldsPlugin()
        assert has_declarative_login(plugin) is False
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_plugin_commands.py::TestDeclarativeLogin -v`
Expected: FAIL (attributes don't exist yet)

**Step 3: Add declarative login attributes to SitePlugin**

In `src/graftpunk/plugins/cli_plugin.py`, add to `SitePlugin` class attributes:

```python
    # Declarative login configuration
    base_url: str = ""
    backend: str = "selenium"
    login_url: str = ""
    login_fields: dict[str, str] = {}
    login_submit: str = ""
    login_failure: str = ""
    login_success: str = ""
```

Add the helper function:

```python
def has_declarative_login(plugin: SitePlugin) -> bool:
    """Check if a plugin has declarative login configuration.

    Requires at minimum: login_url, login_fields, and login_submit.
    """
    login_url = getattr(plugin, "login_url", "")
    login_fields = getattr(plugin, "login_fields", {})
    login_submit = getattr(plugin, "login_submit", "")
    return bool(login_url and login_fields and login_submit)
```

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_plugin_commands.py::TestDeclarativeLogin -v`
Expected: PASS

**Step 5: Run full test suite**

Run: `uv run pytest tests/ -v`
Expected: All tests pass

**Step 6: Commit**

```bash
git add src/graftpunk/plugins/cli_plugin.py tests/unit/test_plugin_commands.py
git commit -m "feat: add declarative login attributes to SitePlugin"
```

---

### Task 5: Implement Declarative Login Engine

**Files:**
- Create: `src/graftpunk/plugins/login_engine.py`
- Test: `tests/unit/test_login_engine.py`

**Step 1: Write the failing test**

Create `tests/unit/test_login_engine.py`:

```python
"""Tests for the declarative login engine."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from graftpunk.plugins.cli_plugin import SitePlugin


class DeclarativeHN(SitePlugin):
    """Test plugin with declarative login (nodriver)."""

    site_name = "hn"
    session_name = "hackernews"
    help_text = "HN"
    base_url = "https://news.ycombinator.com"
    backend = "nodriver"
    login_url = "/login"
    login_fields = {"username": "input[name='acct']", "password": "input[name='pw']"}
    login_submit = "input[value='login']"
    login_failure = "Bad login."


class DeclarativeQuotes(SitePlugin):
    """Test plugin with declarative login (selenium)."""

    site_name = "quotes"
    session_name = "quotes"
    help_text = "Quotes"
    base_url = "https://quotes.toscrape.com"
    backend = "selenium"
    login_url = "/login"
    login_fields = {"username": "#username", "password": "#password"}
    login_submit = "input[type='submit']"
    login_success = "a[href='/logout']"


class TestDeclarativeLoginEngine:
    """Tests for declarative login engine."""

    def test_generate_login_nodriver(self) -> None:
        """Test generating async login method for nodriver backend."""
        from graftpunk.plugins.login_engine import generate_login_method

        plugin = DeclarativeHN()
        login_method = generate_login_method(plugin)
        assert callable(login_method)
        import asyncio
        assert asyncio.iscoroutinefunction(login_method)

    def test_generate_login_selenium(self) -> None:
        """Test generating sync login method for selenium backend."""
        from graftpunk.plugins.login_engine import generate_login_method

        plugin = DeclarativeQuotes()
        login_method = generate_login_method(plugin)
        assert callable(login_method)
        import asyncio
        assert not asyncio.iscoroutinefunction(login_method)

    @pytest.mark.asyncio
    async def test_nodriver_login_success(self) -> None:
        """Test nodriver declarative login success path."""
        from graftpunk.plugins.login_engine import generate_login_method

        plugin = DeclarativeHN()
        login_method = generate_login_method(plugin)

        mock_session = MagicMock()
        mock_tab = AsyncMock()
        mock_element = AsyncMock()
        mock_tab.select = AsyncMock(return_value=mock_element)
        mock_tab.get_content = AsyncMock(return_value="<html>Welcome</html>")

        with (
            patch("graftpunk.plugins.login_engine.BrowserSession") as MockBS,
            patch("graftpunk.plugins.login_engine.cache_session"),
        ):
            instance = MockBS.return_value
            instance.start_async = AsyncMock()
            instance.driver = MagicMock()
            instance.driver.get = AsyncMock(return_value=mock_tab)
            instance.transfer_nodriver_cookies_to_session = AsyncMock()
            instance.driver.stop = MagicMock()

            result = await login_method(plugin, username="user", password="pass")

        assert result is True

    @pytest.mark.asyncio
    async def test_nodriver_login_failure(self) -> None:
        """Test nodriver declarative login failure path."""
        from graftpunk.plugins.login_engine import generate_login_method

        plugin = DeclarativeHN()
        login_method = generate_login_method(plugin)

        mock_tab = AsyncMock()
        mock_element = AsyncMock()
        mock_tab.select = AsyncMock(return_value=mock_element)
        mock_tab.get_content = AsyncMock(return_value="<html>Bad login.</html>")

        with patch("graftpunk.plugins.login_engine.BrowserSession") as MockBS:
            instance = MockBS.return_value
            instance.start_async = AsyncMock()
            instance.driver = MagicMock()
            instance.driver.get = AsyncMock(return_value=mock_tab)
            instance.driver.stop = MagicMock()

            result = await login_method(plugin, username="user", password="wrong")

        assert result is False

    def test_selenium_login_success(self) -> None:
        """Test selenium declarative login success path."""
        from graftpunk.plugins.login_engine import generate_login_method

        plugin = DeclarativeQuotes()
        login_method = generate_login_method(plugin)

        mock_element = MagicMock()

        with (
            patch("graftpunk.plugins.login_engine.BrowserSession") as MockBS,
            patch("graftpunk.plugins.login_engine.cache_session"),
        ):
            instance = MockBS.return_value
            instance.driver = MagicMock()
            instance.driver.get = MagicMock()
            instance.driver.find_element = MagicMock(return_value=mock_element)
            instance.transfer_driver_cookies_to_session = MagicMock()
            instance.quit = MagicMock()

            result = login_method(plugin, username="user", password="pass")

        assert result is True

    def test_selenium_login_failure_element_not_found(self) -> None:
        """Test selenium login failure when success element not found."""
        from graftpunk.plugins.login_engine import generate_login_method

        plugin = DeclarativeQuotes()
        login_method = generate_login_method(plugin)

        with (
            patch("graftpunk.plugins.login_engine.BrowserSession") as MockBS,
            patch("graftpunk.plugins.login_engine.cache_session"),
        ):
            instance = MockBS.return_value
            instance.driver = MagicMock()
            instance.driver.get = MagicMock()
            instance.driver.find_element = MagicMock(
                side_effect=[MagicMock(), MagicMock(), MagicMock(),  # field fills + submit
                             Exception("not found")]  # success element check
            )
            instance.quit = MagicMock()

            # Need a plugin that uses login_success (element check)
            # The find_element for login_success will raise
            result = login_method(plugin, username="user", password="pass")

        assert result is False
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_login_engine.py -v`
Expected: FAIL with `ModuleNotFoundError`

**Step 3: Write the login engine**

Create `src/graftpunk/plugins/login_engine.py`:

```python
"""Declarative login engine for plugins.

Generates login() methods from declarative configuration (CSS selectors,
success/failure indicators). Handles browser lifecycle, cookie transfer,
and session caching automatically.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

from graftpunk import BrowserSession, cache_session
from graftpunk.logging import get_logger

if TYPE_CHECKING:
    from graftpunk.plugins.cli_plugin import SitePlugin

LOG = get_logger(__name__)


def generate_login_method(plugin: SitePlugin) -> Any:
    """Generate a login method from declarative plugin attributes.

    Returns an async function for nodriver backend, sync for selenium.

    Args:
        plugin: Plugin instance with declarative login attributes.

    Returns:
        Callable login method (async or sync depending on backend).
    """
    backend = getattr(plugin, "backend", "selenium")

    if backend == "nodriver":
        return _generate_nodriver_login(plugin)
    return _generate_selenium_login(plugin)


def _generate_nodriver_login(plugin: SitePlugin) -> Any:
    """Generate async login method for nodriver backend."""

    async def login(self: Any, username: str, password: str) -> bool:
        base_url = plugin.base_url.rstrip("/")
        login_url = plugin.login_url
        fields = plugin.login_fields
        submit_selector = plugin.login_submit
        failure_text = plugin.login_failure

        session = BrowserSession(backend="nodriver", headless=False)
        await session.start_async()

        try:
            tab = await session.driver.get(f"{base_url}{login_url}")

            # Fill fields (click before send_keys to prevent keystroke loss)
            for field_name, selector in fields.items():
                value = username if field_name == "username" else password
                element = await tab.select(selector)
                await element.click()
                await element.send_keys(value)

            # Click submit
            submit = await tab.select(submit_selector)
            await submit.click()

            # Wait for page load
            await asyncio.sleep(3)

            # Check success/failure
            page_text = await tab.get_content()
            if failure_text and failure_text in page_text:
                session.driver.stop()
                return False

            # Transfer cookies and cache
            await session.transfer_nodriver_cookies_to_session()
            cache_session(session, plugin.session_name)
            session.driver.stop()
            return True
        except Exception:
            try:
                session.driver.stop()
            except Exception:
                pass
            raise

    return login


def _generate_selenium_login(plugin: SitePlugin) -> Any:
    """Generate sync login method for selenium backend."""

    def login(self: Any, username: str, password: str) -> bool:
        base_url = plugin.base_url.rstrip("/")
        login_url = plugin.login_url
        fields = plugin.login_fields
        submit_selector = plugin.login_submit
        failure_text = plugin.login_failure
        success_selector = plugin.login_success

        session = BrowserSession(backend="selenium", headless=False)

        try:
            session.driver.get(f"{base_url}{login_url}")

            # Fill fields
            for field_name, selector in fields.items():
                value = username if field_name == "username" else password
                element = session.driver.find_element("css selector", selector)
                element.click()
                element.send_keys(value)

            # Click submit
            submit = session.driver.find_element("css selector", submit_selector)
            submit.click()

            # Wait for page load
            time.sleep(2)

            # Check success/failure
            if failure_text:
                page_source = session.driver.page_source
                if failure_text in page_source:
                    session.quit()
                    return False
            elif success_selector:
                try:
                    session.driver.find_element("css selector", success_selector)
                except Exception:
                    session.quit()
                    return False

            # Cache session
            session.transfer_driver_cookies_to_session()
            cache_session(session, plugin.session_name)
            session.quit()
            return True
        except Exception:
            try:
                session.quit()
            except Exception:
                pass
            raise

    return login
```

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_login_engine.py -v`
Expected: PASS

**Step 5: Run full test suite**

Run: `uv run pytest tests/ -v`
Expected: All tests pass

**Step 6: Commit**

```bash
git add src/graftpunk/plugins/login_engine.py tests/unit/test_login_engine.py
git commit -m "feat: add declarative login engine for nodriver and selenium"
```

---

### Task 6: Auto-Generate Login from Declarative Attrs

**Files:**
- Modify: `src/graftpunk/cli/plugin_commands.py` (update `_has_login_method` and registration loop)
- Test: `tests/unit/test_plugin_commands.py` (new tests)

**Step 1: Write the failing test**

Add to `tests/unit/test_plugin_commands.py`:

```python
class TestDeclarativeLoginRegistration:
    """Tests for auto-generating login from declarative attributes."""

    def test_declarative_plugin_gets_login_command(self, isolated_config: Path) -> None:
        """Test that declarative login plugin gets auto-generated login command."""
        import click

        from graftpunk.cli.plugin_commands import (
            _plugin_groups,
            inject_plugin_commands,
            register_plugin_commands,
        )

        class DeclPlugin(SitePlugin):
            site_name = "decltest"
            session_name = "decltest"
            help_text = "Declarative test"
            base_url = "https://example.com"
            backend = "selenium"
            login_url = "/login"
            login_fields = {"username": "#user", "password": "#pass"}
            login_submit = "#submit"
            login_failure = "Bad login."

            @command(help="List items")
            def items(self, session: Any) -> dict[str, list[int]]:
                return {"items": []}

        app = typer.Typer()
        _plugin_groups.clear()

        with (
            patch("graftpunk.cli.plugin_commands.discover_cli_plugins") as mock_py,
            patch("graftpunk.cli.plugin_commands.create_yaml_plugins") as mock_yaml,
        ):
            mock_py.return_value = {"decltest": DeclPlugin}
            mock_yaml.return_value = ([], [])
            registered = register_plugin_commands(app, notify_errors=False)

        assert "decltest" in registered

        click_group = click.Group(name="test")
        inject_plugin_commands(click_group)

        plugin_group = click_group.commands["decltest"]
        assert isinstance(plugin_group, click.Group)
        assert "login" in plugin_group.commands
        assert "items" in plugin_group.commands

    def test_explicit_login_overrides_declarative(self, isolated_config: Path) -> None:
        """Test that an explicit login() method takes precedence over declarative."""
        from graftpunk.cli.plugin_commands import _has_login_method

        class PluginWithBoth(SitePlugin):
            site_name = "both"
            session_name = "both"
            help_text = "Test"
            base_url = "https://example.com"
            login_url = "/login"
            login_fields = {"username": "#user", "password": "#pass"}
            login_submit = "#submit"

            def login(self, username: str, password: str) -> bool:
                return True

        plugin = PluginWithBoth()
        # The explicit login() should be detected by _has_login_method
        assert _has_login_method(plugin) is True
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_plugin_commands.py::TestDeclarativeLoginRegistration -v`
Expected: FAIL (declarative plugin doesn't get login command yet)

**Step 3: Update plugin_commands.py**

In the registration loop, after checking `_has_login_method(plugin)`, add a check for declarative login:

```python
from graftpunk.plugins.cli_plugin import has_declarative_login
from graftpunk.plugins.login_engine import generate_login_method

# In the registration loop, after the existing login auto-registration block:
# Auto-generate login from declarative attributes if no explicit login
if not _has_login_method(plugin) and has_declarative_login(plugin):
    try:
        login_func = generate_login_method(plugin)
        plugin.login = login_func.__get__(plugin, type(plugin))  # type: ignore[attr-defined]
        login_cmd = _create_login_command(plugin)
        plugin_group.add_command(login_cmd, name="login")
        LOG.debug("declarative_login_registered", plugin=site_name)
    except Exception as exc:
        LOG.warning("declarative_login_failed", plugin=site_name, error=str(exc))
        result.add_error(f"{site_name}.login", str(exc), "registration")
```

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_plugin_commands.py::TestDeclarativeLoginRegistration -v`
Expected: PASS

**Step 5: Run full test suite**

Run: `uv run pytest tests/ -v`
Expected: All tests pass

**Step 6: Commit**

```bash
git add src/graftpunk/cli/plugin_commands.py tests/unit/test_plugin_commands.py
git commit -m "feat: auto-generate login command from declarative plugin attributes"
```

---

### Task 7: Add YAML `login:` Block Support

**Files:**
- Modify: `src/graftpunk/plugins/yaml_loader.py` (add `login:` parsing)
- Modify: `src/graftpunk/plugins/yaml_plugin.py` (expose declarative login attrs)
- Test: `tests/unit/test_yaml_loader.py` (new tests)

**Step 1: Write the failing test**

Add to `tests/unit/test_yaml_loader.py`:

```python
class TestYAMLLoginBlock:
    """Tests for YAML login block parsing."""

    def test_parse_login_block(self, isolated_config: Path) -> None:
        """Test parsing a YAML plugin with login block."""
        from graftpunk.plugins.yaml_loader import parse_yaml_plugin

        yaml_content = """
site_name: hn
session_name: hackernews
base_url: "https://news.ycombinator.com"
backend: nodriver

login:
  url: /login
  fields:
    username: "input[name='acct']"
    password: "input[name='pw']"
  submit: "input[value='login']"
  failure: "Bad login."

commands:
  front:
    help: "Get front page"
    url: "/news"
"""
        plugins_dir = isolated_config / "plugins"
        plugins_dir.mkdir(exist_ok=True)
        yaml_file = plugins_dir / "hn.yaml"
        yaml_file.write_text(yaml_content)

        plugin_def = parse_yaml_plugin(yaml_file)

        assert plugin_def.login is not None
        assert plugin_def.login.url == "/login"
        assert plugin_def.login.fields == {
            "username": "input[name='acct']",
            "password": "input[name='pw']",
        }
        assert plugin_def.login.submit == "input[value='login']"
        assert plugin_def.login.failure == "Bad login."
        assert plugin_def.login.success == ""

    def test_parse_no_login_block(self, isolated_config: Path) -> None:
        """Test parsing YAML without login block."""
        from graftpunk.plugins.yaml_loader import parse_yaml_plugin

        yaml_content = """
site_name: simple
commands:
  ping:
    url: "/ping"
"""
        plugins_dir = isolated_config / "plugins"
        plugins_dir.mkdir(exist_ok=True)
        yaml_file = plugins_dir / "simple.yaml"
        yaml_file.write_text(yaml_content)

        plugin_def = parse_yaml_plugin(yaml_file)
        assert plugin_def.login is None
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_yaml_loader.py::TestYAMLLoginBlock -v`
Expected: FAIL

**Step 3: Add login block to YAML loader**

In `src/graftpunk/plugins/yaml_loader.py`, add a `YAMLLoginDef` dataclass:

```python
@dataclass
class YAMLLoginDef:
    """Parsed YAML login block definition."""

    url: str
    fields: dict[str, str]
    submit: str
    failure: str = ""
    success: str = ""
```

Add `login: YAMLLoginDef | None = None` and `backend: str = "selenium"` to `YAMLPluginDef`.

In `parse_yaml_plugin()`, parse the `login:` block:

```python
    # Parse login block
    login_def = None
    login_data = data.get("login")
    if login_data and isinstance(login_data, dict):
        login_def = YAMLLoginDef(
            url=login_data.get("url", ""),
            fields=login_data.get("fields", {}),
            submit=login_data.get("submit", ""),
            failure=login_data.get("failure", ""),
            success=login_data.get("success", ""),
        )

    return YAMLPluginDef(
        ...
        backend=data.get("backend", "selenium"),
        login=login_def,
    )
```

In `src/graftpunk/plugins/yaml_plugin.py`, expose declarative login attributes on `YAMLSitePlugin` so the registration loop can detect them:

```python
    @property
    def base_url(self) -> str:
        return self._def.base_url

    @property
    def backend(self) -> str:
        return self._def.backend

    @property
    def login_url(self) -> str:
        return self._def.login.url if self._def.login else ""

    @property
    def login_fields(self) -> dict[str, str]:
        return self._def.login.fields if self._def.login else {}

    @property
    def login_submit(self) -> str:
        return self._def.login.submit if self._def.login else ""

    @property
    def login_failure(self) -> str:
        return self._def.login.failure if self._def.login else ""

    @property
    def login_success(self) -> str:
        return self._def.login.success if self._def.login else ""
```

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_yaml_loader.py::TestYAMLLoginBlock -v`
Expected: PASS

**Step 5: Run full test suite**

Run: `uv run pytest tests/ -v`
Expected: All tests pass

**Step 6: Commit**

```bash
git add src/graftpunk/plugins/yaml_loader.py src/graftpunk/plugins/yaml_plugin.py tests/unit/test_yaml_loader.py
git commit -m "feat: add YAML login block parsing and expose declarative attrs"
```

---

### Task 8: Add `browser_session()` Context Manager Escape Hatch

**Files:**
- Modify: `src/graftpunk/plugins/cli_plugin.py` (add context manager methods to `SitePlugin`)
- Test: `tests/unit/test_plugin_commands.py` (new tests)

**Step 1: Write the failing test**

```python
class TestBrowserSessionContextManager:
    """Tests for browser_session() context manager escape hatch."""

    @pytest.mark.asyncio
    async def test_browser_session_async(self) -> None:
        """Test async browser_session context manager."""
        from unittest.mock import AsyncMock, MagicMock, patch

        class AsyncPlugin(SitePlugin):
            site_name = "asynctest"
            session_name = "asynctest"
            help_text = "Test"
            base_url = "https://example.com"
            backend = "nodriver"

        plugin = AsyncPlugin()

        with (
            patch("graftpunk.plugins.cli_plugin.BrowserSession") as MockBS,
            patch("graftpunk.plugins.cli_plugin.cache_session"),
        ):
            instance = MockBS.return_value
            instance.start_async = AsyncMock()
            instance.driver = MagicMock()
            mock_tab = AsyncMock()
            instance.driver.get = AsyncMock(return_value=mock_tab)
            instance.transfer_nodriver_cookies_to_session = AsyncMock()
            instance.driver.stop = MagicMock()

            async with plugin.browser_session() as (session, tab):
                assert session is instance
                assert tab is mock_tab

            # Verify cleanup happened
            instance.transfer_nodriver_cookies_to_session.assert_awaited_once()

    def test_browser_session_sync(self) -> None:
        """Test sync browser_session_sync context manager."""
        from unittest.mock import MagicMock, patch

        class SyncPlugin(SitePlugin):
            site_name = "synctest"
            session_name = "synctest"
            help_text = "Test"
            base_url = "https://example.com"
            backend = "selenium"

        plugin = SyncPlugin()

        with (
            patch("graftpunk.plugins.cli_plugin.BrowserSession") as MockBS,
            patch("graftpunk.plugins.cli_plugin.cache_session"),
        ):
            instance = MockBS.return_value
            instance.driver = MagicMock()
            instance.transfer_driver_cookies_to_session = MagicMock()
            instance.quit = MagicMock()

            with plugin.browser_session_sync() as (session, driver):
                assert session is instance
                assert driver is instance.driver

            # Verify cleanup happened
            instance.transfer_driver_cookies_to_session.assert_called_once()
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_plugin_commands.py::TestBrowserSessionContextManager -v`
Expected: FAIL

**Step 3: Add context manager methods to SitePlugin**

In `src/graftpunk/plugins/cli_plugin.py`, add:

```python
from contextlib import asynccontextmanager, contextmanager

# ... in SitePlugin class:

    @asynccontextmanager
    async def browser_session(self):
        """Async context manager for nodriver browser sessions.

        Handles browser creation, startup, cookie transfer, caching, and cleanup.

        Usage:
            async with self.browser_session() as (session, tab):
                # ... custom login logic ...
                return "Welcome" in await tab.get_content()
        """
        from graftpunk import BrowserSession, cache_session

        session = BrowserSession(backend="nodriver", headless=False)
        await session.start_async()
        tab = await session.driver.get(f"{self.base_url}/")
        try:
            yield session, tab
            # Success path: transfer cookies and cache
            await session.transfer_nodriver_cookies_to_session()
            cache_session(session, self.session_name)
        finally:
            try:
                session.driver.stop()
            except Exception:
                pass

    @contextmanager
    def browser_session_sync(self):
        """Sync context manager for selenium browser sessions.

        Handles browser creation, cookie transfer, caching, and cleanup.

        Usage:
            with self.browser_session_sync() as (session, driver):
                driver.get(f"{self.base_url}/login")
                # ... custom login logic ...
        """
        from graftpunk import BrowserSession, cache_session

        session = BrowserSession(backend="selenium", headless=False)
        try:
            yield session, session.driver
            # Success path: transfer cookies and cache
            session.transfer_driver_cookies_to_session()
            cache_session(session, self.session_name)
        finally:
            try:
                session.quit()
            except Exception:
                pass
```

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_plugin_commands.py::TestBrowserSessionContextManager -v`
Expected: PASS

**Step 5: Run full test suite**

Run: `uv run pytest tests/ -v`
Expected: All tests pass

**Step 6: Commit**

```bash
git add src/graftpunk/plugins/cli_plugin.py tests/unit/test_plugin_commands.py
git commit -m "feat: add browser_session() context manager escape hatch"
```

---

### Task 9: Reduce Log Verbosity (Warnings Only by Default)

**Files:**
- Modify: `src/graftpunk/logging.py` (change default level)
- Modify: `src/graftpunk/cli/main.py` (add `-v`/`-vv` flags)
- Test: `tests/unit/test_cli.py` (verify default log level)

**Step 1: Write the failing test**

Add to `tests/unit/test_cli.py`:

```python
class TestVerbosity:
    """Tests for CLI verbosity flags."""

    def test_default_log_level_is_warning(self) -> None:
        """Test that default log level is WARNING (minimal output)."""
        from graftpunk.logging import configure_logging
        import structlog

        configure_logging(level="WARNING")
        # Just verify it configures without error
        logger = structlog.get_logger("test")
        assert logger is not None
```

**Step 2: Run test to verify current behavior**

Run: `uv run pytest tests/unit/test_cli.py::TestVerbosity -v`

**Step 3: Update default log level**

In `src/graftpunk/logging.py`, change default from `"INFO"` to `"WARNING"`:

```python
def configure_logging(
    level: str = "WARNING",
    json_output: bool = False,
) -> None:
```

In `src/graftpunk/cli/main.py`, add a callback for verbosity. Add a `verbose` option to the Typer app:

```python
@app.callback()
def main_callback(
    verbose: Annotated[
        int,
        typer.Option(
            "--verbose", "-v",
            count=True,
            help="Increase verbosity (-v for info, -vv for debug)",
        ),
    ] = 0,
) -> None:
    """graftpunk - turn any website into an API."""
    from graftpunk.logging import configure_logging

    if verbose >= 2:
        configure_logging(level="DEBUG")
    elif verbose >= 1:
        configure_logging(level="INFO")
    else:
        configure_logging(level="WARNING")
```

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_cli.py::TestVerbosity -v`
Expected: PASS

**Step 5: Run full test suite**

Run: `uv run pytest tests/ -v`
Expected: All tests pass

**Step 6: Commit**

```bash
git add src/graftpunk/logging.py src/graftpunk/cli/main.py tests/unit/test_cli.py
git commit -m "feat: reduce default log verbosity, add -v/-vv flags"
```

---

### Task 10: Update Example Plugins to Use Declarative Login

**Files:**
- Modify: `examples/plugins/hackernews.py` (replace manual login with declarative attrs)
- Modify: `examples/plugins/quotes.py` (replace manual login with declarative attrs)

**Step 1: Rewrite hackernews.py**

```python
"""Hacker News plugin - declarative login example (NoDriver).

Site: https://news.ycombinator.com
Auth: Requires real Hacker News account

Usage:
    1. Symlink: ln -s $(pwd)/examples/plugins/hackernews.py ~/.config/graftpunk/plugins/
    2. Login: gp hn login -u your_username
    3. Use: gp hn front
"""

from graftpunk.plugins import SitePlugin, command


class HackerNewsPlugin(SitePlugin):
    """Plugin for Hacker News (news.ycombinator.com)."""

    site_name = "hn"
    session_name = "hackernews"
    help_text = "Hacker News commands"
    base_url = "https://news.ycombinator.com"
    backend = "nodriver"

    login_url = "/login"
    login_fields = {"username": "input[name='acct']", "password": "input[name='pw']"}
    login_submit = "input[value='login']"
    login_failure = "Bad login."

    @command(help="Get front page stories")
    def front(self, session, page: int = 1):
        response = session.get(f"{self.base_url}/news?p={page}")
        return {"url": response.url, "status": response.status_code, "page": page}

    @command(help="Get newest stories")
    def newest(self, session, page: int = 1):
        response = session.get(f"{self.base_url}/newest?p={page}")
        return {"url": response.url, "status": response.status_code, "page": page}

    @command(help="Get saved stories (requires login)")
    def saved(self, session):
        user_cookie = session.cookies.get("user", "")
        username = user_cookie.split("&")[0] if user_cookie else ""
        if not username:
            return {"error": "Not logged in. Run: gp hn login"}
        response = session.get(f"{self.base_url}/favorites?id={username}")
        return {"url": response.url, "status": response.status_code}
```

**Step 2: Rewrite quotes.py**

```python
"""Quotes to Scrape plugin - declarative login example (Selenium).

Site: https://quotes.toscrape.com
Auth: Any username/password works (test site)

Usage:
    1. Symlink: ln -s $(pwd)/examples/plugins/quotes.py ~/.config/graftpunk/plugins/
    2. Login: gp quotes login
    3. Use: gp quotes list
"""

from graftpunk.plugins import SitePlugin, command


class QuotesPlugin(SitePlugin):
    """Plugin for quotes.toscrape.com (test site, any credentials work)."""

    site_name = "quotes"
    session_name = "quotes"
    help_text = "Quotes to Scrape commands (test site)"
    base_url = "https://quotes.toscrape.com"
    backend = "selenium"

    login_url = "/login"
    login_fields = {"username": "#username", "password": "#password"}
    login_submit = "input[type='submit']"
    login_success = "a[href='/logout']"

    @command(help="List quotes from a page")
    def list(self, session, page: int = 1):
        response = session.get(f"{self.base_url}/page/{page}/")
        return {"url": response.url, "status": response.status_code, "page": page}

    @command(help="Get a random quote")
    def random(self, session):
        response = session.get(f"{self.base_url}/random")
        return {"url": response.url, "status": response.status_code}
```

**Step 3: Also update the symlinked copy**

```bash
# The symlinked copy at ~/.config/graftpunk/plugins/ will auto-update
# since it's a symlink. Verify:
ls -la ~/.config/graftpunk/plugins/hackernews.py
ls -la ~/.config/graftpunk/plugins/quotes.py
```

**Step 4: Run full test suite**

Run: `uv run pytest tests/ -v`
Expected: All tests pass

**Step 5: Commit**

```bash
git add examples/plugins/hackernews.py examples/plugins/quotes.py
git commit -m "refactor: convert example plugins to declarative login"
```

---

### Task 11: Smoke Test All Plugins End-to-End

**Files:**
- No code changes — manual verification

**Step 1: Verify `gp --help` shows plugins**

Run: `gp --help`
Expected: Shows hn, quotes, httpbin subcommands

**Step 2: Test declarative login flow (quotes)**

Run: `gp quotes login` (use testuser/testpass)
Expected: Spinner → "Logged in to quotes (session cached)"

**Step 3: Test command output**

Run: `gp quotes list`
Expected: JSON output to stdout

**Step 4: Test verbosity flags**

Run: `gp -v quotes list`
Expected: Info-level log messages visible

Run: `gp -vv quotes list`
Expected: Debug-level log messages visible

**Step 5: Test httpbin still works**

Run: `gp httpbin ip`
Expected: JSON with origin IP

---

### Task 12: Run Quality Checks & Final Commit

**Step 1: Run all quality checks**

```bash
uvx ruff check --fix .
uvx ruff format .
uvx ty check src/
uv run pytest tests/ -v
```

**Step 2: Fix any issues found**

**Step 3: Final commit if needed**

```bash
git add -u
git commit -m "chore: fix lint and formatting"
```

**Step 4: Push to remote**

```bash
git push
```
