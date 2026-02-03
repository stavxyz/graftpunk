# Example Plugins and Templates Implementation Plan

**Goal:** Create example plugins and templates for issue #5, plus add Python plugin auto-discovery.

**Architecture:** Examples live in `examples/` directory, discoverable via symlink to `~/.config/graftpunk/plugins/`. Python auto-discovery mirrors existing YAML discovery pattern.

**Tech Stack:** Python 3.11+, typer/click for CLI, pytest for testing.

---

## Task 1: Create examples directory structure

**Files:**
- Create: `examples/README.md`
- Create: `examples/plugins/.gitkeep`
- Create: `examples/templates/.gitkeep`

**Step 1: Create directory structure**

```bash
mkdir -p examples/plugins examples/templates
touch examples/plugins/.gitkeep examples/templates/.gitkeep
```

**Step 2: Create minimal README placeholder**

Create `examples/README.md`:

```markdown
# graftpunk Examples

Example plugins and templates for building your own graftpunk integrations.

See individual example files for documentation.
```

**Step 3: Commit**

```bash
git add examples/
git commit -m "chore: create examples directory structure

Co-Authored-By: stavxyz <hi@stav.xyz>"
```

---

## Task 2: Add httpbin.yaml (no-auth YAML plugin)

**Files:**
- Create: `examples/plugins/httpbin.yaml`

**Step 1: Create the plugin file**

Create `examples/plugins/httpbin.yaml`:

```yaml
# httpbin.yaml - Simplest example plugin (no authentication required)
#
# httpbin.org is a free HTTP testing service. This plugin demonstrates
# basic YAML plugin structure without any session/auth complexity.
#
# Usage:
#   1. Symlink to plugins directory:
#      ln -s $(pwd)/examples/plugins/httpbin.yaml ~/.config/graftpunk/plugins/
#   2. Run commands:
#      gp httpbin ip
#      gp httpbin headers
#      gp httpbin status --code 418

site_name: httpbin
session_name: ""  # Empty string = no session required
help: "httpbin.org test commands (no auth required)"
base_url: "https://httpbin.org"

commands:
  ip:
    help: "Get your public IP address"
    method: GET
    url: "/ip"

  headers:
    help: "Echo request headers back to you"
    method: GET
    url: "/headers"

  get:
    help: "Test GET request (echoes query params)"
    method: GET
    url: "/get"

  status:
    help: "Return a specific HTTP status code"
    method: GET
    url: "/status/{code}"
    params:
      - name: code
        type: int
        required: true
        help: "HTTP status code to return (e.g., 200, 404, 418)"
        is_option: false

  uuid:
    help: "Generate a random UUID"
    method: GET
    url: "/uuid"
    jmespath: "uuid"
```

**Step 2: Verify it parses correctly**

```bash
python -c "
from graftpunk.plugins.yaml_loader import parse_yaml_plugin
from pathlib import Path
p = parse_yaml_plugin(Path('examples/plugins/httpbin.yaml'))
print(f'Loaded: {p.site_name} with {len(p.commands)} commands')
for c in p.commands:
    print(f'  - {c.name}: {c.help_text}')
"
```

Expected output:
```
Loaded: httpbin with 5 commands
  - ip: Get your public IP address
  - headers: Echo request headers back to you
  - get: Test GET request (echoes query params)
  - status: Return a specific HTTP status code
  - uuid: Generate a random UUID
```

**Step 3: Commit**

```bash
git add examples/plugins/httpbin.yaml
git commit -m "feat: add httpbin.yaml example plugin

Simplest example - no authentication required.
Demonstrates YAML plugin structure with commands,
parameters, and jmespath extraction.

Co-Authored-By: stavxyz <hi@stav.xyz>"
```

---

## Task 3: Add httpbin_auth.yaml (header auth YAML plugin)

**Files:**
- Create: `examples/plugins/httpbin_auth.yaml`

**Step 1: Create the plugin file**

Create `examples/plugins/httpbin_auth.yaml`:

```yaml
# httpbin_auth.yaml - YAML plugin with header authentication
#
# Demonstrates injecting auth headers via environment variables.
# The ${VAR} syntax expands environment variables at request time.
#
# Usage:
#   1. Symlink to plugins directory:
#      ln -s $(pwd)/examples/plugins/httpbin_auth.yaml ~/.config/graftpunk/plugins/
#   2. Set auth (base64 of "user:pass"):
#      export HTTPBIN_AUTH="dXNlcjpwYXNz"
#   3. Run commands:
#      gp httpbin-auth whoami
#      gp httpbin-auth basic-auth --user testuser --pass testpass

site_name: httpbin-auth
session_name: ""  # No cached session needed
help: "httpbin.org with authentication headers"
base_url: "https://httpbin.org"

headers:
  Authorization: "Basic ${HTTPBIN_AUTH}"

commands:
  whoami:
    help: "Check that auth header is being sent"
    method: GET
    url: "/headers"
    jmespath: "headers.Authorization"

  bearer-test:
    help: "Test bearer token endpoint (returns 200 if Authorization header present)"
    method: GET
    url: "/bearer"
```

