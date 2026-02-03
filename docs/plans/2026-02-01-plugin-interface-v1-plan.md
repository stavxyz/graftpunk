# Plugin Interface v1 Implementation Plan

**Goal:** Implement the canonical plugin interface for graftpunk v1 — CommandContext, CommandResult, CommandMetadata, credentials dict, observability, resource limits, BrowserSession context manager, YAML login, and collision detection.

**Architecture:** New types (CommandContext, CommandResult, CommandMetadata, ObservabilityContext) are added to cli_plugin.py. A new `src/graftpunk/observe/` module handles capture backends and storage. All handler signatures change from `handler(session, **kwargs)` to `handler(ctx, **kwargs)`. Login changes from `login(username, password)` to `login(credentials)`. BrowserSession gains `__enter__`/`__exit__`/`__aenter__`/`__aexit__`. No backwards compatibility — this is pre-release.

**Tech Stack:** Python 3.11+, Click, requests, selenium, nodriver (CDP), dataclasses, pytest, structlog

**Design doc:** `docs/plans/2026-02-01-plugin-interface-v1-design.md`

---

## Task 1: Add CommandMetadata dataclass and update @command decorator

Replace monkey-patched `_is_cli_command`, `_help_text`, `_params` with a single `CommandMetadata` dataclass.

**Files:**
- Modify: `src/graftpunk/plugins/cli_plugin.py:47-197`
- Modify: `tests/unit/test_cli_plugin.py`

**Step 1: Write failing tests**

In `tests/unit/test_cli_plugin.py`, add tests:

```python
from graftpunk.plugins.cli_plugin import CommandMetadata, command, PluginParamSpec


class TestCommandMetadata:
    def test_command_decorator_stores_metadata(self):
        @command(help="Search items", params=[])
        def search(ctx, query: str):
            pass

        assert hasattr(search, "_command_meta")
        assert isinstance(search._command_meta, CommandMetadata)
        assert search._command_meta.help_text == "Search items"
        assert search._command_meta.params == []

    def test_command_decorator_with_params(self):
        params = [PluginParamSpec(name="query", param_type=str, required=True)]

        @command(help="Search", params=params)
        def search(ctx, query: str):
            pass

        assert search._command_meta.params == params

    def test_command_decorator_name_from_function(self):
        @command(help="Do something")
        def my_command(ctx):
            pass

        assert search._command_meta.name == "my_command"

    def test_command_metadata_is_frozen(self):
        meta = CommandMetadata(name="test", help_text="help", params=[])
        with pytest.raises(FrozenInstanceError):
            meta.name = "other"

    def test_no_legacy_attributes(self):
        """@command must NOT set _is_cli_command, _help_text, _params."""
        @command(help="Search")
        def search(ctx):
            pass

        assert not hasattr(search, "_is_cli_command")
        assert not hasattr(search, "_help_text")
        assert not hasattr(search, "_params")
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_cli_plugin.py::TestCommandMetadata -v`
Expected: FAIL — CommandMetadata not defined

**Step 3: Implement CommandMetadata and update @command**

In `src/graftpunk/plugins/cli_plugin.py`:

Add dataclass after PluginParamSpec (~line 57):
```python
@dataclass(frozen=True)
class CommandMetadata:
    """Metadata stored on @command-decorated methods."""

    name: str
    help_text: str
    params: list[PluginParamSpec] = field(default_factory=list)
```

Update the `command()` decorator (~line 170) to:
```python
def command(
    help: str = "",
    params: list[PluginParamSpec] | None = None,
):
    """Decorator to mark a method as a CLI command."""
    def decorator(func: Any) -> Any:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            return func(*args, **kwargs)

        wrapper._command_meta = CommandMetadata(
            name=func.__name__,
            help_text=help,
            params=params or [],
        )
        return wrapper
    return decorator
```

Remove all setting of `_is_cli_command`, `_help_text`, `_params`.

**Step 4: Update SitePlugin.get_commands() to use _command_meta**

In `get_commands()` (~line 273), change the method scanning from checking `_is_cli_command` to checking `_command_meta`:

```python
def get_commands(self) -> dict[str, CommandSpec]:
    commands: dict[str, CommandSpec] = {}
    for attr_name in dir(self):
        method = getattr(self, attr_name, None)
        if method is None:
            continue
        meta = getattr(method, "_command_meta", None)
        if meta is None:
            continue
        params = meta.params if meta.params else self._introspect_params(method)
        commands[meta.name] = CommandSpec(
            name=meta.name,
            handler=method,
            help_text=meta.help_text,
            params=params,
        )
    return commands
```

**Step 5: Update all references in plugin_commands.py**

In `src/graftpunk/cli/plugin_commands.py`, update `_has_login_method()` (~line 194):

```python
def _has_login_method(plugin: Any) -> bool:
    login = getattr(plugin, "login", None)
    if login is None or not callable(login):
        return False
    # Exclude @command-decorated login methods
    return not hasattr(login, "_command_meta")
```

**Step 6: Update __init__.py exports**

In `src/graftpunk/plugins/__init__.py`, add `CommandMetadata` to exports.

**Step 7: Run full test suite**

Run: `uv run pytest tests/ -v`
Expected: PASS (all existing tests updated for new attribute)

**Step 8: Commit**

```bash
git add src/graftpunk/plugins/cli_plugin.py src/graftpunk/plugins/__init__.py src/graftpunk/cli/plugin_commands.py tests/unit/test_cli_plugin.py
git commit -m "feat: replace monkey-patched command attrs with CommandMetadata dataclass"
```

---

## Task 2: Add api_version to PluginConfig

**Files:**
- Modify: `src/graftpunk/plugins/cli_plugin.py:69-93`
- Modify: `tests/unit/test_cli_plugin.py`

**Step 1: Write failing test**

```python
class TestApiVersion:
    def test_plugin_config_has_api_version(self):
        config = PluginConfig(
            site_name="test", session_name="test", help_text="Test"
        )
        assert config.api_version == 1

    def test_plugin_config_custom_api_version(self):
        config = PluginConfig(
            site_name="test", session_name="test", help_text="Test", api_version=2
        )
        assert config.api_version == 2
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_cli_plugin.py::TestApiVersion -v`
Expected: FAIL — unexpected keyword argument 'api_version'

**Step 3: Add api_version field**

In `PluginConfig` dataclass (~line 69), add after `backend`:

