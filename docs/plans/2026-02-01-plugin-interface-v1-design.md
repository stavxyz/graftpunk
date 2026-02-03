# Plugin Interface v1 Design

**Goal:** Define the canonical plugin interface for graftpunk's first release — clean, extensible, no legacy baggage.

**Context:** Pre-release. No external users. Existing examples will be updated to match. This is the one chance to get the interface right without backwards-compatibility constraints.

---

## Decisions

| Topic | Decision |
|-------|----------|
| API versioning | `api_version: int = 1` on PluginConfig |
| Handler signature | `CommandContext` dataclass (no bare session) |
| Login credentials | `credentials: dict[str, str]` — plugin declares field names |
| Site name collisions | Fail fast with error showing both plugin sources |
| Decorator metadata | `CommandMetadata` dataclass stored as `_command_meta` |
| Browser lifecycle | Context manager protocol on `BrowserSession` |
| Structured responses | `CommandResult` wrapper |
| YAML login | `login:` section in YAML schema |
| Resource limits | `timeout`, `max_retries`, `rate_limit` on `CommandSpec` |
| Observability | Hybrid: framework auto-captures, `ObservabilityContext` on `CommandContext` for plugin annotations |

---

## New Types

### CommandContext

Replaces bare `requests.Session` as the first argument to all command handlers.

```python
@dataclass(frozen=True)
class CommandContext:
    session: requests.Session
    plugin_name: str
    command_name: str
    api_version: int
    observe: ObservabilityContext
```

All handlers receive `CommandContext` as their first positional argument. No dual-path — this is the only signature. The `observe` field is always present — in `off` mode, all its methods are no-ops.

### CommandResult

Optional structured return type for command handlers.

```python
@dataclass
class CommandResult:
    data: Any
    metadata: dict[str, Any] = field(default_factory=dict)
    format_hint: str | None = None  # "table", "json", "raw"
```

Handlers may return raw data (treated as `CommandResult(data=result)` by the formatter) or an explicit `CommandResult` for richer output. Formatters check `isinstance(result, CommandResult)` and unwrap.

### CommandMetadata

Replaces monkey-patched `_is_cli_command`, `_help_text`, `_params` attributes.

```python
@dataclass(frozen=True)
class CommandMetadata:
    name: str
    help_text: str
    params: list[PluginParamSpec]
```

The `@command` decorator stores this as `func._command_meta`. Introspection is a single `getattr(method, '_command_meta', None)` check.

---

## Modified Types

### PluginConfig

Add `api_version`:

```python
@dataclass
class PluginConfig:
    site_name: str
    session_name: str
    help_text: str
    base_url: str = ""
    requires_session: bool = True
    backend: Literal["selenium", "nodriver"] = "selenium"
    api_version: int = 1
    # ... existing login fields unchanged ...
```

### CommandSpec

Add resource limit fields:

```python
@dataclass
class CommandSpec:
    name: str
    handler: Any
    help_text: str
    params: list[PluginParamSpec] = field(default_factory=list)
    timeout: float | None = None      # Request timeout in seconds
    max_retries: int = 0              # Retries on failure (0 = none)
    rate_limit: float | None = None   # Min seconds between requests
```

Execution layer behavior:
- `timeout` → passed to `session.request(timeout=...)`
- `max_retries` → retry loop with exponential backoff
- `rate_limit` → enforced minimum delay between consecutive calls

### @command Decorator

Updated to store `CommandMetadata`:

```python
def command(name: str, help_text: str = "", params: list[PluginParamSpec] | None = None):
    def decorator(func):
        func._command_meta = CommandMetadata(
            name=name,
            help_text=help_text,
            params=params or [],
        )
        return func
    return decorator
```

Remove all references to `_is_cli_command`, `_help_text`, `_params`.

---

## Login Credential Model

### Principle

Graftpunk is agnostic about what login requires. Plugin authors declare field names; graftpunk collects and forwards them as `credentials: dict[str, str]`.

### Plugin Declaration

`login_fields` maps field names to CSS selectors (for declarative login):

```python
login_fields: dict[str, str] = {
    "username": "input#email",
    "password": "input#password",
    "totp": "input#mfa",  # any extra fields — no special handling needed
}
```

### Handler Signature

Custom login methods receive the full dict:

```python
def login(self, credentials: dict[str, str]) -> bool:
    api_key = credentials["api_key"]
    org_id = credentials["org_id"]
    # ... site-specific logic ...
```

### CLI Prompting

The CLI iterates declared field names and prompts for each. Fields with names containing "password", "secret", "token", or "key" are masked (hidden input).

### Declarative Login Engine

`generate_login_method()` updated to use `credentials: dict[str, str]`. For each field in `login_fields`, it looks up the value from `credentials` by field name, then types it into the corresponding CSS selector.

---

## YAML Login Support

YAML plugins gain a `login:` section:

```yaml
name: my-site
base_url: https://example.com
session_name: my-site

login:
  url: /login
  fields:
    username: "input#email"
    password: "input#password"
  submit: "button[type=submit]"
  failure_text: "Invalid credentials"
  success_selector: ".dashboard"

commands:
  - name: search
    url: /api/search
    timeout: 30
    max_retries: 2
```

The YAML loader parses the `login:` block into PluginConfig's login attributes. The declarative login engine generates the method — same as Python plugins.

---

## BrowserSession Context Manager

```python
class BrowserSession(requestium.Session):
    def __enter__(self) -> BrowserSession:
        return self

    def __exit__(self, *exc_info) -> None:
        self.quit()

    async def __aenter__(self) -> BrowserSession:
        await self.start_async()
        return self

    async def __aexit__(self, *exc_info) -> None:
        self.quit()
```

Login engine usage becomes:

```python
# nodriver
async with BrowserSession(backend="nodriver", headless=False) as session:
    tab = await session.driver.get(url)
    # ... login logic ...
    await session.transfer_nodriver_cookies_to_session()

# selenium
with BrowserSession(backend="selenium", headless=False) as session:
    session.driver.get(url)
    # ... login logic ...
    session.transfer_driver_cookies_to_session()
```

No manual try/finally cleanup.

---

## Site Name Collision Detection

In `register_plugin_commands()`, track registered plugins by name:

```python
_registered_plugins: dict[str, str] = {}  # site_name → source description

# During registration:
if site_name in _registered_plugins:
    raise PluginError(
        f"Plugin name collision: '{site_name}' is already registered "
        f"by {_registered_plugins[site_name]}. Rename one of the plugins."
    )
_registered_plugins[site_name] = source  # e.g. "entry_point:my_package"
```

Source strings describe where the plugin came from:
- `"entry_point:package_name"` for entry point plugins
- `"file:/path/to/plugin.py"` for file-based Python plugins
- `"yaml:/path/to/plugin.yaml"` for YAML plugins

---

## Resource Limits Execution

In the command execution path (`plugin_commands.py`):

```python
import time

def _execute_with_limits(handler, ctx, spec, **kwargs):
    """Execute a handler respecting resource limit configuration."""
    attempts = 1 + spec.max_retries
    last_exc = None

    for attempt in range(attempts):
        try:
            # Rate limiting
            if spec.rate_limit:
                _enforce_rate_limit(spec.name, spec.rate_limit)

            # Timeout applied via session
            if spec.timeout:
                ctx = dataclasses.replace(ctx)  # Don't mutate original
                # timeout is passed through to request calls

            return handler(ctx, **kwargs)
        except Exception as exc:
            last_exc = exc
            if attempt < attempts - 1:
                backoff = 2 ** attempt
                LOG.warning("command_retry", command=spec.name, attempt=attempt + 1, backoff=backoff)
                time.sleep(backoff)

    raise last_exc
```

Rate limit tracking uses a module-level dict of last-execution timestamps per command name.

---

## Observability System

### Principle

Graftpunk core automatically captures browser diagnostics (screenshots, network activity, console logs) during browser sessions. Plugins can optionally annotate captures with named waypoints. Zero burden if ignored — observability works with no plugin cooperation.

### Three Pillars

1. **Screenshots** — PNG snapshots of browser state
   - Auto: on page navigation, on error, before/after login form submission
   - Manual: `ctx.observe.screenshot("captcha-appeared")`

2. **Network activity (HAR)** — Every HTTP request/response the browser makes
   - Selenium: Chrome Performance Logging (`goog:loggingPrefs`)
   - nodriver: CDP `Network` domain events
   - Stored in standard HAR 1.2 format (importable by Chrome DevTools, Fiddler, Charles)

3. **Console logs** — JavaScript console output
   - Selenium: `driver.get_log('browser')`
   - nodriver: CDP `Runtime.consoleAPICalled` + `Log.entryAdded`

### Storage Layout

```
~/.local/share/graftpunk/observe/
  <session-name>/
    <run-timestamp>/
      screenshots/
        001-navigation-https-example-com.png
        002-plugin-before-login.png
        003-auto-after-submit.png
      network.har           # Standard HAR 1.2 format
      console.jsonl          # One JSON object per log entry
      events.jsonl           # Plugin annotations + framework events
      metadata.json          # Run info: plugin, command, timestamps, config
```

### Activation Modes

| Mode | Screenshots | Network | Console | When |
|------|------------|---------|---------|------|
| `off` | No | No | No | Default for non-browser commands |
| `errors` | On error only | Last N requests on error | On error only | Default for browser commands |
| `full` | All events | Full HAR | All entries | `--observe` CLI flag or config |

### ObservabilityContext (Plugin-Facing)

```python
@dataclass
class ObservabilityContext:
    """Plugin-facing observability handle.

    All methods are safe to call regardless of activation mode.
    In 'off' mode, they're no-ops.
    """

    def screenshot(self, label: str) -> Path | None:
        """Capture a named screenshot. Returns path if captured, None if off."""
        ...

    def log(self, event: str, data: dict[str, Any] | None = None) -> None:
        """Record a custom event to events.jsonl."""
        ...

    def mark(self, label: str) -> None:
        """Add a named timestamp marker (appears in HAR/timeline)."""
        ...
```