**Step 2: Verify it parses correctly**

```bash
python -c "
from graftpunk.plugins.yaml_loader import parse_yaml_plugin
from pathlib import Path
p = parse_yaml_plugin(Path('examples/plugins/httpbin_auth.yaml'))
print(f'Loaded: {p.site_name}')
print(f'Headers: {p.headers}')
"
```

Expected output:
```
Loaded: httpbin-auth
Headers: {'Authorization': 'Basic ${HTTPBIN_AUTH}'}
```

**Step 3: Commit**

```bash
git add examples/plugins/httpbin_auth.yaml
git commit -m "feat: add httpbin_auth.yaml example plugin

Demonstrates header injection via environment variables.
Uses \${HTTPBIN_AUTH} syntax for auth header.

Co-Authored-By: stavxyz <hi@stav.xyz>"
```

---

## Task 4: Add Python plugin auto-discovery (python_loader.py)

**Files:**
- Create: `src/graftpunk/plugins/python_loader.py`
- Modify: `src/graftpunk/plugins/__init__.py`
- Create: `tests/unit/test_python_loader.py`

### Step 1: Write the failing test

Create `tests/unit/test_python_loader.py`:

```python
"""Tests for Python plugin auto-discovery."""

from pathlib import Path

import pytest


class TestDiscoverPythonPlugins:
    """Tests for Python plugin discovery from plugins directory."""

    def test_discover_no_plugins_dir(self, isolated_config: Path) -> None:
        """Test discovery when plugins directory doesn't exist."""
        from graftpunk.plugins.python_loader import discover_python_plugins

        result = discover_python_plugins()
        assert result.plugins == []
        assert result.errors == []

    def test_discover_empty_plugins_dir(self, isolated_config: Path) -> None:
        """Test discovery with empty plugins directory."""
        from graftpunk.plugins.python_loader import discover_python_plugins

        plugins_dir = isolated_config / "plugins"
        plugins_dir.mkdir()

        result = discover_python_plugins()
        assert result.plugins == []
        assert result.errors == []

    def test_discover_single_plugin(self, isolated_config: Path) -> None:
        """Test discovering a single Python plugin."""
        from graftpunk.plugins.python_loader import discover_python_plugins

        plugins_dir = isolated_config / "plugins"
        plugins_dir.mkdir()

        # Create a simple plugin file
        plugin_code = '''
from graftpunk.plugins import SitePlugin, command

class TestPlugin(SitePlugin):
    site_name = "testplugin"
    session_name = "test"
    help_text = "Test plugin"

    @command(help="Test command")
    def test_cmd(self, session):
        return {"test": True}
'''
        (plugins_dir / "test_plugin.py").write_text(plugin_code)

        result = discover_python_plugins()
        assert len(result.plugins) == 1
        assert result.plugins[0].site_name == "testplugin"
        assert result.errors == []

    def test_discover_skips_underscore_files(self, isolated_config: Path) -> None:
        """Test that files starting with _ are skipped."""
        from graftpunk.plugins.python_loader import discover_python_plugins

        plugins_dir = isolated_config / "plugins"
        plugins_dir.mkdir()

        # Create files that should be skipped
        (plugins_dir / "__init__.py").write_text("")
        (plugins_dir / "_helper.py").write_text("x = 1")

        result = discover_python_plugins()
        assert result.plugins == []
        assert result.errors == []

    def test_discover_handles_import_error(self, isolated_config: Path) -> None:
        """Test that import errors are collected, not raised."""
        from graftpunk.plugins.python_loader import discover_python_plugins

        plugins_dir = isolated_config / "plugins"
        plugins_dir.mkdir()

        # Create a plugin with syntax error
        (plugins_dir / "bad_plugin.py").write_text("this is not valid python!")

        result = discover_python_plugins()
        assert result.plugins == []
        assert result.has_errors
        assert len(result.errors) == 1
        assert "bad_plugin.py" in str(result.errors[0].filepath)

    def test_discover_multiple_plugins_in_file(self, isolated_config: Path) -> None:
        """Test that multiple SitePlugin subclasses in one file are all discovered."""
        from graftpunk.plugins.python_loader import discover_python_plugins

        plugins_dir = isolated_config / "plugins"
        plugins_dir.mkdir()

        plugin_code = '''
from graftpunk.plugins import SitePlugin, command

class PluginA(SitePlugin):
    site_name = "plugin_a"
    session_name = ""
    help_text = "Plugin A"

class PluginB(SitePlugin):
    site_name = "plugin_b"
    session_name = ""
    help_text = "Plugin B"
'''
        (plugins_dir / "multi.py").write_text(plugin_code)

        result = discover_python_plugins()
        assert len(result.plugins) == 2
        site_names = {p.site_name for p in result.plugins}
        assert site_names == {"plugin_a", "plugin_b"}

    def test_discover_skips_non_siteplugin_classes(self, isolated_config: Path) -> None:
        """Test that classes not inheriting SitePlugin are skipped."""
        from graftpunk.plugins.python_loader import discover_python_plugins

        plugins_dir = isolated_config / "plugins"
        plugins_dir.mkdir()

        plugin_code = '''
class NotAPlugin:
    site_name = "fake"

class AlsoNotAPlugin:
    pass
'''
        (plugins_dir / "not_plugins.py").write_text(plugin_code)

        result = discover_python_plugins()
        assert result.plugins == []
        assert result.errors == []
```