```python
api_version: int = 1
```

Also add to `SitePlugin` class attributes (~line 236):

```python
api_version: int = 1
```

**Step 4: Run tests**

Run: `uv run pytest tests/ -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/graftpunk/plugins/cli_plugin.py tests/unit/test_cli_plugin.py
git commit -m "feat: add api_version field to PluginConfig and SitePlugin"
```

---

## Task 3: Add CommandContext dataclass

**Files:**
- Modify: `src/graftpunk/plugins/cli_plugin.py`
- Modify: `tests/unit/test_cli_plugin.py`

**Step 1: Write failing tests**

```python
from graftpunk.plugins.cli_plugin import CommandContext


class TestCommandContext:
    def test_command_context_fields(self):
        import requests
        session = requests.Session()
        ctx = CommandContext(
            session=session,
            plugin_name="test",
            command_name="search",
            api_version=1,
        )
        assert ctx.session is session
        assert ctx.plugin_name == "test"
        assert ctx.command_name == "search"
        assert ctx.api_version == 1

    def test_command_context_is_frozen(self):
        import requests
        ctx = CommandContext(
            session=requests.Session(),
            plugin_name="test",
            command_name="cmd",
            api_version=1,
        )
        with pytest.raises(FrozenInstanceError):
            ctx.plugin_name = "other"
```

Note: `observe` field will be added in Task 9 when the observability module exists. For now, CommandContext has session, plugin_name, command_name, api_version.

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_cli_plugin.py::TestCommandContext -v`
Expected: FAIL — cannot import CommandContext

**Step 3: Implement**

In `src/graftpunk/plugins/cli_plugin.py`, add after CommandMetadata:

```python
@dataclass(frozen=True)
class CommandContext:
    """Execution context passed to command handlers."""

    session: requests.Session
    plugin_name: str
    command_name: str
    api_version: int
```

Add `import requests` at top if not present (it's already imported indirectly via requestium, but add explicit import).

**Step 4: Export from __init__.py**

Add `CommandContext` to `src/graftpunk/plugins/__init__.py` exports.

**Step 5: Run tests**

Run: `uv run pytest tests/ -v`
Expected: PASS

**Step 6: Commit**

```bash
git add src/graftpunk/plugins/cli_plugin.py src/graftpunk/plugins/__init__.py tests/unit/test_cli_plugin.py
git commit -m "feat: add CommandContext dataclass for handler injection"
```

---

## Task 4: Add CommandResult dataclass

**Files:**
- Modify: `src/graftpunk/plugins/cli_plugin.py`
- Modify: `src/graftpunk/plugins/formatters.py`
- Modify: `tests/unit/test_cli_plugin.py`
- Modify: `tests/unit/test_formatters.py` (create if needed)

**Step 1: Write failing tests**

```python
from graftpunk.plugins.cli_plugin import CommandResult


class TestCommandResult:
    def test_command_result_data_only(self):
        result = CommandResult(data={"key": "value"})
        assert result.data == {"key": "value"}
        assert result.metadata == {}
        assert result.format_hint is None

    def test_command_result_with_metadata(self):
        result = CommandResult(
            data=[1, 2, 3],
            metadata={"total": 100, "page": 1},
            format_hint="table",
        )
        assert result.metadata["total"] == 100
        assert result.format_hint == "table"
```

For formatters, in `tests/unit/test_formatters.py`:

```python
from graftpunk.plugins.cli_plugin import CommandResult
from graftpunk.plugins.formatters import format_output


class TestCommandResultFormatting:
    def test_format_raw_data(self, capsys):
        """Raw data (not CommandResult) still works."""
        from rich.console import Console
        console = Console()
        format_output({"key": "value"}, "json", console)
        # Should not raise

    def test_format_command_result_unwraps(self, capsys):
        """CommandResult.data is extracted before formatting."""
        from rich.console import Console
        console = Console()
        result = CommandResult(data={"key": "value"})
        format_output(result, "json", console)
        # Should not raise — formats result.data, not the wrapper
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_cli_plugin.py::TestCommandResult tests/unit/test_formatters.py::TestCommandResultFormatting -v`
Expected: FAIL

**Step 3: Implement CommandResult**

In `src/graftpunk/plugins/cli_plugin.py`, add:

```python
@dataclass
class CommandResult:
    """Structured return type for command handlers."""

    data: Any
    metadata: dict[str, Any] = field(default_factory=dict)
    format_hint: str | None = None
```

**Step 4: Update formatters to unwrap CommandResult**

In `src/graftpunk/plugins/formatters.py`, at the start of `format_output()`:

```python
from graftpunk.plugins.cli_plugin import CommandResult

def format_output(data: Any, format_type: str, console: Console) -> None:
    # Unwrap CommandResult
    if isinstance(data, CommandResult):
        data = data.data
    # ... rest of function unchanged
```

**Step 5: Export from __init__.py**

Add `CommandResult` to exports.

**Step 6: Run tests**

Run: `uv run pytest tests/ -v`
Expected: PASS

**Step 7: Commit**

```bash
git add src/graftpunk/plugins/cli_plugin.py src/graftpunk/plugins/formatters.py src/graftpunk/plugins/__init__.py tests/
git commit -m "feat: add CommandResult dataclass with formatter unwrapping"
```

---

## Task 5: Add resource limit fields to CommandSpec

**Files:**
- Modify: `src/graftpunk/plugins/cli_plugin.py:59-66`
- Modify: `tests/unit/test_cli_plugin.py`

**Step 1: Write failing tests**

```python
class TestCommandSpecResourceLimits:
    def test_default_no_limits(self):
        spec = CommandSpec(name="test", handler=lambda: None, help_text="Test")
        assert spec.timeout is None
        assert spec.max_retries == 0
        assert spec.rate_limit is None

    def test_custom_limits(self):
        spec = CommandSpec(
            name="test", handler=lambda: None, help_text="Test",
            timeout=30.0, max_retries=3, rate_limit=1.0,
        )
        assert spec.timeout == 30.0
        assert spec.max_retries == 3
        assert spec.rate_limit == 1.0
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_cli_plugin.py::TestCommandSpecResourceLimits -v`
Expected: FAIL — unexpected keyword argument 'timeout'

**Step 3: Add fields to CommandSpec**

In `CommandSpec` dataclass (~line 59):

```python
@dataclass
class CommandSpec:
    """Specification for a CLI command."""

    name: str
    handler: Any
    help_text: str
    params: list[PluginParamSpec] = field(default_factory=list)
    timeout: float | None = None
    max_retries: int = 0
    rate_limit: float | None = None