Three methods. All no-ops when observability is off. Plugin authors never need to check mode.

### CaptureBackend (Internal)

```python
class CaptureBackend(Protocol):
    """Backend-specific capture implementation."""

    async def take_screenshot(self, label: str) -> Path | None: ...
    def get_har_entries(self) -> list[dict]: ...
    def get_console_logs(self) -> list[dict]: ...
    def start_capture(self) -> None: ...
    def stop_capture(self) -> None: ...
```

Two implementations:
- `SeleniumCaptureBackend` — WebDriver APIs (`get_screenshot_as_png`, Performance Logging, `get_log`)
- `NodriverCaptureBackend` — CDP protocol (`Page.captureScreenshot`, `Network` domain, `Runtime` domain)

### BrowserSession Integration

```python
class BrowserSession:
    def __enter__(self):
        if self._observe_mode != "off":
            self._capture = create_capture_backend(self._backend_type, self.driver)
            self._capture.start_capture()
        return self

    def __exit__(self, *exc_info):
        if self._capture:
            if exc_info[0] is not None:
                self._capture.take_screenshot("error-on-exit")
            self._capture.stop_capture()
            self._flush_to_disk()
        self.quit()
```

### CLI Integration

```bash
gp --observe hn search "python"     # Full capture for this command
gp --observe=errors hn login        # Errors-only (default for browser commands)
```

Global config in `~/.config/graftpunk/config.toml`:
```toml
[observe]
mode = "errors"          # default mode
retention_days = 7       # auto-cleanup old captures
```

Observe management commands:
```bash
gp observe list                     # Show recent capture sessions
gp observe show <run-id>            # Open capture directory
gp observe clean                    # Remove old captures
```

---

## What We Are NOT Doing

These were considered and explicitly deferred:

- **Class-level command registry** — `CommandMetadata` on functions is sufficient
- **Standalone browser context manager functions** — CM on BrowserSession itself is cleaner
- **Type-narrowed returns (union type)** — `CommandResult` wrapper is more extensible
- **Backwards compatibility shims** — pre-release, no legacy to support

---

## Files Affected

| File | Changes |
|------|---------|
| `src/graftpunk/plugins/cli_plugin.py` | Add CommandContext, CommandResult, CommandMetadata. Update @command decorator. Add api_version to PluginConfig. Add resource fields to CommandSpec. Update login signature to credentials dict. |
| `src/graftpunk/plugins/login_engine.py` | Update to credentials dict. Use BrowserSession context managers. |
| `src/graftpunk/session.py` | Add `__enter__`/`__exit__`/`__aenter__`/`__aexit__` to BrowserSession. Wire observability capture start/stop. |
| `src/graftpunk/plugins/yaml_loader.py` | Parse `login:` section. Parse `timeout`/`max_retries`/`rate_limit` per command. |
| `src/graftpunk/plugins/yaml_plugin.py` | Wire YAML login config into PluginConfig. Pass resource limits to CommandSpec. |
| `src/graftpunk/cli/plugin_commands.py` | Collision detection. CommandContext creation (with ObservabilityContext). Resource limit execution. Update handler invocation. Credential prompting. |
| `src/graftpunk/plugins/formatters.py` | Handle `CommandResult` unwrapping. |
| `src/graftpunk/plugins/__init__.py` | Export new types. |
| `examples/plugins/quotes.py` | Update to new interface (CommandContext, credentials). |
| `examples/plugins/hackernews.py` | Update to new interface. |
| `examples/plugins/httpbin.yaml` | Add resource limits example. |
| `examples/templates/python_template.py` | Update template to new interface. |
| `examples/templates/yaml_template.yaml` | Update template with login section example. |
| `tests/unit/test_plugin_commands.py` | Update for CommandContext, collision detection, resource limits. |
| `tests/unit/test_session.py` | Add context manager tests. |
| `tests/unit/test_cli_plugin.py` | Update for CommandMetadata, CommandResult. |
| `tests/unit/test_login_engine.py` | Update for credentials dict, context manager usage. |
| `tests/unit/test_yaml_loader.py` | Add YAML login parsing tests. |
| `src/graftpunk/observe/__init__.py` | New module. ObservabilityContext, NoOpObservabilityContext. |
| `src/graftpunk/observe/capture.py` | New module. CaptureBackend protocol, SeleniumCaptureBackend, NodriverCaptureBackend. |
| `src/graftpunk/observe/storage.py` | New module. HAR writer, screenshot storage, JSONL event log, disk flush, cleanup. |
| `src/graftpunk/cli/main.py` | Add `--observe` global flag. Add `gp observe list/show/clean` commands. |
| `tests/unit/test_observe.py` | New. ObservabilityContext tests, capture backend tests, storage tests. |
