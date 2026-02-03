# Plugin Interface v1 Final Design

**Goal:** Address all critical, strong, and future-now items from the long-term plugin interface review before locking in the v1 public API.

**Architecture:** Additive changes to existing frozen dataclasses, one breaking rename (`login` → `login_config`), a unified `@command` decorator that handles both commands and command groups, and entry-point-based custom formatters.

---

## Section 1: Core Type Changes

### C1 — API Version Negotiation

Add to `cli_plugin.py`:

```python
SUPPORTED_API_VERSIONS: frozenset[int] = frozenset({1})
```

Export from `plugins/__init__.py`.

**Registration check** in `register_plugin_commands`:
```python
if plugin.api_version not in SUPPORTED_API_VERSIONS:
    result.add_error(
        site_name,
        f"Plugin requires api_version {plugin.api_version}, "
        f"but graftpunk supports {sorted(SUPPORTED_API_VERSIONS)}. "
        f"Upgrade graftpunk or downgrade the plugin.",
        "registration",
    )
    continue
```

**Version branch pattern** in `_create_plugin_command` callback:
```python
if ctx.api_version == 1:
    result = _execute_with_limits(handler, ctx, spec, **kwargs)
else:
    # Future: v2+ behavior
    result = _execute_with_limits(handler, ctx, spec, **kwargs)
```

Both branches identical for now — establishes the pattern.

**Documentation:** Add to `HOW_IT_WORKS.md`: "api_version 1 means: synchronous handlers, CommandContext with session/base_url/config/observe, LoginConfig-based declarative login, list[CommandSpec] from get_commands()."

### C2 — CommandContext Expansion

Add fields to `CommandContext`:

```python
@dataclass(frozen=True)
class CommandContext:
    session: requests.Session
    plugin_name: str
    command_name: str
    api_version: int
    base_url: str = ""
    config: PluginConfig | None = None
    observe: ObservabilityContext = field(default_factory=NoOpObservabilityContext)
```

`base_url` gives YAML plugin handlers access to the plugin's base URL (they have no `self`). `config` gives full plugin configuration access for advanced use cases.

The framework populates both in `_create_plugin_command` when building the `CommandContext`.

### C3 — CommandError Exception

New exception in `exceptions.py`:

```python
class CommandError(PluginError):
    """Expected command failure with a user-facing message.

    Plugin authors raise this for anticipated errors (validation failures,
    business rule violations). The framework displays user_message cleanly
    without traceback.

    Example:
        raise CommandError("Amount must be positive")
    """

    def __init__(self, user_message: str) -> None:
        self.user_message = user_message
        super().__init__(user_message)
```

In `_create_plugin_command` callback, catch it distinctly:
```python
except CommandError as exc:
    gp_console.error(exc.user_message)
    raise SystemExit(1) from exc
except PluginError as exc:
    gp_console.error(f"Plugin error: {exc}")
    raise SystemExit(1) from exc
```

Export from `plugins/__init__.py` and `exceptions.py`.

### R2 — get_commands() Returns list[CommandSpec]

Change `CLIPluginProtocol`:
```python
def get_commands(self) -> list[CommandSpec]: ...
```

Change `SitePlugin.get_commands()` to return `list[CommandSpec]`.

Framework builds the name→spec dict internally in `register_plugin_commands`.

### R3 — Per-Command requires_session

Add to `CommandSpec`:
```python
@dataclass(frozen=True)
class CommandSpec:
    # ... existing fields ...
    requires_session: bool | None = None  # None = inherit from plugin
```

In `_create_plugin_command` callback:
```python
needs_session = spec.requires_session if spec.requires_session is not None else plugin.requires_session
if needs_session:
    session = plugin.get_session()
else:
    session = requests.Session()
```

Add to `@command` decorator:
```python
def command(
    help: str = "",
    params: list[PluginParamSpec] | None = None,
    parent: type | None = None,
    requires_session: bool | None = None,
) -> ...:
```

### R7 — Plugin Metadata

Add optional fields to `PluginConfig`:
```python
@dataclass(frozen=True)
class PluginConfig:
    # ... existing fields ...
    plugin_version: str = ""
    plugin_author: str = ""
    plugin_url: str = ""
```

