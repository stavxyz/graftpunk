# Plugin Config Unification Implementation Plan

**Goal:** Eliminate duplicated defaulting/validation logic between YAML and Python plugin paths by introducing a shared `PluginConfig` dataclass and `build_plugin_config()` factory.

**Architecture:** Both YAML and Python plugins produce a `PluginConfig` via `build_plugin_config()`, which handles all defaulting (site_name inference, session_name, help_text) and validation in one place. YAML plugins become dynamic `SitePlugin` subclasses — no more `YAMLSitePlugin` adapter. `YAMLPluginDef` and `YAMLLoginDef` are deleted.

**Tech Stack:** Python 3.13, dataclasses, pytest, ruff, ty

**Design doc:** `docs/plans/2026-02-01-plugin-config-unification-design.md`

**Quality checks after every task:**
```bash
uv run pytest tests/ -v --tb=short
uvx ruff check .
uvx ruff format .
uvx ty check src/
```

---

### Task 1: Add PluginConfig and build_plugin_config() to cli_plugin.py

**Files:**
- Modify: `src/graftpunk/plugins/cli_plugin.py`
- Test: `tests/unit/test_plugin_commands.py`

This task adds the new types without changing any existing behavior. Everything that exists today keeps working.

**Step 1: Write failing tests for build_plugin_config()**

Add to `tests/unit/test_plugin_commands.py` — a new test class `TestBuildPluginConfig`:

```python
class TestBuildPluginConfig:
    """Tests for build_plugin_config shared factory."""

    def test_minimal_config(self) -> None:
        """site_name is the only truly required field."""
        from graftpunk.plugins.cli_plugin import PluginConfig, build_plugin_config

        config = build_plugin_config(site_name="mysite")
        assert isinstance(config, PluginConfig)
        assert config.site_name == "mysite"
        assert config.session_name == "mysite"  # defaults to site_name
        assert config.help_text == "Commands for mysite"

    def test_explicit_session_name_preserved(self) -> None:
        """Explicit session_name is not overridden."""
        from graftpunk.plugins.cli_plugin import build_plugin_config

        config = build_plugin_config(site_name="hn", session_name="hackernews")
        assert config.session_name == "hackernews"

    def test_explicit_help_text_preserved(self) -> None:
        """Explicit help_text is not overridden."""
        from graftpunk.plugins.cli_plugin import build_plugin_config

        config = build_plugin_config(site_name="mysite", help_text="Custom help")
        assert config.help_text == "Custom help"

    def test_site_name_inferred_from_base_url(self) -> None:
        """site_name is inferred from base_url when not provided."""
        from graftpunk.plugins.cli_plugin import build_plugin_config

        config = build_plugin_config(base_url="https://httpbin.org")
        assert config.site_name == "httpbin"

    def test_missing_site_name_and_base_url_raises(self) -> None:
        """Raises PluginError when site_name cannot be determined."""
        from graftpunk.plugins.cli_plugin import build_plugin_config

        with pytest.raises(PluginError, match="site_name"):
            build_plugin_config()

    def test_unknown_kwargs_ignored(self) -> None:
        """Extra kwargs not in PluginConfig fields are silently ignored."""
        from graftpunk.plugins.cli_plugin import build_plugin_config

        config = build_plugin_config(site_name="mysite", commands=[], headers={}, unknown_field=42)
        assert config.site_name == "mysite"

    def test_requires_session_default_true(self) -> None:
        """requires_session defaults to True."""
        from graftpunk.plugins.cli_plugin import build_plugin_config

        config = build_plugin_config(site_name="mysite")
        assert config.requires_session is True

    def test_requires_session_explicit_false(self) -> None:
        """requires_session can be set to False."""
        from graftpunk.plugins.cli_plugin import build_plugin_config

        config = build_plugin_config(site_name="mysite", requires_session=False)
        assert config.requires_session is False

    def test_login_fields_preserved(self) -> None:
        """Login fields are stored on PluginConfig."""
        from graftpunk.plugins.cli_plugin import build_plugin_config

        config = build_plugin_config(
            site_name="mysite",
            login_url="/login",
            login_fields={"username": "#user", "password": "#pass"},
            login_submit="#submit",
        )
        assert config.login_url == "/login"
        assert config.login_fields == {"username": "#user", "password": "#pass"}
        assert config.login_submit == "#submit"

    def test_empty_site_name_string_raises(self) -> None:
        """Empty string site_name raises after inference attempt."""
        from graftpunk.plugins.cli_plugin import build_plugin_config

        with pytest.raises(PluginError, match="site_name"):
            build_plugin_config(site_name="")
```