### Step 2: Run test to verify it fails

```bash
pytest tests/unit/test_python_loader.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'graftpunk.plugins.python_loader'`

### Step 3: Write minimal implementation

Create `src/graftpunk/plugins/python_loader.py`:

```python
"""Python plugin loader for auto-discovery from plugins directory.

This module handles loading Python plugins from ~/.config/graftpunk/plugins/*.py
Similar to yaml_loader.py but for Python files containing SitePlugin subclasses.
"""

from __future__ import annotations

import importlib.util
import inspect
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from graftpunk.config import get_settings
from graftpunk.logging import get_logger
from graftpunk.plugins.cli_plugin import SitePlugin

if TYPE_CHECKING:
    from types import ModuleType

LOG = get_logger(__name__)


@dataclass
class PythonDiscoveryError:
    """Error encountered while loading a Python plugin file.

    Attributes:
        filepath: Path to the Python file that failed to load.
        error: Human-readable error message describing the failure.
    """

    filepath: Path
    error: str


@dataclass
class PythonDiscoveryResult:
    """Result of Python plugin discovery.

    Supports partial success: plugins that fail to load are recorded as
    errors while valid plugins are still returned.

    Attributes:
        plugins: Successfully loaded SitePlugin instances.
        errors: Errors for plugins that could not be loaded.
    """

    plugins: list[SitePlugin] = field(default_factory=list)
    errors: list[PythonDiscoveryError] = field(default_factory=list)

    @property
    def has_errors(self) -> bool:
        """Return True if any load errors occurred."""
        return bool(self.errors)


def _load_module_from_file(filepath: Path) -> ModuleType:
    """Load a Python module from a file path.

    Args:
        filepath: Path to the Python file.

    Returns:
        Loaded module object.

    Raises:
        Exception: If module cannot be loaded (syntax error, import error, etc.)
    """
    module_name = f"graftpunk_plugin_{filepath.stem}"
    spec = importlib.util.spec_from_file_location(module_name, filepath)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot create module spec for {filepath}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _find_siteplugin_classes(module: ModuleType) -> list[type[SitePlugin]]:
    """Find all SitePlugin subclasses in a module.

    Args:
        module: Loaded Python module to inspect.

    Returns:
        List of SitePlugin subclass types (not instances).
    """
    plugin_classes: list[type[SitePlugin]] = []

    for name, obj in inspect.getmembers(module, inspect.isclass):
        # Skip imported classes (only want classes defined in this module)
        if obj.__module__ != module.__name__:
            continue

        # Skip SitePlugin itself
        if obj is SitePlugin:
            continue

        # Check if it's a subclass of SitePlugin
        if issubclass(obj, SitePlugin):
            plugin_classes.append(obj)

    return plugin_classes


def discover_python_plugins() -> PythonDiscoveryResult:
    """Discover all Python plugins in the config directory.

    Looks for *.py files in ~/.config/graftpunk/plugins/
    Skips files starting with underscore (e.g., __init__.py, _helpers.py).

    Supports partial success: valid plugins are returned even if some files
    fail to load. Check result.has_errors to see if any failures occurred.

    Returns:
        PythonDiscoveryResult containing loaded plugin instances and any errors.
    """
    settings = get_settings()
    plugins_dir = settings.config_dir / "plugins"

    if not plugins_dir.exists():
        LOG.debug("python_plugins_dir_not_found", path=str(plugins_dir))
        return PythonDiscoveryResult()

    result = PythonDiscoveryResult()

    for py_file in plugins_dir.glob("*.py"):
        # Skip files starting with underscore
        if py_file.name.startswith("_"):
            LOG.debug("python_plugin_skipped_underscore", path=str(py_file))
            continue

        try:
            module = _load_module_from_file(py_file)
            plugin_classes = _find_siteplugin_classes(module)

            for plugin_class in plugin_classes:
                try:
                    instance = plugin_class()
                    result.plugins.append(instance)
                    LOG.info(
                        "python_plugin_loaded",
                        site_name=instance.site_name,
                        path=str(py_file),
                    )
                except Exception as exc:
                    LOG.warning(
                        "python_plugin_instantiation_failed",
                        path=str(py_file),
                        class_name=plugin_class.__name__,
                        error=str(exc),
                    )
                    result.errors.append(
                        PythonDiscoveryError(
                            filepath=py_file,
                            error=f"Failed to instantiate {plugin_class.__name__}: {exc}",
                        )
                    )

        except Exception as exc:
            LOG.warning("python_plugin_load_failed", path=str(py_file), error=str(exc))
            result.errors.append(PythonDiscoveryError(filepath=py_file, error=str(exc)))

    return result
```