Add to `build_plugin_config()` known fields. Surface in `gp plugins` listing when populated.

YAML plugins can set these as top-level keys:
```yaml
site_name: mysite
plugin_version: "1.0.0"
plugin_author: "Jane Doe"
plugin_url: "https://github.com/jane/gp-mysite"
```

---

## Section 2: login → login_config Rename (R1)

Rename the config attribute everywhere to eliminate the name collision between `LoginConfig` data and `def login()` method.

### Files to modify:

**`cli_plugin.py`:**
- `CLIPluginProtocol.login` property → `login_config: LoginConfig | None`
- `SitePlugin.login` class attr → `login_config: LoginConfig | None = None`
- `PluginConfig.login` field → `login_config: LoginConfig | None = None`
- `__init_subclass__`: `cls.login = LoginConfig(...)` → `cls.login_config = LoginConfig(...)`
- `build_plugin_config()`: `filtered["login"]` → `filtered["login_config"]`, pop key adjusted
- `has_declarative_login()`: check `plugin.login_config`

**`login_engine.py`:**
- `plugin.login.url` → `plugin.login_config.url` (and `.fields`, `.submit`, `.failure`, `.success`)
- `plugin.login is None` → `plugin.login_config is None`

**`plugin_commands.py`:**
- `_has_login_method`: simplified to just `callable(getattr(plugin, "login", None))` — `login` is now *only* a method when present, never config
- Login registration: `plugin.login_config.fields`
- `_create_login_command`: receives fields from caller (already refactored)

**`yaml_loader.py`:**
- `login_config=login_config` in `build_plugin_config()` call
- YAML key stays `login:` — loader maps to `login_config` internally

**`yaml_plugin.py`:**
- `attrs["login_config"] = config.login_config`

**`plugins/__init__.py`:**
- No change needed (LoginConfig export stays the same)

**Examples:**
- `hackernews.py`: `login = LoginConfig(...)` → `login_config = LoginConfig(...)`
- `quotes.py`: same
- `python_template.py`: same
- YAML files: unchanged (key is `login:`)

**Tests:** Every test referencing `plugin.login` as config → `plugin.login_config`. Tests for `def login()` methods unchanged.

---

## Section 3: Unified @command Decorator with Command Groups (F5)

### Design

One `@command` decorator, polymorphic on what it decorates:

**Decorating a function → command:**
```python
@command(help="Show dashboard")
def dashboard(self, ctx: CommandContext): ...    # gp mysite dashboard
```

**Decorating a class → command group:**
```python
@command(help="Account operations")
class Accounts:
    def list(self, ctx: CommandContext): ...     # gp mysite accounts list
    def detail(self, ctx, id: int): ...         # gp mysite accounts detail --id 5
    def _helper(self): ...                       # skipped (underscore prefix)
```

All non-underscore methods become subcommands automatically. Use `@command` on individual methods only to customize help/params.

**`parent=` for nesting:**
```python
@command(help="Statements", parent=Accounts)
class Statements:
    def download(self, ctx): ...                # gp mysite accounts statements download

@command(help="Quick transfer", parent=Accounts)
def quick_transfer(self, ctx, amount: float): ...  # gp mysite accounts quick-transfer
```

Both classes and functions can use `parent=` to attach under a group.

### New metadata type

```python
@dataclass(frozen=True)
class CommandGroupMeta:
    """Metadata stored on @command-decorated classes."""
    name: str
    help_text: str
    parent: type | None = None
```

### Updated CommandMetadata

```python
@dataclass(frozen=True)
class CommandMetadata:
    name: str
    help_text: str
    params: tuple[PluginParamSpec, ...] = ()
    parent: type | None = None          # NEW: group attachment
```

### Updated CommandSpec

```python
@dataclass(frozen=True)
class CommandSpec:
    # ... existing fields ...
    group: str | None = None            # NEW: dotted path, e.g. "accounts.statements"
```

### Updated @command decorator