```

**Step 4: Run tests**

Run: `uv run pytest tests/ -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/graftpunk/plugins/cli_plugin.py tests/unit/test_cli_plugin.py
git commit -m "feat: add timeout, max_retries, rate_limit to CommandSpec"
```

---

## Task 6: Update handler invocation to pass CommandContext

This is the key wiring task — change the execution path from `handler(session, **kwargs)` to `handler(ctx, **kwargs)`.

**Files:**
- Modify: `src/graftpunk/cli/plugin_commands.py:101-191`
- Modify: `src/graftpunk/plugins/yaml_plugin.py:69-159`
- Modify: `examples/plugins/hackernews.py`
- Modify: `examples/plugins/quotes.py` (if exists)
- Modify: `examples/templates/python_template.py`
- Modify: `tests/unit/test_plugin_commands.py`

**Step 1: Write failing tests**

In `tests/unit/test_plugin_commands.py`, add/update tests to verify CommandContext is passed:

```python
class TestCommandContextPassing:
    def test_handler_receives_command_context(self):
        """Handler should receive CommandContext as first arg, not bare session."""
        from graftpunk.plugins.cli_plugin import CommandContext

        received_ctx = None

        def handler(ctx, **kwargs):
            nonlocal received_ctx
            received_ctx = ctx
            return {"result": "ok"}

        # ... set up plugin with this handler, invoke click command ...
        # Assert received_ctx is a CommandContext instance
        assert isinstance(received_ctx, CommandContext)
        assert received_ctx.plugin_name == "test"
```

**Step 2: Run test to verify it fails**

Expected: FAIL — handler receives session, not CommandContext

**Step 3: Update _create_click_command in plugin_commands.py**

In the callback function (~line 152-184), change:

```python
# Old:
session = plugin.get_session()
result = cmd_spec.handler(session, **kwargs)

# New:
from graftpunk.plugins.cli_plugin import CommandContext
session = plugin.get_session()
ctx = CommandContext(
    session=session,
    plugin_name=plugin.site_name,
    command_name=cmd_spec.name,
    api_version=getattr(plugin, "api_version", 1),
)
result = cmd_spec.handler(ctx, **kwargs)
```

**Step 4: Add resource limit execution wrapper**

Add `_execute_with_limits()` function and use it in the callback:

```python
import time

_last_execution: dict[str, float] = {}  # command_key → timestamp

def _enforce_rate_limit(command_key: str, rate_limit: float) -> None:
    now = time.monotonic()
    last = _last_execution.get(command_key)
    if last is not None:
        elapsed = now - last
        if elapsed < rate_limit:
            time.sleep(rate_limit - elapsed)
    _last_execution[command_key] = time.monotonic()

def _execute_with_limits(handler: Any, ctx: CommandContext, spec: CommandSpec, **kwargs: Any) -> Any:
    attempts = 1 + spec.max_retries
    last_exc: Exception | None = None
    command_key = f"{ctx.plugin_name}.{spec.name}"

    for attempt in range(attempts):
        try:
            if spec.rate_limit:
                _enforce_rate_limit(command_key, spec.rate_limit)
            return handler(ctx, **kwargs)
        except Exception as exc:
            last_exc = exc
            if attempt < attempts - 1:
                backoff = 2 ** attempt
                LOG.warning("command_retry", command=spec.name, attempt=attempt + 1, backoff=backoff)
                time.sleep(backoff)

    assert last_exc is not None
    raise last_exc
```

Then in the callback, replace `result = cmd_spec.handler(ctx, **kwargs)` with:

```python
result = _execute_with_limits(cmd_spec.handler, ctx, cmd_spec, **kwargs)
```

**Step 5: Update YAML plugin handler signature**

In `src/graftpunk/plugins/yaml_plugin.py`, change `_create_handler`:

```python
def handler(ctx: CommandContext, **kwargs: Any) -> Any:
    session = ctx.session
    # ... rest uses session as before
```

Update the import at top:
```python
from graftpunk.plugins.cli_plugin import CommandContext, CommandSpec, PluginConfig, PluginParamSpec, SitePlugin
```

**Step 6: Update example plugins**

In `examples/plugins/hackernews.py`, change command methods from:
```python
@command(help="Get front page stories")
def front(self, session, limit: int = 10):
```
to:
```python
@command(help="Get front page stories")
def front(self, ctx, limit: int = 10):
    session = ctx.session
```

Same pattern for all @command methods in all example plugins and templates.

**Step 7: Update SitePlugin._introspect_params to skip 'ctx' instead of 'session'**

In `_introspect_params()` (~line 293), the method skips `self` and `session`. Change to skip `self` and `ctx`:

```python
skip = {"self", "ctx"}
```

**Step 8: Run full test suite**

Run: `uv run pytest tests/ -v`
Expected: PASS after updating all test mocks/assertions

**Step 9: Commit**

```bash
git add src/graftpunk/cli/plugin_commands.py src/graftpunk/plugins/yaml_plugin.py src/graftpunk/plugins/cli_plugin.py examples/ tests/
git commit -m "feat: pass CommandContext to handlers instead of bare session"
```

---

## Task 7: Update login credential model

Change from `login(username, password)` to `login(credentials: dict[str, str])`.

**Files:**
- Modify: `src/graftpunk/plugins/login_engine.py`
- Modify: `src/graftpunk/cli/plugin_commands.py:209-284`
- Modify: `examples/plugins/hackernews.py`
- Modify: `tests/unit/test_login_engine.py`
- Modify: `tests/unit/test_plugin_commands.py`

**Step 1: Write failing tests**

In `tests/unit/test_login_engine.py`:

```python
class TestCredentialsDictLogin:
    def test_nodriver_login_receives_credentials_dict(self):
        """Generated login method should accept credentials dict."""
        plugin = mock_plugin(backend="nodriver")
        login_method = generate_login_method(plugin)
        # Signature check
        import inspect
        sig = inspect.signature(login_method)
        assert "credentials" in sig.parameters

    def test_selenium_login_receives_credentials_dict(self):
        plugin = mock_plugin(backend="selenium")
        login_method = generate_login_method(plugin)
        import inspect
        sig = inspect.signature(login_method)
        assert "credentials" in sig.parameters