### Step 4: Run tests to verify they pass

```bash
pytest tests/unit/test_python_loader.py -v
```

Expected: All tests PASS

### Step 5: Update plugins/__init__.py exports

Edit `src/graftpunk/plugins/__init__.py` to add exports:

Add import:
```python
from graftpunk.plugins.python_loader import (
    PythonDiscoveryError,
    PythonDiscoveryResult,
    discover_python_plugins,
)
```

Add to `__all__`:
```python
    "PythonDiscoveryError",
    "PythonDiscoveryResult",
    "discover_python_plugins",
```

### Step 6: Run full test suite

```bash
pytest tests/ -v
```

Expected: All tests PASS

### Step 7: Commit

```bash
git add src/graftpunk/plugins/python_loader.py src/graftpunk/plugins/__init__.py tests/unit/test_python_loader.py
git commit -m "feat: add Python plugin auto-discovery

Discover SitePlugin subclasses from ~/.config/graftpunk/plugins/*.py
Mirrors yaml_loader.py pattern with PythonDiscoveryResult.
Skips files starting with underscore.

Co-Authored-By: stavxyz <hi@stav.xyz>"
```

---

## Task 5: Integrate Python discovery into plugin_commands.py

**Files:**
- Modify: `src/graftpunk/cli/plugin_commands.py:221-253`
- Modify: `tests/unit/test_plugin_commands.py`

### Step 1: Write the failing test

Add to `tests/unit/test_plugin_commands.py`:

```python
class TestPythonPluginDiscovery:
    """Tests for Python plugin auto-discovery integration."""

    def test_register_python_plugin_from_file(self, isolated_config: Path) -> None:
        """Test registering a Python plugin discovered from file."""
        from graftpunk.cli.plugin_commands import register_plugin_commands

        # Create a plugin file in the config plugins directory
        plugins_dir = isolated_config / "plugins"
        plugins_dir.mkdir()

        plugin_code = '''
from graftpunk.plugins import SitePlugin, command

class FilePlugin(SitePlugin):
    site_name = "fileplugin"
    session_name = ""
    help_text = "Plugin from file"

    @command(help="Test command")
    def test(self, session):
        return {"test": True}
'''
        (plugins_dir / "file_plugin.py").write_text(plugin_code)

        app = typer.Typer()
        registered = register_plugin_commands(app, notify_errors=False)

        assert "fileplugin" in registered
        assert registered["fileplugin"] == "Plugin from file"
```

### Step 2: Run test to verify it fails

```bash
pytest tests/unit/test_plugin_commands.py::TestPythonPluginDiscovery -v
```

Expected: FAIL (plugin not discovered because integration not done yet)

### Step 3: Update plugin_commands.py

Edit `src/graftpunk/cli/plugin_commands.py`:

Add import at top:
```python
from graftpunk.plugins.python_loader import discover_python_plugins
```

In `register_plugin_commands()`, after the YAML discovery section (around line 253), add:

```python
    # Discover Python plugins from files
    try:
        python_file_result = discover_python_plugins()
        all_plugins.extend(python_file_result.plugins)
        # Aggregate any Python file load errors
        for py_error in python_file_result.errors:
            result.add_error(str(py_error.filepath.name), py_error.error, "discovery")
    except Exception as exc:
        LOG.exception("python_file_plugin_discovery_failed", error=str(exc))
        result.add_error("python-file-plugins", str(exc), "discovery")
```

### Step 4: Run test to verify it passes

```bash
pytest tests/unit/test_plugin_commands.py::TestPythonPluginDiscovery -v
```

Expected: PASS

### Step 5: Run full test suite

```bash
pytest tests/ -v
```

Expected: All tests PASS

### Step 6: Commit