Also add this import at the top of the file if not present:
```python
from graftpunk.exceptions import PluginError
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_plugin_commands.py::TestBuildPluginConfig -v`
Expected: FAIL (PluginConfig and build_plugin_config don't exist yet)

**Step 3: Implement PluginConfig and build_plugin_config()**

Add to `src/graftpunk/plugins/cli_plugin.py`, after the `CommandSpec` dataclass (line 59) and before `CLIPluginProtocol`:

```python
import dataclasses


@dataclass
class PluginConfig:
    """Canonical plugin configuration produced by both YAML and Python paths.

    Use build_plugin_config() to create instances with proper defaulting
    and validation instead of constructing directly.
    """

    site_name: str
    session_name: str
    help_text: str
    base_url: str = ""
    requires_session: bool = True
    backend: str = "selenium"
    username_envvar: str = ""
    password_envvar: str = ""
    login_url: str = ""
    login_fields: dict[str, str] = field(default_factory=dict)
    login_submit: str = ""
    login_failure: str = ""
    login_success: str = ""


def build_plugin_config(**raw: Any) -> PluginConfig:
    """Build a PluginConfig with shared defaulting and validation.

    Accepts arbitrary kwargs, filters to known PluginConfig fields,
    applies defaults (site_name inference, session_name, help_text),
    and validates.

    This is the single place where plugin configuration defaults and
    validation happen, shared by both YAML and Python plugin paths.

    Args:
        **raw: Plugin configuration values. Unknown keys are ignored.

    Returns:
        Validated PluginConfig.

    Raises:
        PluginError: If site_name cannot be determined.
    """
    from graftpunk.plugins import infer_site_name

    # Filter to known fields
    known_fields = {f.name for f in dataclasses.fields(PluginConfig)}
    filtered = {k: v for k, v in raw.items() if k in known_fields}

    # Infer site_name from base_url if not provided
    site_name = filtered.get("site_name") or ""
    if not site_name:
        base_url = filtered.get("base_url") or ""
        if base_url:
            site_name = infer_site_name(base_url)
    if not site_name:
        raise PluginError(
            "Plugin missing 'site_name'. Set site_name explicitly "
            "or provide base_url to auto-infer it from the domain."
        )
    filtered["site_name"] = site_name

    # Default session_name to site_name
    if not filtered.get("session_name"):
        filtered["session_name"] = site_name

    # Default help_text
    if not filtered.get("help_text"):
        filtered["help_text"] = f"Commands for {site_name}"

    return PluginConfig(**filtered)
```

Also add the import at the top of the file:
```python
import dataclasses
```

And add the `PluginError` import:
```python
from graftpunk.exceptions import PluginError
```

**Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_plugin_commands.py::TestBuildPluginConfig -v`
Expected: all PASS

**Step 5: Run full quality checks**

```bash
uv run pytest tests/ -v --tb=short
uvx ruff check .
uvx ruff format .
uvx ty check src/
```

**Step 6: Commit**

```bash
git add src/graftpunk/plugins/cli_plugin.py tests/unit/test_plugin_commands.py
git commit -m "feat: add PluginConfig dataclass and build_plugin_config() factory"
```

---

### Task 2: Wire SitePlugin.__init_subclass__ to use build_plugin_config()

**Files:**
- Modify: `src/graftpunk/plugins/cli_plugin.py`
- Test: `tests/unit/test_plugin_commands.py`

Replace the ad-hoc defaulting logic in `__init_subclass__` with a `build_plugin_config()` call. Write back inferred values to class attributes so the external API doesn't change.

**Step 1: Write failing tests**

Add to `tests/unit/test_plugin_commands.py`, in the existing `TestSiteNameAutoInference` class (or a new `TestSitePluginConfigIntegration` class):

```python
class TestSitePluginConfigIntegration:
    """Tests that SitePlugin.__init_subclass__ uses build_plugin_config."""

    def test_session_name_defaults_to_site_name(self) -> None:
        """Python plugins get session_name defaulted to site_name."""
        class MyPlugin(SitePlugin):
            site_name = "mysite"

        assert MyPlugin.session_name == "mysite"

    def test_help_text_auto_generated(self) -> None:
        """Python plugins get help_text auto-generated."""
        class MyPlugin(SitePlugin):
            site_name = "mysite"

        assert MyPlugin.help_text == "Commands for mysite"

    def test_explicit_session_name_not_overridden(self) -> None:
        """Explicit session_name is preserved."""
        class MyPlugin(SitePlugin):
            site_name = "hn"
            session_name = "hackernews"

        assert MyPlugin.session_name == "hackernews"

    def test_explicit_help_text_not_overridden(self) -> None:
        """Explicit help_text is preserved."""
        class MyPlugin(SitePlugin):
            site_name = "mysite"
            help_text = "Custom help"

        assert MyPlugin.help_text == "Custom help"

    def test_plugin_config_stored(self) -> None:
        """_plugin_config is stored on the class."""
        from graftpunk.plugins.cli_plugin import PluginConfig

        class MyPlugin(SitePlugin):
            site_name = "mysite"

        assert isinstance(MyPlugin._plugin_config, PluginConfig)
        assert MyPlugin._plugin_config.site_name == "mysite"

    def test_site_name_inferred_from_base_url(self) -> None:
        """site_name inferred from base_url (existing behavior preserved)."""
        class MyPlugin(SitePlugin):
            base_url = "https://httpbin.org"

        assert MyPlugin.site_name == "httpbin"
        assert MyPlugin.session_name == "httpbin"
        assert MyPlugin.help_text == "Commands for httpbin"
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_plugin_commands.py::TestSitePluginConfigIntegration -v`
Expected: FAIL (session_name still defaults to "", help_text still defaults to "")

**Step 3: Update __init_subclass__ to use build_plugin_config()**

In `src/graftpunk/plugins/cli_plugin.py`, replace the current `__init_subclass__` method (lines 158-172) with:

```python
    def __init_subclass__(cls, **kwargs: Any) -> None:
        """Apply shared defaults and store canonical config.

        Calls build_plugin_config() to handle site_name inference,
        session_name defaulting, help_text generation, and validation.
        Writes inferred values back to class attributes.
        """
        super().__init_subclass__(**kwargs)
        if "login_fields" not in cls.__dict__:
            cls.login_fields = {}

        # Extract non-private, non-callable class attributes
        raw = {
            k: v
            for k, v in cls.__dict__.items()
            if not k.startswith("_") and not callable(v) and not isinstance(v, (classmethod, staticmethod, property))
        }

        # Build config (may fail for incomplete plugins — that's OK,
        # they'll be caught at registration time)
        try:
            cls._plugin_config = build_plugin_config(**raw)
            # Write back inferred/defaulted values
            cls.site_name = cls._plugin_config.site_name
            cls.session_name = cls._plugin_config.session_name
            cls.help_text = cls._plugin_config.help_text
        except PluginError:
            # Plugin has no site_name or base_url yet — that's fine,
            # it will be caught at registration time
            pass
```

**Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_plugin_commands.py::TestSitePluginConfigIntegration -v`
Expected: all PASS

Then run the full suite to verify nothing is broken:
```bash
uv run pytest tests/ -v --tb=short
```

**Step 5: Commit**

```bash
git add src/graftpunk/plugins/cli_plugin.py tests/unit/test_plugin_commands.py
git commit -m "refactor: wire SitePlugin.__init_subclass__ to build_plugin_config()"
```

---

### Task 3: Rewrite yaml_loader.py to produce PluginConfig

**Files:**
- Modify: `src/graftpunk/plugins/yaml_loader.py`
- Test: `tests/unit/test_yaml_loader.py`

Delete `YAMLPluginDef`, `YAMLLoginDef`. Simplify `parse_yaml_plugin()` to normalize the YAML dict (including flattening nested `login:` blocks) and call `build_plugin_config()`. Keep `YAMLCommandDef` (it's YAML-specific command data). Keep `expand_env_vars`, `validate_yaml_schema`, discovery types.

`parse_yaml_plugin()` now returns a `tuple[PluginConfig, list[YAMLCommandDef]]` instead of `YAMLPluginDef`.

**Step 1: Update tests in test_yaml_loader.py**

All tests that use `parse_yaml_plugin` currently expect a `YAMLPluginDef` return. Update them to expect a `(PluginConfig, commands)` tuple.

The key changes to every `parse_yaml_plugin` call site:

Before: `plugin = parse_yaml_plugin(yaml_file)`; `plugin.site_name`, `plugin.session_name`, etc.

After: `config, commands = parse_yaml_plugin(yaml_file)`; `config.site_name`, `config.session_name`, etc.

For tests that check commands: `commands[0].name`, `commands[0].url`, etc.

Also update imports: replace `YAMLPluginDef` with `PluginConfig` from `graftpunk.plugins.cli_plugin`.

For tests that check login fields: they're now flat on `config` (`config.login_url`, `config.login_fields`), not nested (`plugin.login.url`).

Add a new test for nested-to-flat login normalization:

```python
def test_parse_nested_login_block(self, tmp_path: Path) -> None:
    """Nested login: block is flattened to login_url/login_fields/login_submit."""
    yaml_content = """
site_name: mysite
base_url: "https://example.com"
login:
  url: "/login"
  fields:
    username: "#user"
    password: "#pass"
  submit: "#submit"
  failure: "Bad login"
commands:
  cmd:
    url: "/api"
"""
    yaml_file = tmp_path / "test.yaml"
    yaml_file.write_text(yaml_content)

    config, commands = parse_yaml_plugin(yaml_file)

    assert config.login_url == "/login"
    assert config.login_fields == {"username": "#user", "password": "#pass"}
    assert config.login_submit == "#submit"
    assert config.login_failure == "Bad login"
```

Add a test for flat login fields:

```python
def test_parse_flat_login_fields(self, tmp_path: Path) -> None:
    """Flat login_url/login_fields work directly."""
    yaml_content = """
site_name: mysite
base_url: "https://example.com"
login_url: "/login"
login_fields:
  username: "#user"
  password: "#pass"
login_submit: "#submit"
commands:
  cmd:
    url: "/api"
"""
    yaml_file = tmp_path / "test.yaml"
    yaml_file.write_text(yaml_content)

    config, commands = parse_yaml_plugin(yaml_file)

    assert config.login_url == "/login"
    assert config.login_fields == {"username": "#user", "password": "#pass"}
    assert config.login_submit == "#submit"
```

**Step 2: Rewrite yaml_loader.py**

Delete `YAMLPluginDef` and `YAMLLoginDef` dataclasses. Keep `YAMLCommandDef`, `YAMLDiscoveryError`, `YAMLDiscoveryResult`, `expand_env_vars`, `VALID_METHODS`, `ENV_VAR_PATTERN`.

Update `validate_yaml_schema()`: remove the site_name inference code (that's now in `build_plugin_config()`). Keep YAML-specific validations only: commands must exist, commands must be dict, each command must have url, valid method, valid params.

For site_name validation in `validate_yaml_schema`, simplify to just checking the type if present (not requiring it — `build_plugin_config` handles the required check):

```python
if "site_name" in data:
    site_name = data["site_name"]
    if not isinstance(site_name, str) or not site_name.strip():
        raise PluginError(f"Plugin '{filepath}': 'site_name' must be a non-empty string.")
```

Rewrite `parse_yaml_plugin()` to return `tuple[PluginConfig, list[YAMLCommandDef]]`:

```python
def parse_yaml_plugin(filepath: Path) -> tuple[PluginConfig, list[YAMLCommandDef]]:
```

The body:
1. Read YAML (same as today)
2. Call `validate_yaml_schema(data, filepath)`
3. Parse command defs into `list[YAMLCommandDef]` (same as today)
4. Normalize nested `login:` block to flat fields:
   ```python
   login_data = data.pop("login", None)
   if login_data and isinstance(login_data, dict):
       # Validate required login fields
       missing = [f for f in ("url", "fields", "submit") if not login_data.get(f)]
       if missing:
           raise PluginError(...)
       data.setdefault("login_url", login_data["url"])
       data.setdefault("login_fields", login_data["fields"])
       data.setdefault("login_submit", login_data["submit"])
       data.setdefault("login_failure", login_data.get("failure", ""))
       data.setdefault("login_success", login_data.get("success", ""))
   elif login_data is not None:
       raise PluginError(f"Plugin '{filepath}': 'login' must be a mapping, ...")
   ```
5. Parse headers (same as today), store in data dict
6. Pop YAML-only keys that aren't PluginConfig fields: `commands`, `headers` (headers are plugin-level headers for YAML request handlers, stored on `YAMLCommandDef` closures, not on `PluginConfig`)
7. Rename `help` → `help_text` if present: `data["help_text"] = data.pop("help", "")`
8. Call `build_plugin_config(**data)` → `PluginConfig`
9. Return `(config, commands)`

Update `YAMLDiscoveryResult` to store `tuple[PluginConfig, list[YAMLCommandDef]]` instead of `YAMLPluginDef`:

```python
@dataclass
class YAMLDiscoveryResult:
    plugins: list[tuple[PluginConfig, list[YAMLCommandDef]]] = field(default_factory=list)
    errors: list[YAMLDiscoveryError] = field(default_factory=list)
```

Update `discover_yaml_plugins()` accordingly.

**Step 3: Run tests**

```bash
uv run pytest tests/unit/test_yaml_loader.py -v --tb=short
```

Fix any remaining test failures.

**Step 4: Commit**

```bash
git add src/graftpunk/plugins/yaml_loader.py tests/unit/test_yaml_loader.py
git commit -m "refactor: yaml_loader produces PluginConfig, delete YAMLPluginDef/YAMLLoginDef"
```

---

### Task 4: Rewrite yaml_plugin.py — delete YAMLSitePlugin, add dynamic subclass factory

**Files:**
- Modify: `src/graftpunk/plugins/yaml_plugin.py`
- Test: `tests/unit/test_yaml_plugin.py`

Delete `YAMLSitePlugin`. Move its HTTP handler closure logic (`_create_handler`, `_convert_params`) into module-level functions. Add a `create_yaml_site_plugin()` factory that creates a dynamic `SitePlugin` subclass. Update `create_yaml_plugins()` to return `list[SitePlugin]`.

**Step 1: Update test_yaml_plugin.py**

All `YAMLSitePlugin` instantiations become calls through the new factory or direct `SitePlugin` interactions. Update the `_make_plugin_def` helper to use `PluginConfig` and `YAMLCommandDef` directly.

Replace `_make_plugin_def` helper:

```python
from graftpunk.plugins.cli_plugin import PluginConfig, build_plugin_config
from graftpunk.plugins.yaml_loader import YAMLCommandDef

def _make_config(**kwargs: Any) -> PluginConfig:
    """Helper to create a PluginConfig with sensible defaults."""
    defaults = {"site_name": "testsite", "session_name": "testsession", "base_url": "https://api.example.com"}
    defaults.update(kwargs)
    return build_plugin_config(**defaults)
```

Replace `YAMLSitePlugin(plugin_def)` with calls to `create_yaml_site_plugin(config, commands)`.

For handler/URL/param tests, continue testing through the command handlers returned by `get_commands()`.

**Step 2: Rewrite yaml_plugin.py**

Keep at the top:
- `URL_PARAM_PATTERN`
- jmespath import logic (`_jmespath`, `HAS_JMESPATH`)
- `expand_env_vars` import
- `LOG`

Delete the entire `YAMLSitePlugin` class.

Add module-level functions extracted from `YAMLSitePlugin`:

```python
def _convert_params(cmd_def: YAMLCommandDef) -> list[ParamSpec]:
    """Convert YAML param definitions to ParamSpec objects."""
    # Same body as the old YAMLSitePlugin._convert_params, but takes
    # cmd_def directly instead of self
    ...

def _create_handler(cmd_def: YAMLCommandDef, base_url: str, plugin_headers: dict[str, str]) -> Any:
    """Create a callable handler for a YAML command."""
    # Same body as old YAMLSitePlugin._create_handler, but takes
    # base_url and plugin_headers as explicit args instead of self._def
    ...
```

Add the dynamic subclass factory:

```python
def create_yaml_site_plugin(
    config: PluginConfig,
    commands: list[YAMLCommandDef],
    plugin_headers: dict[str, str] | None = None,
) -> SitePlugin:
    """Create a SitePlugin instance from YAML-parsed config and commands.

    Dynamically creates a SitePlugin subclass with get_commands()
    returning CommandSpecs built from the YAML command definitions.
    """
    headers = plugin_headers or {}

    # Build CommandSpec list
    command_specs: dict[str, CommandSpec] = {}
    for cmd_def in commands:
        handler = _create_handler(cmd_def, config.base_url, headers)
        params = _convert_params(cmd_def)
        command_specs[cmd_def.name] = CommandSpec(
            name=cmd_def.name,
            handler=handler,
            help_text=cmd_def.help_text,
            params=params,
        )

    # Create dynamic subclass — __init_subclass__ fires and calls
    # build_plugin_config() again (idempotent, values already defaulted)
    attrs: dict[str, Any] = {
        k: v for k, v in dataclasses.asdict(config).items()
    }

    def get_commands(self: Any) -> dict[str, CommandSpec]:
        return command_specs

    attrs["get_commands"] = get_commands

    plugin_class = type(f"YAMLPlugin_{config.site_name}", (SitePlugin,), attrs)
    return plugin_class()
```

Update `create_yaml_plugins()`:

```python
def create_yaml_plugins() -> tuple[list[SitePlugin], list[YAMLDiscoveryError]]:
    """Create SitePlugin instances from discovered YAML files."""
    discovery_result = discover_yaml_plugins()
    plugins = []
    for config, commands in discovery_result.plugins:
        # Headers are stored separately (YAML-specific, for request handlers)
        # We need to re-read them — they were parsed alongside the config
        plugins.append(create_yaml_site_plugin(config, commands))
    return plugins, discovery_result.errors
```

**Note on headers:** The YAML `headers:` field is plugin-level HTTP headers used in request handlers, not a `PluginConfig` field. The parser needs to pass them through. Options:
1. Return them as a third element in the tuple from `parse_yaml_plugin()`: `tuple[PluginConfig, list[YAMLCommandDef], dict[str, str]]`
2. Store them on each `YAMLCommandDef` (they're already merged at handler creation time)

Option 1 is cleaner — update `parse_yaml_plugin` to return `tuple[PluginConfig, list[YAMLCommandDef], dict[str, str]]` and `YAMLDiscoveryResult.plugins` to `list[tuple[PluginConfig, list[YAMLCommandDef], dict[str, str]]]`.

**Step 3: Run tests**

```bash
uv run pytest tests/unit/test_yaml_plugin.py -v --tb=short
uv run pytest tests/ -v --tb=short
```

**Step 4: Commit**

```bash
git add src/graftpunk/plugins/yaml_plugin.py tests/unit/test_yaml_plugin.py
git commit -m "refactor: delete YAMLSitePlugin, add dynamic SitePlugin factory"
```

---

### Task 5: Update plugin_commands.py and main.py imports

**Files:**
- Modify: `src/graftpunk/cli/plugin_commands.py`
- Modify: `src/graftpunk/cli/main.py`
- Test: `tests/unit/test_plugin_commands.py`
- Test: `tests/unit/test_cli.py`

Remove `YAMLSitePlugin` imports. Update `create_yaml_plugins` import path and usage. Since YAML plugins are now `SitePlugin` instances, no type-specific handling is needed anywhere.

**Step 1: Update plugin_commands.py**

Change import:
```python
# Before:
from graftpunk.plugins.yaml_plugin import YAMLSitePlugin, create_yaml_plugins

# After:
from graftpunk.plugins.yaml_plugin import create_yaml_plugins
```

Remove any remaining `YAMLSitePlugin` references (there shouldn't be any after the earlier `requires_session` refactor, but double-check).

**Step 2: Update main.py**

The `plugins` command calls `create_yaml_plugins()` and iterates results. Update to work with `SitePlugin` instances (which have `.site_name` just like before):

```python
# Should work as-is since SitePlugin instances have .site_name
yaml_plugins, _ = create_yaml_plugins()
for plugin in yaml_plugins:
    cli_plugin_names.add(plugin.site_name)
```

**Step 3: Update test_plugin_commands.py**

Update all test methods that import `YAMLPluginDef` or `YAMLSitePlugin`:
- Replace `YAMLPluginDef(...)` with `build_plugin_config(...)` + `create_yaml_site_plugin(...)`
- Or simplify tests to use `SitePlugin` subclasses directly where YAML-specific behavior isn't being tested

In `TestYAMLSitePlugin`: these tests were testing the adapter. Since the adapter is gone, rework them to test through `create_yaml_site_plugin()` or delete if redundant with `TestBuildPluginConfig`.

In `TestCreateYamlPlugins`: update to expect `SitePlugin` instances.

**Step 4: Update test_cli.py**

Mocks for `create_yaml_plugins` return `SitePlugin` instances now. Update mock setup:

```python
# Before: mock yaml_plugin with .site_name property
# After: use a real SitePlugin subclass or mock with the same shape
```

**Step 5: Run full test suite**

```bash
uv run pytest tests/ -v --tb=short
uvx ruff check .
uvx ruff format .
uvx ty check src/
```

**Step 6: Commit**

```bash
git add src/graftpunk/cli/plugin_commands.py src/graftpunk/cli/main.py tests/unit/test_plugin_commands.py tests/unit/test_cli.py
git commit -m "refactor: remove YAMLSitePlugin imports, unify plugin types"
```

---

### Task 6: Update plugins/__init__.py exports and clean up

**Files:**
- Modify: `src/graftpunk/plugins/__init__.py`
- Verify: no remaining references to deleted types

**Step 1: Update exports**

Add `PluginConfig` and `build_plugin_config` to `__all__` and imports:

```python
from graftpunk.plugins.cli_plugin import (
    CLIPluginProtocol,
    CommandSpec,
    ParamSpec,
    PluginConfig,
    SitePlugin,
    build_plugin_config,
    command,
)

__all__ = [
    # Base classes and decorators
    "SitePlugin",
    "command",
    "CLIPluginProtocol",
    "CommandSpec",
    "ParamSpec",
    "PluginConfig",
    "build_plugin_config",
    # Utilities
    "infer_site_name",
    ...
]
```

**Step 2: Verify no remaining references to deleted types**

Search for any remaining references to `YAMLPluginDef`, `YAMLLoginDef`, `YAMLSitePlugin` across the entire codebase:

```bash
grep -r "YAMLPluginDef\|YAMLLoginDef\|YAMLSitePlugin" src/ tests/
```

Fix any remaining references.

**Step 3: Run full quality checks**

```bash
uv run pytest tests/ -v --tb=short
uvx ruff check .
uvx ruff format .
uvx ty check src/
```

**Step 4: Commit**

```bash
git add src/graftpunk/plugins/__init__.py
git commit -m "refactor: export PluginConfig and build_plugin_config from plugins package"
```

---

### Task 7: Verify examples still work and update docs

**Files:**
- Verify: `examples/plugins/httpbin.yaml` (should work unchanged)
- Verify: `examples/plugins/hackernews.py` (should work unchanged)
- Verify: `examples/plugins/quotes.py` (should work unchanged)
- Modify: `docs/plans/2026-02-01-plugin-config-unification-design.md` (mark complete)

**Step 1: Run integration smoke tests**

```bash
# YAML plugin (httpbin)
gp httpbin ip
gp httpbin uuid
gp httpbin --help

# Python plugins (if symlinked)
gp hn --help
gp quotes --help
```

**Step 2: Run full quality checks one final time**

```bash
uv run pytest tests/ -v --tb=short
uvx ruff check .
uvx ruff format .
uvx ty check src/
```

**Step 3: Commit if any changes needed**

```bash
git add -A
git commit -m "docs: mark plugin config unification complete"
```