```

**Step 2: Run tests to verify they fail**

Expected: FAIL — signature has `username, password` not `credentials`

**Step 3: Update login_engine.py**

In `_generate_nodriver_login()`, change:
```python
async def login(credentials: dict[str, str]) -> bool:
    base_url = plugin.base_url.rstrip("/")
    login_url = plugin.login_url
    fields = plugin.login_fields
    # ...

    # Fill fields
    for field_name, selector in fields.items():
        value = credentials.get(field_name, "")
        element = await tab.select(selector)
        await element.click()
        await element.send_keys(value)
```

Same for `_generate_selenium_login()`:
```python
def login(credentials: dict[str, str]) -> bool:
    # ...
    for field_name, selector in fields.items():
        value = credentials.get(field_name, "")
        element = session.driver.find_element("css selector", selector)
        element.click()
        element.send_keys(value)
```

**Step 4: Update CLI credential prompting in plugin_commands.py**

In `_create_login_command()` (~line 209), change from `--username`/`--password` flags to dynamically prompting based on `login_fields`:

```python
def _create_login_command(plugin: Any, group: click.Group) -> None:
    login_method = plugin.login
    fields = getattr(plugin, "login_fields", {"username": "", "password": ""})
    secret_keywords = {"password", "secret", "token", "key"}

    @group.command(name="login", help=f"Log in to {plugin.site_name}")
    @click.pass_context
    def login_cmd(click_ctx, **kwargs):
        credentials = {}
        for field_name in fields:
            is_secret = any(kw in field_name.lower() for kw in secret_keywords)
            # Check envvar first
            envvar = f"{plugin.site_name.upper().replace('-', '_')}_{field_name.upper()}"
            env_value = os.environ.get(envvar)
            if env_value:
                credentials[field_name] = env_value
            else:
                credentials[field_name] = click.prompt(
                    field_name.replace("_", " ").title(),
                    hide_input=is_secret,
                )

        if asyncio.iscoroutinefunction(login_method):
            result = asyncio.run(login_method(credentials))
        else:
            result = login_method(credentials)

        if result:
            console.print(f"[green]Successfully logged in to {plugin.site_name}[/green]")
        else:
            console.print(f"[red]Login failed for {plugin.site_name}[/red]")
            raise SystemExit(1)
```

Also support explicit CLI options for common fields (username/password) via envvar, while also prompting for any additional fields.

**Step 5: Update example plugins**

Any example plugin with a custom `login()` method updates to `login(self, credentials: dict[str, str]) -> bool`.

**Step 6: Run tests**

Run: `uv run pytest tests/ -v`
Expected: PASS

**Step 7: Commit**

```bash
git add src/graftpunk/plugins/login_engine.py src/graftpunk/cli/plugin_commands.py examples/ tests/
git commit -m "feat: change login to credentials dict for agnostic auth"
```

---

## Task 8: Add BrowserSession context manager

**Files:**
- Modify: `src/graftpunk/session.py`
- Modify: `tests/unit/test_session.py`

**Step 1: Write failing tests**

```python
class TestBrowserSessionContextManager:
    def test_sync_context_manager(self):
        """BrowserSession supports 'with' statement."""
        with patch.object(BrowserSession, "__init__", return_value=None):
            session = BrowserSession.__new__(BrowserSession)
            session._backend_type = "selenium"
            session._observe_mode = "off"
            session._capture = None

            with patch.object(session, "quit") as mock_quit:
                with session as s:
                    assert s is session
                mock_quit.assert_called_once()

    def test_sync_context_manager_calls_quit_on_error(self):
        with patch.object(BrowserSession, "__init__", return_value=None):
            session = BrowserSession.__new__(BrowserSession)
            session._backend_type = "selenium"
            session._observe_mode = "off"
            session._capture = None

            with patch.object(session, "quit") as mock_quit:
                with pytest.raises(ValueError):
                    with session:
                        raise ValueError("test error")
                mock_quit.assert_called_once()

    @pytest.mark.asyncio
    async def test_async_context_manager(self):
        with patch.object(BrowserSession, "__init__", return_value=None):
            session = BrowserSession.__new__(BrowserSession)
            session._backend_type = "nodriver"
            session._backend_instance = None
            session._observe_mode = "off"
            session._capture = None

            with patch.object(session, "start_async", new_callable=AsyncMock) as mock_start:
                with patch.object(session, "quit") as mock_quit:
                    async with session as s:
                        assert s is session
                    mock_start.assert_called_once()
                    mock_quit.assert_called_once()
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_session.py::TestBrowserSessionContextManager -v`
Expected: FAIL — __enter__ not defined

**Step 3: Implement context manager methods**

In `src/graftpunk/session.py`, add to `BrowserSession` class:

```python
def __enter__(self) -> BrowserSession:
    return self

def __exit__(self, exc_type: type | None, exc_val: BaseException | None, exc_tb: Any) -> None:
    self.quit()

async def __aenter__(self) -> BrowserSession:
    await self.start_async()
    return self

async def __aexit__(self, exc_type: type | None, exc_val: BaseException | None, exc_tb: Any) -> None:
    self.quit()
```

**Step 4: Run tests**

Run: `uv run pytest tests/ -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/graftpunk/session.py tests/unit/test_session.py
git commit -m "feat: add context manager protocol to BrowserSession"
```

---

## Task 9: Implement observability module

This is the largest task — creates the new `src/graftpunk/observe/` module.

**Files:**
- Create: `src/graftpunk/observe/__init__.py`
- Create: `src/graftpunk/observe/context.py`
- Create: `src/graftpunk/observe/capture.py`
- Create: `src/graftpunk/observe/storage.py`
- Create: `tests/unit/test_observe.py`
- Modify: `src/graftpunk/plugins/cli_plugin.py` (add observe to CommandContext)

**Step 1: Write failing tests for ObservabilityContext**

In `tests/unit/test_observe.py`:

```python
from graftpunk.observe import ObservabilityContext, NoOpObservabilityContext


class TestNoOpObservabilityContext:
    def test_screenshot_returns_none(self):
        ctx = NoOpObservabilityContext()
        assert ctx.screenshot("test") is None

    def test_log_is_noop(self):
        ctx = NoOpObservabilityContext()
        ctx.log("event", {"key": "value"})  # should not raise

    def test_mark_is_noop(self):
        ctx = NoOpObservabilityContext()
        ctx.mark("label")  # should not raise