```bash
git add src/graftpunk/cli/plugin_commands.py tests/unit/test_plugin_commands.py
git commit -m "feat: integrate Python plugin auto-discovery into CLI

Plugins in ~/.config/graftpunk/plugins/*.py are now auto-discovered
alongside YAML plugins and entry-point plugins.

Co-Authored-By: stavxyz <hi@stav.xyz>"
```

---

## Task 6: Add quotes.py (Selenium test site plugin)

**Files:**
- Create: `examples/plugins/quotes.py`

**Step 1: Create the plugin file**

Create `examples/plugins/quotes.py`:

```python
"""Quotes to Scrape plugin - Selenium backend example.

This plugin demonstrates:
- Form-based login with Selenium
- Caching sessions for later API use
- The @command decorator for CLI commands

Site: https://quotes.toscrape.com
Auth: Any username/password works (it's a test site)

Usage:
    1. Symlink to plugins directory:
       ln -s $(pwd)/examples/plugins/quotes.py ~/.config/graftpunk/plugins/

    2. Log in (opens browser):
       python -c "
       from graftpunk.plugins.python_loader import discover_python_plugins
       plugins = discover_python_plugins().plugins
       plugin = next(p for p in plugins if p.site_name == 'quotes')
       plugin.login()
       "

    3. Use cached session:
       gp quotes list
       gp quotes list --page 2
"""

from graftpunk import BrowserSession, cache_session
from graftpunk.plugins import SitePlugin, command


class QuotesPlugin(SitePlugin):
    """Plugin for quotes.toscrape.com (test site, any credentials work)."""

    site_name = "quotes"
    session_name = "quotes"
    help_text = "Quotes to Scrape commands (test site)"

    base_url = "https://quotes.toscrape.com"

    def login(
        self, username: str = "testuser", password: str = "testpass"
    ) -> bool:
        """Log in to the site and cache the session.

        Args:
            username: Any username (site accepts anything).
            password: Any password (site accepts anything).

        Returns:
            True if login successful.
        """
        session = BrowserSession(backend="selenium", headless=False)
        session.driver.get(f"{self.base_url}/login")

        # Fill in login form
        session.driver.find_element("id", "username").send_keys(username)
        session.driver.find_element("id", "password").send_keys(password)
        session.driver.find_element("css selector", "input[type='submit']").click()

        # Verify login succeeded (page shows Logout link)
        session.driver.find_element("css selector", "a[href='/logout']")

        # Cache the authenticated session
        cache_session(session, self.session_name)
        session.quit()
        return True

    @command(help="List quotes from a page")
    def list(self, session, page: int = 1):
        """Fetch quotes from a page.

        Args:
            session: Authenticated requests session.
            page: Page number (default: 1).

        Returns:
            Dict with URL and status code.
        """
        response = session.get(f"{self.base_url}/page/{page}/")
        return {
            "url": response.url,
            "status": response.status_code,
            "page": page,
        }

    @command(help="Get a random quote")
    def random(self, session):
        """Fetch the random quote page.

        Args:
            session: Authenticated requests session.

        Returns:
            Dict with URL and status code.
        """
        response = session.get(f"{self.base_url}/random")
        return {
            "url": response.url,
            "status": response.status_code,
        }
```

**Step 2: Verify it parses correctly**

```bash
python -c "
from graftpunk.plugins.python_loader import discover_python_plugins
from pathlib import Path
import sys
sys.path.insert(0, 'examples/plugins')

# Manually load to verify syntax
import importlib.util
spec = importlib.util.spec_from_file_location('quotes', 'examples/plugins/quotes.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
print(f'Loaded: {module.QuotesPlugin.site_name}')
print(f'Commands: list, random')
"
```

**Step 3: Commit**

```bash
git add examples/plugins/quotes.py
git commit -m "feat: add quotes.py example plugin (Selenium)

Demonstrates form login with Selenium backend.
Uses quotes.toscrape.com (test site, any credentials work).

Co-Authored-By: stavxyz <hi@stav.xyz>"
```

---

## Task 7: Add hackernews.py (NoDriver real site plugin)

**Files:**
- Create: `examples/plugins/hackernews.py`

**Step 1: Create the plugin file**

Create `examples/plugins/hackernews.py`:

```python
"""Hacker News plugin - NoDriver backend example.

This plugin demonstrates:
- Form-based login with NoDriver (async)
- Real-world site integration
- Better anti-detection for modern sites

Site: https://news.ycombinator.com
Auth: Requires real Hacker News account

Usage:
    1. Symlink to plugins directory:
       ln -s $(pwd)/examples/plugins/hackernews.py ~/.config/graftpunk/plugins/

    2. Log in (opens browser, requires real credentials):
       python -c "
       import asyncio
       from graftpunk.plugins.python_loader import discover_python_plugins
       plugins = discover_python_plugins().plugins
       plugin = next(p for p in plugins if p.site_name == 'hn')
       asyncio.run(plugin.login('your_username', 'your_password'))
       "

    3. Use cached session:
       gp hn front
       gp hn front --page 2
       gp hn saved  # requires login
"""

import asyncio

from graftpunk import BrowserSession, cache_session
from graftpunk.plugins import SitePlugin, command


class HackerNewsPlugin(SitePlugin):
    """Plugin for Hacker News (news.ycombinator.com)."""

    site_name = "hn"
    session_name = "hackernews"
    help_text = "Hacker News commands"

    base_url = "https://news.ycombinator.com"

    async def login(self, username: str, password: str) -> bool:
        """Log in to Hacker News and cache the session.

        Uses NoDriver backend for better anti-detection.

        Args:
            username: Your Hacker News username.
            password: Your Hacker News password.

        Returns:
            True if login successful.
        """
        session = BrowserSession(backend="nodriver", headless=False)

        # NoDriver is async
        await session.driver.get(f"{self.base_url}/login")

        # Find and fill login form
        acct_field = await session.driver.select("input[name='acct']")
        await acct_field.send_keys(username)

        pw_field = await session.driver.select("input[name='pw']")
        await pw_field.send_keys(password)

        submit = await session.driver.select("input[value='login']")
        await submit.click()

        # Wait for login to complete (logout link appears)
        # Give it time since HN can be slow
        await asyncio.sleep(2)

        # Cache the session
        cache_session(session, self.session_name)
        await session.quit()
        return True

    @command(help="Get front page stories")
    def front(self, session, page: int = 1):
        """Fetch front page stories.

        Args:
            session: Authenticated requests session.
            page: Page number (default: 1).

        Returns:
            Dict with URL and status code.
        """
        response = session.get(f"{self.base_url}/news?p={page}")
        return {
            "url": response.url,
            "status": response.status_code,
            "page": page,
        }

    @command(help="Get newest stories")
    def newest(self, session, page: int = 1):
        """Fetch newest stories.

        Args:
            session: Authenticated requests session.
            page: Page number (default: 1).

        Returns:
            Dict with URL and status code.
        """
        response = session.get(f"{self.base_url}/newest?p={page}")
        return {
            "url": response.url,
            "status": response.status_code,
            "page": page,
        }

    @command(help="Get saved stories (requires login)")
    def saved(self, session):
        """Fetch your saved stories.

        Requires an authenticated session.

        Args:
            session: Authenticated requests session.

        Returns:
            Dict with URL and status code.
        """
        response = session.get(f"{self.base_url}/saved")
        return {
            "url": response.url,
            "status": response.status_code,
        }
```

**Step 2: Verify it parses correctly**

```bash
python -c "
import importlib.util
spec = importlib.util.spec_from_file_location('hackernews', 'examples/plugins/hackernews.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
print(f'Loaded: {module.HackerNewsPlugin.site_name}')
print(f'Session: {module.HackerNewsPlugin.session_name}')
"
```

**Step 3: Commit**

```bash
git add examples/plugins/hackernews.py
git commit -m "feat: add hackernews.py example plugin (NoDriver)

Demonstrates async login with NoDriver backend.
Real-world site requiring actual HN credentials.

Co-Authored-By: stavxyz <hi@stav.xyz>"
```

---

## Task 8: Add templates

**Files:**
- Create: `examples/templates/yaml_template.yaml`
- Create: `examples/templates/python_template.py`

**Step 1: Create YAML template**

Create `examples/templates/yaml_template.yaml`:

```yaml
# YAML Plugin Template
# Copy this file and customize for your site.
# Save or symlink to: ~/.config/graftpunk/plugins/<name>.yaml
#
# Documentation: https://github.com/stavxyz/graftpunk

site_name: mysite                    # CLI command group: gp mysite <cmd>
session_name: mysite                 # Cached session name (or "" for no session)
help: "Description of your plugin"
base_url: "https://example.com"      # Optional: prepended to command URLs

# Optional: Headers applied to all requests
# Environment variables use ${VAR} syntax
# headers:
#   Authorization: "Bearer ${MY_API_TOKEN}"
#   X-Custom-Header: "value"

commands:
  # Simple GET command
  example:
    help: "Description of this command"
    method: GET
    url: "/api/endpoint"

  # Command with URL parameter (positional argument)
  # get-item:
  #   help: "Get item by ID"
  #   method: GET
  #   url: "/api/items/{id}"
  #   params:
  #     - name: id
  #       type: int
  #       required: true
  #       help: "Item ID"
  #       is_option: false  # Makes it positional: gp mysite get-item 123

  # Command with query parameter (--option flag)
  # list:
  #   help: "List items with pagination"
  #   method: GET
  #   url: "/api/items"
  #   params:
  #     - name: page
  #       type: int
  #       required: false
  #       default: 1
  #       help: "Page number"

  # Command with jmespath extraction
  # users:
  #   help: "Get user names only"
  #   method: GET
  #   url: "/api/users"
  #   jmespath: "data[*].name"  # Extract just names from response
```