```python
def command(
    help: str = "",
    params: list[PluginParamSpec] | None = None,
    parent: type | None = None,
    requires_session: bool | None = None,
) -> Callable:
    def decorator(target):
        if isinstance(target, type):
            # Class → command group
            target._command_group_meta = CommandGroupMeta(
                name=_to_cli_name(target.__name__),
                help_text=help,
                parent=parent,
            )
            # Auto-discover methods: for each non-underscore callable,
            # if it doesn't already have _command_meta, attach default metadata
            for attr_name in dir(target):
                if attr_name.startswith("_"):
                    continue
                attr = getattr(target, attr_name, None)
                if callable(attr) and not hasattr(attr, "_command_meta"):
                    attr._command_meta = CommandMetadata(
                        name=attr_name,
                        help_text="",
                        params=(),
                        parent=None,
                    )
            return target
        else:
            # Function → command (existing behavior + parent support)
            target._command_meta = CommandMetadata(
                name=target.__name__,
                help_text=help,
                params=tuple(params) if params else (),
                parent=parent,
            )
            return target
    return decorator

def _to_cli_name(name: str) -> str:
    """Convert PythonName to cli-name."""
    # CamelCase → kebab-case, underscores → hyphens
    import re
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "-", name)
    return s.lower().replace("_", "-")
```

### Updated SitePlugin.get_commands()

```python
def get_commands(self) -> list[CommandSpec]:
    commands: list[CommandSpec] = []

    # 1. Discover @command-decorated methods on self (group=None)
    for attr_name in dir(self):
        if attr_name.startswith("_"):
            continue
        attr = getattr(self, attr_name)
        meta = getattr(attr, "_command_meta", None)
        if callable(attr) and meta is not None and meta.parent is None:
            params = meta.params if meta.params else tuple(self._introspect_params(attr))
            commands.append(CommandSpec(
                name=attr_name,
                handler=attr,
                help_text=meta.help_text,
                params=params,
                requires_session=None,
                group=None,
            ))

    # 2. Discover @command-decorated classes registered against this plugin
    commands.extend(self._discover_command_groups())

    return commands

def _discover_command_groups(self) -> list[CommandSpec]:
    """Walk registered command group classes and produce CommandSpecs."""
    # Implementation resolves parent= references into dotted group paths
    # and instantiates group classes to discover their methods
    ...
```

### CLI registration

`register_plugin_commands` reads `spec.group` and builds Click group hierarchy:

```python
for spec in plugin.get_commands():
    if spec.group is None:
        # Top-level command
        cmd = _create_plugin_command(plugin, spec)
        plugin_group.add_command(cmd, name=spec.name)
    else:
        # Grouped command — ensure parent groups exist
        group = _ensure_group_hierarchy(plugin_group, spec.group)
        cmd = _create_plugin_command(plugin, spec)
        group.add_command(cmd, name=spec.name)
```

### YAML plugins

YAML plugins don't support command groups in v1. All YAML commands have `group=None`. Documented as a known limitation.

### Group class registration

Command group classes need to be associated with a plugin. Two approaches:

**Option A (recommended): Module-level discovery.** When `get_commands()` runs, it inspects the module where the plugin class was defined and finds all `@command`-decorated classes in that module. Classes with `parent=None` are top-level groups for this plugin. Classes with `parent=SomeGroup` chain from there.

**Option B: Explicit registration.** Plugin declares `command_groups = [Accounts, Statements]`. More explicit but more boilerplate.

Option A is more ergonomic — define a class in the same file and it's discovered automatically.

---

## Section 4: Custom Output Formatters (F4)

### OutputFormatter Protocol

```python
@runtime_checkable
class OutputFormatter(Protocol):
    """Protocol for custom output formatters."""

    @property
    def name(self) -> str:
        """Formatter name used in --format flag."""
        ...

    def format(self, data: Any, console: Console) -> None:
        """Format and print data to the console."""
        ...
```

### Built-in formatters

Refactor `formatters.py` to implement the protocol:

```python
class JsonFormatter:
    name = "json"
    def format(self, data: Any, console: Console) -> None: ...

class TableFormatter:
    name = "table"
    def format(self, data: Any, console: Console) -> None: ...

class RawFormatter:
    name = "raw"
    def format(self, data: Any, console: Console) -> None: ...

BUILTIN_FORMATTERS: dict[str, OutputFormatter] = {
    "json": JsonFormatter(),
    "table": TableFormatter(),
    "raw": RawFormatter(),
}
```