class TestObservabilityContext:
    def test_screenshot_delegates_to_capture(self, tmp_path):
        from graftpunk.observe.storage import ObserveStorage
        storage = ObserveStorage(base_dir=tmp_path, session_name="test", run_id="run1")
        capture = MockCaptureBackend()
        ctx = ObservabilityContext(capture=capture, storage=storage, mode="full")
        result = ctx.screenshot("test-label")
        assert capture.screenshot_called

    def test_log_writes_event(self, tmp_path):
        from graftpunk.observe.storage import ObserveStorage
        storage = ObserveStorage(base_dir=tmp_path, session_name="test", run_id="run1")
        ctx = ObservabilityContext(capture=None, storage=storage, mode="full")
        ctx.log("my_event", {"detail": "value"})
        events = storage.read_events()
        assert len(events) == 1
        assert events[0]["event"] == "my_event"

    def test_off_mode_is_noop(self):
        ctx = ObservabilityContext(capture=None, storage=None, mode="off")
        assert ctx.screenshot("test") is None
        ctx.log("event")  # no raise
        ctx.mark("label")  # no raise
```

**Step 2: Run tests to verify they fail**

Expected: FAIL — module not found

**Step 3: Implement observe module**

`src/graftpunk/observe/__init__.py`:
```python
"""Observability system for browser session debugging."""

from graftpunk.observe.context import NoOpObservabilityContext, ObservabilityContext

__all__ = ["ObservabilityContext", "NoOpObservabilityContext"]
```

`src/graftpunk/observe/context.py`:
```python
"""Plugin-facing observability context."""

from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from graftpunk.logging import get_logger

if TYPE_CHECKING:
    from graftpunk.observe.capture import CaptureBackend
    from graftpunk.observe.storage import ObserveStorage

LOG = get_logger(__name__)


class ObservabilityContext:
    """Plugin-facing observability handle.

    All methods are safe to call regardless of mode.
    In 'off' mode, they're no-ops.
    """

    def __init__(
        self,
        capture: CaptureBackend | None,
        storage: ObserveStorage | None,
        mode: str,
    ) -> None:
        self._capture = capture
        self._storage = storage
        self._mode = mode
        self._counter = 0

    def screenshot(self, label: str) -> Path | None:
        if self._mode == "off" or self._capture is None or self._storage is None:
            return None
        self._counter += 1
        png_data = self._capture.take_screenshot_sync()
        if png_data is None:
            return None
        return self._storage.save_screenshot(self._counter, label, png_data)

    def log(self, event: str, data: dict[str, Any] | None = None) -> None:
        if self._mode == "off" or self._storage is None:
            return
        self._storage.write_event(event, data or {})

    def mark(self, label: str) -> None:
        if self._mode == "off" or self._storage is None:
            return
        self._storage.write_event("mark", {"label": label, "timestamp": time.time()})


class NoOpObservabilityContext(ObservabilityContext):
    """No-op implementation for when observability is off."""

    def __init__(self) -> None:
        super().__init__(capture=None, storage=None, mode="off")
```

`src/graftpunk/observe/capture.py`:
```python
"""Backend-specific browser capture implementations."""

from __future__ import annotations

from typing import Any, Protocol

from graftpunk.logging import get_logger

LOG = get_logger(__name__)


class CaptureBackend(Protocol):
    """Protocol for browser capture backends."""

    def start_capture(self) -> None: ...
    def stop_capture(self) -> None: ...
    def take_screenshot_sync(self) -> bytes | None: ...
    def get_har_entries(self) -> list[dict[str, Any]]: ...
    def get_console_logs(self) -> list[dict[str, Any]]: ...


class SeleniumCaptureBackend:
    """Capture backend for Selenium WebDriver."""

    def __init__(self, driver: Any) -> None:
        self._driver = driver
        self._har_entries: list[dict[str, Any]] = []
        self._console_logs: list[dict[str, Any]] = []

    def start_capture(self) -> None:
        LOG.debug("selenium_capture_started")

    def stop_capture(self) -> None:
        # Collect final logs
        try:
            self._console_logs = self._driver.get_log("browser")
        except Exception as exc:
            LOG.debug("console_log_collection_failed", error=str(exc))
        LOG.debug("selenium_capture_stopped")

    def take_screenshot_sync(self) -> bytes | None:
        try:
            return self._driver.get_screenshot_as_png()
        except Exception as exc:
            LOG.warning("screenshot_failed", error=str(exc))
            return None

    def get_har_entries(self) -> list[dict[str, Any]]:
        try:
            logs = self._driver.get_log("performance")
            return [entry for entry in logs]
        except Exception as exc:
            LOG.debug("har_collection_failed", error=str(exc))
            return []

    def get_console_logs(self) -> list[dict[str, Any]]:
        return self._console_logs


class NodriverCaptureBackend:
    """Capture backend for nodriver (CDP)."""

    def __init__(self, browser: Any) -> None:
        self._browser = browser
        self._har_entries: list[dict[str, Any]] = []
        self._console_logs: list[dict[str, Any]] = []

    def start_capture(self) -> None:
        LOG.debug("nodriver_capture_started")

    def stop_capture(self) -> None:
        LOG.debug("nodriver_capture_stopped")

    def take_screenshot_sync(self) -> bytes | None:
        """Sync screenshot — requires running event loop externally for nodriver."""
        # nodriver screenshots are async; caller must handle
        LOG.debug("nodriver_screenshot_sync_not_available")
        return None

    def get_har_entries(self) -> list[dict[str, Any]]:
        return self._har_entries

    def get_console_logs(self) -> list[dict[str, Any]]:
        return self._console_logs


def create_capture_backend(backend_type: str, driver: Any) -> CaptureBackend:
    """Create a capture backend for the given browser type."""
    if backend_type == "nodriver":
        return NodriverCaptureBackend(driver)
    return SeleniumCaptureBackend(driver)