**Step 2: Create Python template**

Create `examples/templates/python_template.py`:

```python
"""Python Plugin Template.

Copy this file and customize for your site.
Save or symlink to: ~/.config/graftpunk/plugins/<name>.py

Documentation: https://github.com/stavxyz/graftpunk
"""

from graftpunk import BrowserSession, cache_session
from graftpunk.plugins import SitePlugin, command


class MySitePlugin(SitePlugin):
    """Plugin for MySite.

    Replace this with a description of what your plugin does.
    """

    # CLI command group: gp mysite <cmd>
    site_name = "mysite"

    # Cached session name (must match what you pass to cache_session)
    session_name = "mysite"

    # Help text shown in CLI
    help_text = "Description of your plugin"

    # Base URL for the site
    base_url = "https://example.com"

    def login(self, username: str, password: str) -> bool:
        """Log in to the site and cache the session.

        Customize this method for your site's login flow.

        Args:
            username: Your username.
            password: Your password.

        Returns:
            True if login successful.
        """
        # Choose backend: "selenium" or "nodriver"
        session = BrowserSession(backend="selenium", headless=False)
        session.driver.get(f"{self.base_url}/login")

        # Fill login form - customize selectors for your site
        # session.driver.find_element("id", "username").send_keys(username)
        # session.driver.find_element("id", "password").send_keys(password)
        # session.driver.find_element("id", "submit").click()

        # Wait for login to complete - customize selector
        # session.driver.find_element("css selector", "a[href='/logout']")

        # Cache the authenticated session
        cache_session(session, self.session_name)
        session.quit()
        return True

    @command(help="Example command")
    def example(self, session, param: str = "default"):
        """Example command that makes an API request.

        Args:
            session: Authenticated requests session (injected automatically).
            param: Example parameter with default value.

        Returns:
            Response data (will be formatted as JSON by default).
        """
        response = session.get(f"{self.base_url}/api/endpoint")
        return response.json()

    # Add more commands as needed:
    #
    # @command(help="List items")
    # def list(self, session, page: int = 1):
    #     response = session.get(f"{self.base_url}/api/items?page={page}")
    #     return response.json()
    #
    # @command(help="Get item by ID")
    # def get(self, session, item_id: int):
    #     response = session.get(f"{self.base_url}/api/items/{item_id}")
    #     return response.json()
```

**Step 3: Commit**

```bash
git add examples/templates/
git commit -m "feat: add plugin templates

YAML and Python templates for users to copy and customize.
Includes inline documentation and common patterns.

Co-Authored-By: stavxyz <hi@stav.xyz>"
```

---

## Task 9: Write examples/README.md

**Files:**
- Modify: `examples/README.md`

**Step 1: Write the full README**

Update `examples/README.md`:

```markdown
# graftpunk Examples

Example plugins and templates for building your own graftpunk integrations.

## Quick Start

1. Symlink an example to your plugins directory:

   ```bash
   mkdir -p ~/.config/graftpunk/plugins
   ln -s $(pwd)/examples/plugins/httpbin.yaml ~/.config/graftpunk/plugins/
   ```

2. Run it:

   ```bash
   gp httpbin ip
   gp httpbin headers
   gp httpbin status --code 418
   ```

## Examples

| Plugin | Type | Backend | Auth | Description |
|--------|------|---------|------|-------------|
| `httpbin.yaml` | YAML | — | None | Simplest example, no auth needed |
| `httpbin_auth.yaml` | YAML | — | Header | Environment variable injection |
| `quotes.py` | Python | Selenium | Form | Test site, any credentials work |
| `hackernews.py` | Python | NoDriver | Form | Real site, requires HN account |

### httpbin.yaml

The simplest possible plugin. No authentication, no session caching—just HTTP calls.

```bash
ln -s $(pwd)/examples/plugins/httpbin.yaml ~/.config/graftpunk/plugins/
gp httpbin ip
gp httpbin uuid
gp httpbin status --code 418  # I'm a teapot!
```

### httpbin_auth.yaml

Demonstrates header injection via environment variables.

```bash
ln -s $(pwd)/examples/plugins/httpbin_auth.yaml ~/.config/graftpunk/plugins/
export HTTPBIN_AUTH="dXNlcjpwYXNz"  # base64("user:pass")
gp httpbin-auth whoami
```

### quotes.py

Python plugin using Selenium for form login. Uses a test site that accepts any credentials.

```bash
ln -s $(pwd)/examples/plugins/quotes.py ~/.config/graftpunk/plugins/