### Entry point discovery

```toml
[project.entry-points."graftpunk.formatters"]
csv = "mypackage.formatters:CsvFormatter"
```

```python
def discover_formatters() -> dict[str, OutputFormatter]:
    """Discover all formatters (built-in + entry points)."""
    formatters = dict(BUILTIN_FORMATTERS)
    for ep in importlib.metadata.entry_points(group="graftpunk.formatters"):
        cls = ep.load()
        instance = cls()
        formatters[instance.name] = instance
    return formatters
```

### Dynamic --format flag

In `_create_plugin_command`, the `--format` choice list is built from discovered formatters:

```python
available_formats = list(discover_formatters().keys())
params.append(
    click.Option(
        ["--format", "-f"],
        type=click.Choice(available_formats),
        default="json",
        help=f"Output format: {', '.join(available_formats)}",
    )
)
```

### format_output updated

```python
def format_output(data: Any, format_type: str, console: Console) -> None:
    formatters = discover_formatters()
    formatter = formatters.get(format_type)
    if formatter is None:
        LOG.warning("unknown_format", format=format_type)
        formatter = formatters["json"]  # fallback

    # Unwrap CommandResult
    if isinstance(data, CommandResult):
        if data.format_hint and data.format_hint in formatters and format_type == "json":
            # Plugin hint takes effect only when user hasn't specified --format
            formatter = formatters[data.format_hint]
        data = data.data

    formatter.format(data, console)
```

### Exports

Add to `plugins/__init__.py`: `OutputFormatter`, `discover_formatters`.

Entry point group: `graftpunk.formatters`.

---

## Section 5: Lifecycle Hooks (R4)

### SitePlugin additions

```python
class SitePlugin:
    # ... existing ...

    def setup(self) -> None:
        """Called once after the plugin is registered successfully.

        Override to perform initialization (validate config, open connections,
        warm caches). Exceptions here are caught and reported as registration
        errors — the plugin is skipped but other plugins continue.
        """
        pass

    def teardown(self) -> None:
        """Called during application shutdown.

        Override to clean up resources (close connections, flush buffers).
        Exceptions here are logged but do not propagate — all plugins
        get their teardown called regardless of individual failures.
        """
        pass
```

### CLIPluginProtocol additions

```python
class CLIPluginProtocol(Protocol):
    # ... existing properties ...
    def get_commands(self) -> list[CommandSpec]: ...
    def get_session(self) -> requests.Session: ...
    def setup(self) -> None: ...
    def teardown(self) -> None: ...
```

### Registration integration

In `register_plugin_commands`, after a plugin's commands are registered:

```python
try:
    plugin.setup()
    LOG.debug("plugin_setup_complete", plugin=site_name)
except Exception as exc:  # noqa: BLE001
    LOG.exception("plugin_setup_failed", plugin=site_name, error=str(exc))
    result.add_error(site_name, f"setup() failed: {exc}", "registration")
    # Remove the plugin group since setup failed
    continue
```

For teardown, register with atexit during registration:

```python
import atexit

_registered_plugins_for_teardown: list[CLIPluginProtocol] = []

def _teardown_all_plugins() -> None:
    for plugin in reversed(_registered_plugins_for_teardown):
        try:
            plugin.teardown()
        except Exception as exc:  # noqa: BLE001
            LOG.error("plugin_teardown_failed", plugin=plugin.site_name, error=str(exc))

atexit.register(_teardown_all_plugins)
```

After successful setup, append to teardown list:
```python
_registered_plugins_for_teardown.append(plugin)
```

---

## Section 6: Async Handler Detection (F1)

In `_execute_with_limits`, after calling the handler:

```python
result = handler(ctx, **kwargs)
if asyncio.iscoroutine(result):
    LOG.warning(
        "async_handler_auto_executed",
        command=spec.name,
        plugin=ctx.plugin_name,
        hint="Async handlers work but are not officially supported in v1. "
             "Consider using a synchronous handler.",
    )
    result = asyncio.run(result)
return result
```