```

`src/graftpunk/observe/storage.py`:
```python
"""Observability data storage — screenshots, HAR, events, console logs."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from graftpunk.logging import get_logger

LOG = get_logger(__name__)


class ObserveStorage:
    """Manages on-disk storage for a single observability run."""

    def __init__(self, base_dir: Path, session_name: str, run_id: str) -> None:
        self._run_dir = base_dir / session_name / run_id
        self._screenshots_dir = self._run_dir / "screenshots"
        self._screenshots_dir.mkdir(parents=True, exist_ok=True)
        self._events_path = self._run_dir / "events.jsonl"
        self._console_path = self._run_dir / "console.jsonl"
        self._har_path = self._run_dir / "network.har"
        self._metadata_path = self._run_dir / "metadata.json"

    @property
    def run_dir(self) -> Path:
        return self._run_dir

    def save_screenshot(self, index: int, label: str, png_data: bytes) -> Path:
        filename = f"{index:03d}-{label}.png"
        path = self._screenshots_dir / filename
        path.write_bytes(png_data)
        LOG.debug("screenshot_saved", path=str(path))
        return path

    def write_event(self, event: str, data: dict[str, Any]) -> None:
        entry = {"event": event, "timestamp": time.time(), **data}
        with self._events_path.open("a") as f:
            f.write(json.dumps(entry) + "\n")

    def read_events(self) -> list[dict[str, Any]]:
        if not self._events_path.exists():
            return []
        events = []
        for line in self._events_path.read_text().strip().split("\n"):
            if line:
                events.append(json.loads(line))
        return events

    def write_console_logs(self, logs: list[dict[str, Any]]) -> None:
        with self._console_path.open("a") as f:
            for entry in logs:
                f.write(json.dumps(entry) + "\n")

    def write_har(self, entries: list[dict[str, Any]]) -> None:
        har = {
            "log": {
                "version": "1.2",
                "creator": {"name": "graftpunk", "version": "0.1.0"},
                "entries": entries,
            }
        }
        self._har_path.write_text(json.dumps(har, indent=2))

    def write_metadata(self, metadata: dict[str, Any]) -> None:
        self._metadata_path.write_text(json.dumps(metadata, indent=2))

    def cleanup_old_runs(self, retention_days: int = 7) -> int:
        """Remove runs older than retention_days. Returns count removed."""
        if not self._run_dir.parent.exists():
            return 0
        cutoff = time.time() - (retention_days * 86400)
        removed = 0
        for run_dir in self._run_dir.parent.iterdir():
            if run_dir.is_dir() and run_dir.stat().st_mtime < cutoff:
                import shutil
                shutil.rmtree(run_dir)
                removed += 1
        return removed
```

**Step 4: Add observe field to CommandContext**

In `src/graftpunk/plugins/cli_plugin.py`, update CommandContext:

```python
from graftpunk.observe import ObservabilityContext, NoOpObservabilityContext

@dataclass(frozen=True)
class CommandContext:
    """Execution context passed to command handlers."""

    session: requests.Session
    plugin_name: str
    command_name: str
    api_version: int
    observe: ObservabilityContext = field(default_factory=NoOpObservabilityContext)
```

**Step 5: Run tests**

Run: `uv run pytest tests/ -v`
Expected: PASS

**Step 6: Commit**

```bash
git add src/graftpunk/observe/ src/graftpunk/plugins/cli_plugin.py tests/unit/test_observe.py
git commit -m "feat: add observability module with context, capture backends, and storage"
```

---

## Task 10: Wire observability into BrowserSession and CLI

**Files:**
- Modify: `src/graftpunk/session.py`
- Modify: `src/graftpunk/cli/plugin_commands.py`
- Modify: `src/graftpunk/cli/main.py`
- Modify: `tests/unit/test_session.py`

**Step 1: Write failing tests**

```python
class TestBrowserSessionObservability:
    def test_context_manager_starts_capture_in_full_mode(self):
        # BrowserSession with observe_mode="full" should start capture on __enter__
        ...

    def test_context_manager_flushes_on_exit(self):
        # __exit__ should flush HAR and console logs to storage
        ...

    def test_error_triggers_screenshot(self):
        # When exception occurs, __exit__ should take error screenshot
        ...
```

**Step 2: Run tests to verify they fail**

**Step 3: Add observe_mode to BrowserSession.__init__**

In `src/graftpunk/session.py`, add `observe_mode` parameter:

```python
def __init__(
    self,
    *args: Any,
    headless: bool = True,
    default_timeout: int = 15,
    use_stealth: bool = True,
    backend: str = "selenium",
    observe_mode: str = "off",
    **kwargs: Any,
) -> None:
    self._observe_mode = observe_mode
    self._capture = None
    self._observe_storage = None
    # ... rest of __init__
```

**Step 4: Update context manager to wire capture**

```python
def __enter__(self) -> BrowserSession:
    if self._observe_mode != "off" and self.driver is not None:
        from graftpunk.observe.capture import create_capture_backend
        from graftpunk.observe.storage import ObserveStorage
        import datetime
        run_id = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        base_dir = Path.home() / ".local" / "share" / "graftpunk" / "observe"
        session_name = getattr(self, "_session_name", "default")
        self._observe_storage = ObserveStorage(base_dir, session_name, run_id)
        self._capture = create_capture_backend(self._backend_type, self.driver)
        self._capture.start_capture()
    return self

def __exit__(self, exc_type, exc_val, exc_tb):
    if self._capture is not None:
        if exc_type is not None:
            self._capture.take_screenshot_sync()  # error screenshot
        self._capture.stop_capture()
        if self._observe_storage is not None:
            self._observe_storage.write_har(self._capture.get_har_entries())
            self._observe_storage.write_console_logs(self._capture.get_console_logs())
    self.quit()
    return None
```

**Step 5: Add --observe flag to CLI**

In `src/graftpunk/cli/main.py`, add `--observe` option to the main group. Store in Click context for plugin commands to read.

**Step 6: Add `gp observe list/show/clean` commands**

In `src/graftpunk/cli/main.py`, add observe command group:

```python
@cli.group()
def observe():
    """Manage observability captures."""
    pass

@observe.command(name="list")
def observe_list():
    """List recent capture sessions."""
    ...

@observe.command()
@click.argument("run_id")
def show(run_id):
    """Show capture directory for a run."""
    ...

@observe.command()
@click.option("--days", default=7, help="Remove captures older than N days")
def clean(days):
    """Remove old capture data."""
    ...
```

**Step 7: Update plugin_commands.py to create ObservabilityContext**

In `_create_click_command` callback, create ObservabilityContext and pass to CommandContext:

```python
from graftpunk.observe import ObservabilityContext, NoOpObservabilityContext

observe_mode = click_ctx.obj.get("observe_mode", "off") if click_ctx.obj else "off"
if observe_mode == "off":
    observe_ctx = NoOpObservabilityContext()
else:
    observe_ctx = ObservabilityContext(capture=..., storage=..., mode=observe_mode)

ctx = CommandContext(
    session=session,
    plugin_name=plugin.site_name,
    command_name=cmd_spec.name,
    api_version=getattr(plugin, "api_version", 1),
    observe=observe_ctx,
)
```

**Step 8: Run tests**

Run: `uv run pytest tests/ -v`
Expected: PASS

**Step 9: Commit**

```bash
git add src/graftpunk/session.py src/graftpunk/cli/main.py src/graftpunk/cli/plugin_commands.py tests/
git commit -m "feat: wire observability into BrowserSession, CLI, and command execution"
```

---

## Task 11: Add site name collision detection

**Files:**
- Modify: `src/graftpunk/cli/plugin_commands.py`
- Modify: `tests/unit/test_plugin_commands.py`

**Step 1: Write failing tests**

```python
class TestSiteNameCollisionDetection:
    def test_duplicate_site_name_raises(self):
        """Registering two plugins with the same site_name raises PluginError."""
        from graftpunk.exceptions import PluginError
        # Create two plugins with same site_name
        # Attempt to register both
        # Assert PluginError raised with both sources mentioned

    def test_different_site_names_no_collision(self):
        """Plugins with different site_names register fine."""
        ...

    def test_collision_error_includes_source(self):
        """Error message includes source of both plugins."""
        ...
```

**Step 2: Run tests to verify they fail**

**Step 3: Add collision detection to register_plugin_commands**

In `register_plugin_commands()`, add:

```python
_registered_plugins: dict[str, str] = {}  # site_name → source

# In the registration loop, before creating click group:
source = f"entry_point:{plugin.__class__.__module__}"  # or "file:/path" or "yaml:/path"
if site_name in _registered_plugins:
    raise PluginError(
        f"Plugin name collision: '{site_name}' is already registered "
        f"by {_registered_plugins[site_name]}. Rename one of the plugins."
    )
_registered_plugins[site_name] = source
```

**Step 4: Run tests**

Run: `uv run pytest tests/ -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/graftpunk/cli/plugin_commands.py tests/unit/test_plugin_commands.py
git commit -m "feat: fail fast on plugin site_name collisions"
```

---

## Task 12: Add YAML login support

**Files:**
- Modify: `src/graftpunk/plugins/yaml_loader.py`
- Modify: `src/graftpunk/plugins/yaml_plugin.py`
- Modify: `tests/unit/test_yaml_loader.py`

**Step 1: Write failing tests**

In `tests/unit/test_yaml_loader.py`:

```python
class TestYAMLLoginParsing:
    def test_login_section_parsed(self):
        yaml_content = """
        name: test-site
        base_url: https://example.com
        session_name: test
        help: Test plugin
        login:
          url: /login
          fields:
            username: "input#email"
            password: "input#pass"
          submit: "button[type=submit]"
          failure_text: "Invalid credentials"
          success_selector: ".dashboard"
        commands:
          - name: search
            url: /api/search
        """
        config, commands, headers = parse_yaml_plugin(yaml_content, Path("test.yaml"))
        assert config.login_url == "/login"
        assert config.login_fields == {"username": "input#email", "password": "input#pass"}
        assert config.login_submit == "button[type=submit]"
        assert config.login_failure == "Invalid credentials"
        assert config.login_success == ".dashboard"

    def test_yaml_without_login_section(self):
        """Plugins without login: section still work."""
        yaml_content = """
        name: test-site
        base_url: https://example.com
        session_name: test
        help: Test plugin
        commands:
          - name: search
            url: /api/search
        """
        config, commands, headers = parse_yaml_plugin(yaml_content, Path("test.yaml"))
        assert config.login_url == ""
        assert config.login_fields == {}
```

**Step 2: Run tests to verify they fail (or pass if login parsing already exists)**

Note: The explore agent found that `parse_yaml_plugin` already normalizes a `login:` block at line ~205-303. Verify whether it already handles this correctly. If yes, this task reduces to adding tests + adding `timeout`/`max_retries`/`rate_limit` parsing for YAML commands.

**Step 3: Add resource limit parsing for YAML commands**

In `parse_yaml_plugin()`, when parsing command dicts, extract resource limit fields:

```python
# In the command parsing loop:
timeout = cmd_dict.get("timeout")
max_retries = cmd_dict.get("max_retries", 0)
rate_limit = cmd_dict.get("rate_limit")
```

These get passed through to `CommandSpec` in `yaml_plugin.py`'s `create_yaml_site_plugin`.

**Step 4: Update YAMLCommandDef**

Add fields to `YAMLCommandDef`:
```python
timeout: float | None = None
max_retries: int = 0
rate_limit: float | None = None
```

**Step 5: Update yaml_plugin.py to pass resource limits**

In `create_yaml_site_plugin`, when building CommandSpec:

```python
command_specs[cmd_def.name] = CommandSpec(
    name=cmd_def.name,
    handler=handler,
    help_text=cmd_def.help_text,
    params=params,
    timeout=cmd_def.timeout,
    max_retries=cmd_def.max_retries,
    rate_limit=cmd_def.rate_limit,
)
```

**Step 6: Run tests**

Run: `uv run pytest tests/ -v`
Expected: PASS

**Step 7: Commit**

```bash
git add src/graftpunk/plugins/yaml_loader.py src/graftpunk/plugins/yaml_plugin.py tests/unit/test_yaml_loader.py
git commit -m "feat: add YAML login support and resource limits to YAML commands"
```

---

## Task 13: Update login engine to use BrowserSession context manager

**Files:**
- Modify: `src/graftpunk/plugins/login_engine.py`
- Modify: `tests/unit/test_login_engine.py`

**Step 1: Write failing tests**

```python
class TestLoginEngineContextManager:
    def test_nodriver_login_uses_async_context_manager(self):
        """Login should use 'async with BrowserSession(...)' instead of manual try/finally."""
        # Verify BrowserSession.__aenter__ and __aexit__ are called
        ...

    def test_selenium_login_uses_sync_context_manager(self):
        """Login should use 'with BrowserSession(...)' instead of manual try/finally."""
        ...
```

**Step 2: Run tests to verify they fail**

**Step 3: Refactor login engine to use context managers**

Replace `_generate_nodriver_login`:
```python
async def login(credentials: dict[str, str]) -> bool:
    base_url = plugin.base_url.rstrip("/")
    login_url = plugin.login_url
    fields = plugin.login_fields
    submit_selector = plugin.login_submit
    failure_text = plugin.login_failure

    async with BrowserSession(backend="nodriver", headless=False) as session:
        tab = await session.driver.get(f"{base_url}{login_url}")

        for field_name, selector in fields.items():
            value = credentials.get(field_name, "")
            element = await tab.select(selector)
            await element.click()
            await element.send_keys(value)

        submit = await tab.select(submit_selector)
        await submit.click()
        await asyncio.sleep(3)

        page_text = await tab.get_content()
        if failure_text and failure_text in page_text:
            LOG.warning("nodriver_login_failed_credentials", plugin=plugin.site_name)
            return False

        success_selector = getattr(plugin, "login_success", "") or ""
        if success_selector:
            try:
                await tab.select(success_selector)
            except Exception:
                LOG.warning("nodriver_login_success_element_not_found", plugin=plugin.site_name)
                return False

        await session.transfer_nodriver_cookies_to_session()
        cache_session(session, plugin.session_name)
        return True
```

Replace `_generate_selenium_login`:
```python
def login(credentials: dict[str, str]) -> bool:
    base_url = plugin.base_url.rstrip("/")
    login_url = plugin.login_url
    fields = plugin.login_fields
    submit_selector = plugin.login_submit
    failure_text = plugin.login_failure
    success_selector = plugin.login_success

    with BrowserSession(backend="selenium", headless=False) as session:
        session.driver.get(f"{base_url}{login_url}")

        for field_name, selector in fields.items():
            value = credentials.get(field_name, "")
            element = session.driver.find_element("css selector", selector)
            element.click()
            element.send_keys(value)

        submit = session.driver.find_element("css selector", submit_selector)
        submit.click()
        time.sleep(2)

        if failure_text:
            if failure_text in session.driver.page_source:
                LOG.warning("selenium_login_failed_credentials", plugin=plugin.site_name)
                return False
        elif success_selector:
            try:
                session.driver.find_element("css selector", success_selector)
            except NoSuchElementException:
                LOG.warning("selenium_login_failed_no_success_element", plugin=plugin.site_name)
                return False

        session.transfer_driver_cookies_to_session()
        cache_session(session, plugin.session_name)
        return True
```

Remove `_quiet_browser_cleanup`, `_stop_nodriver_quietly` — no longer needed.

**Step 4: Run tests**

Run: `uv run pytest tests/ -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/graftpunk/plugins/login_engine.py tests/unit/test_login_engine.py
git commit -m "refactor: use BrowserSession context managers in login engine"
```

---

## Task 14: Update examples and templates

**Files:**
- Modify: `examples/plugins/hackernews.py`
- Modify: `examples/plugins/httpbin.yaml`
- Modify: `examples/templates/python_template.py`
- Modify: `examples/templates/yaml_template.yaml`
- Modify: `examples/plugins/quotes.py` (if it exists)

**Step 1: Update hackernews.py**

Change command methods to use `ctx` parameter:
```python
from graftpunk.plugins import SitePlugin, command, CommandContext

class HackerNewsPlugin(SitePlugin):
    # ... existing class attrs ...
    api_version = 1

    @command(help="Get front page stories")
    def front(self, ctx: CommandContext, limit: int = 10):
        response = ctx.session.get(f"{self.base_url}/frontpage", params={"limit": limit})
        ...
```

If it has a custom `login()`, update to `login(self, credentials: dict[str, str])`.

**Step 2: Update httpbin.yaml**

Add resource limits to a few commands as examples:
```yaml
commands:
  - name: delay
    url: /delay/{seconds}
    timeout: 60
    max_retries: 1
```

**Step 3: Update python_template.py**

```python
from graftpunk.plugins import SitePlugin, command, CommandContext

class MyPlugin(SitePlugin):
    site_name = "my-site"
    session_name = "my-site"
    base_url = "https://example.com"
    help_text = "Commands for my-site"
    api_version = 1

    @command(help="Example command")
    def example(self, ctx: CommandContext):
        response = ctx.session.get(f"{self.base_url}/api/example")
        response.raise_for_status()
        return response.json()
```

**Step 4: Update yaml_template.yaml**

Add login section example (commented out):
```yaml
# login:
#   url: /login
#   fields:
#     username: "input#email"
#     password: "input#password"
#   submit: "button[type=submit]"
#   failure_text: "Invalid credentials"
```

**Step 5: Run tests**

Run: `uv run pytest tests/ -v`
Expected: PASS

**Step 6: Commit**

```bash
git add examples/
git commit -m "docs: update examples and templates to v1 plugin interface"
```

---

## Task 15: Run quality checks and final verification

**Step 1: Run linter**

```bash
uvx ruff check --fix .
uvx ruff format .
```

**Step 2: Run type checker**

```bash
uvx ty check src/
```

**Step 3: Run full test suite with coverage**

```bash
uv run pytest tests/ -v --cov=src
```

**Step 4: Fix any issues found**

**Step 5: Commit any fixes**

```bash
git add -A
git commit -m "fix: resolve lint and type issues from v1 interface changes"
```

---

## Dependency Order

```
Task 1 (CommandMetadata) → no deps
Task 2 (api_version) → no deps
Task 3 (CommandContext) → depends on Task 2
Task 4 (CommandResult) → no deps
Task 5 (resource limits) → no deps
Task 6 (handler invocation) → depends on Tasks 1, 3, 5
Task 7 (credentials) → depends on Task 6
Task 8 (BrowserSession CM) → no deps
Task 9 (observability module) → depends on Task 3
Task 10 (wire observability) → depends on Tasks 8, 9
Task 11 (collision detection) → no deps
Task 12 (YAML login + limits) → depends on Tasks 5, 7
Task 13 (login engine CM) → depends on Tasks 7, 8
Task 14 (examples) → depends on Tasks 6, 7
Task 15 (final checks) → depends on all
```

Parallelizable groups:
- **Group A** (no deps): Tasks 1, 2, 4, 5, 8, 11 — but should be done sequentially to avoid merge conflicts
- **Group B** (after Group A): Tasks 3, 6, 7, 9
- **Group C** (after Group B): Tasks 10, 12, 13, 14
- **Group D** (after all): Task 15