# Log in (opens browser)
python -c "
from graftpunk.plugins.python_loader import discover_python_plugins
plugin = next(p for p in discover_python_plugins().plugins if p.site_name == 'quotes')
plugin.login()
"

# Use the cached session
gp quotes list
gp quotes list --page 2
```

### hackernews.py

Python plugin using NoDriver (better anti-detection) for a real site.

```bash
ln -s $(pwd)/examples/plugins/hackernews.py ~/.config/graftpunk/plugins/

# Log in (opens browser, requires real HN account)
python -c "
import asyncio
from graftpunk.plugins.python_loader import discover_python_plugins
plugin = next(p for p in discover_python_plugins().plugins if p.site_name == 'hn')
asyncio.run(plugin.login('your_username', 'your_password'))
"

# Use the cached session
gp hn front
gp hn newest
gp hn saved
```

## YAML vs Python

**Use YAML when:**
- Simple GET/POST to JSON APIs
- No login automation needed
- Quick prototyping

**Use Python when:**
- Login automation required
- Response parsing (BeautifulSoup, etc.)
- Complex conditional logic
- Multiple requests per command

## Creating Your Own

1. Copy a template from `templates/`:
   - `yaml_template.yaml` for YAML plugins
   - `python_template.py` for Python plugins

2. Save or symlink to `~/.config/graftpunk/plugins/`

3. Run `gp plugins` to verify discovery

## Plugin Discovery

graftpunk discovers plugins from three sources (in order):

1. **Entry points** — Python packages registered via `pyproject.toml`
2. **YAML files** — `~/.config/graftpunk/plugins/*.yaml`
3. **Python files** — `~/.config/graftpunk/plugins/*.py`

If two plugins have the same `site_name`, the first one wins.

## See Also

- [Main README](../README.md) — Full graftpunk documentation
- [Plugin System](../README.md#plugins) — Plugin architecture details
```

**Step 2: Commit**

```bash
git add examples/README.md
git commit -m "docs: write examples README with quick start guide

Covers all examples, YAML vs Python guidance,
and plugin discovery documentation.

Co-Authored-By: stavxyz <hi@stav.xyz>"
```

---

## Task 10: Run quality checks and full test suite

**Step 1: Run linting**

```bash
ruff check --fix .
ruff format .
```

**Step 2: Run type checking**

```bash
mypy src/
```

**Step 3: Run full test suite**

```bash
pytest tests/ -v
```

**Step 4: Fix any issues found**

Address any linting, type, or test failures.

**Step 5: Commit fixes (if any)**

```bash
git add -u
git commit -m "fix: address linting and type errors

Co-Authored-By: stavxyz <hi@stav.xyz>"
```

---

## Task 11: Manual end-to-end test

**Step 1: Symlink httpbin.yaml**

```bash
mkdir -p ~/.config/graftpunk/plugins
ln -s $(pwd)/examples/plugins/httpbin.yaml ~/.config/graftpunk/plugins/
```

**Step 2: Test commands**

```bash
gp httpbin ip
gp httpbin headers
gp httpbin status --code 418
gp httpbin uuid
```

**Step 3: Test plugins list**

```bash
gp plugins
```

Expected: Should show `httpbin` in the list.

**Step 4: Clean up symlink**

```bash
rm ~/.config/graftpunk/plugins/httpbin.yaml
```

---

## Task 12: Create pull request

**Step 1: Push branch**

```bash
git push -u origin feature/example-plugins
```

**Step 2: Create PR**

```bash
gh pr create --title "feat: example plugins and Python auto-discovery" --body "$(cat <<'EOF'
## Summary

Implements issue #5: example plugins and templates.

- Add example plugins (httpbin YAML, quotes/hackernews Python)
- Add Python plugin auto-discovery from `~/.config/graftpunk/plugins/*.py`
- Add plugin templates for users to copy

## Changes

- `examples/` — Example plugins and templates with README
- `src/graftpunk/plugins/python_loader.py` — Python plugin auto-discovery
- `src/graftpunk/cli/plugin_commands.py` — Integrate Python discovery
- `tests/unit/test_python_loader.py` — Tests for Python discovery

## Test plan

- [ ] `gp httpbin ip` works after symlinking
- [ ] `gp httpbin status --code 418` returns teapot
- [ ] `gp plugins` shows discovered plugins
- [ ] All tests pass

Closes #5
EOF
)"
```

---

## Acceptance Criteria

- [ ] `gp httpbin ip` works after symlinking example
- [ ] `gp httpbin status --code 418` returns 418 response
- [ ] Python plugins in `~/.config/graftpunk/plugins/*.py` are auto-discovered
- [ ] `gp plugins` shows all discovered plugins
- [ ] All tests pass
- [ ] Examples have comprehensive inline comments
- [ ] README explains each example clearly