This makes async handlers "just work" without formally supporting them. The warning serves as documentation and as a hook for v2 changes.

---

## Section 7: Documentation Updates

### HOW_IT_WORKS.md additions

1. **API Version Contract:** "api_version 1 means: synchronous handlers, CommandContext with session/base_url/config/observe, LoginConfig-based declarative login, list[CommandSpec] from get_commands(), setup()/teardown() lifecycle."

2. **YAML Plugin Limitations:** List: no command groups, no request chaining, no conditional logic, no custom transforms beyond JMESPath, no pagination, no token refresh, no custom login flows. Frame positively as "graduation" path to Python.

3. **Format Precedence:** "`--format` CLI flag > `CommandResult.format_hint` > default (`json`)."

4. **Error Handling for Plugin Authors:** "Raise `CommandError('message')` for expected failures. Raise `PluginError` for infrastructure failures. Other exceptions are treated as crashes."

5. **Async Handlers:** "Handlers must be synchronous in v1. Async handlers are auto-detected and executed but not officially supported."

6. **Plugin Configuration Convention:** "Per-user plugin config: `~/.config/graftpunk/plugin-config/{site_name}.yaml`. Framework does not read this — it's a convention for plugin authors."

7. **Command Groups:** Document the `@command` decorator's class behavior, `parent=` parameter, and nesting patterns.

---

## Summary of All Changes

| Item | Category | Change |
|------|----------|--------|
| C1 | API version | `SUPPORTED_API_VERSIONS` constant, registration check, version branch pattern |
| C2 | CommandContext | Add `base_url`, `config` fields |
| C3 | CommandError | New exception with `user_message`, distinct CLI catch |
| R1 | login rename | `login` → `login_config` on SitePlugin, Protocol, PluginConfig, all consumers |
| R2 | get_commands | Return `list[CommandSpec]` instead of `dict[str, CommandSpec]` |
| R3 | requires_session | Add to CommandSpec (per-command), `None` = inherit from plugin |
| R4 | Lifecycle hooks | `setup()` + `teardown()` on SitePlugin and Protocol |
| R5 | YAML limits | Document limitations and graduation path |
| R6 | Format precedence | Document: flag > hint > default |
| R7 | Plugin metadata | Add `plugin_version`, `plugin_author`, `plugin_url` to PluginConfig |
| F1 | Async handlers | Auto-detect and execute coroutines with warning |
| F4 | Custom formatters | OutputFormatter protocol, entry-point discovery, dynamic --format |
| F5 | Command groups | Unified @command on classes, `parent=` nesting, `CommandGroupMeta`, `group` field on CommandSpec |
| F6 | Plugin config | Document convention (`~/.config/graftpunk/plugin-config/`) |

### Files affected (estimated)

| File | Changes |
|------|---------|
| `exceptions.py` | Add `CommandError` |
| `cli_plugin.py` | Rename `login`→`login_config`, add `CommandGroupMeta`, update `@command`, update `get_commands()`, add `setup()`/`teardown()`, add `SUPPORTED_API_VERSIONS`, update `CommandContext`/`CommandSpec`/`CommandMetadata`, add `R7` metadata fields |
| `plugin_commands.py` | Catch `CommandError`, version check, version branch, group hierarchy builder, dynamic format flag, teardown registration, `login_config` rename |
| `login_engine.py` | `login_config` rename |
| `yaml_loader.py` | `login_config` rename, metadata fields |
| `yaml_plugin.py` | `login_config` rename |
| `python_loader.py` | No changes |
| `plugins/__init__.py` | Export `CommandError`, `CommandGroupMeta`, `SUPPORTED_API_VERSIONS`, `OutputFormatter`, `discover_formatters` |
| `formatters.py` | Refactor to protocol + three implementations, discovery |
| `observe/context.py` | No changes |
| `session.py` | No changes |
| `exceptions.py` | Add `CommandError` |
| `HOW_IT_WORKS.md` | Sections 1-7 documentation |
| Examples + templates | `login_config` rename, command group examples |
| All test files | Update for all the above |
