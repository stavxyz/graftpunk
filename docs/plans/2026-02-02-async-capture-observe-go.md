# Async Capture, Session Context, and `gp observe go` Implementation Plan

**Goal:** Enable nodriver async screenshots, add active session context (`gp session use/unset`), and build `gp observe go <URL>` for interactive authenticated observability.

**Architecture:** Three layered features — async capture primitives at the bottom, session context in the middle, `observe go` on top. Each layer is independently useful but they compose for the full experience. Cookie injection (session→browser) is a new primitive needed by `observe go`.

**Tech Stack:** Python 3.12+, typer, nodriver CDP, asyncio, pytest

---

### Task 1: Add async methods to CaptureBackend protocol and NodriverCaptureBackend

**Files:**
- Modify: `src/graftpunk/observe/capture.py`
- Test: `tests/unit/test_observe.py`

**Step 1: Write the failing tests**

```python
# In tests/unit/test_observe.py, add to TestNodriverCaptureBackend:

@pytest.mark.asyncio
async def test_take_screenshot_async_returns_none_without_tab(self) -> None:
    backend = NodriverCaptureBackend(browser=MagicMock())
    result = await backend.take_screenshot()
    assert result is None

@pytest.mark.asyncio
async def test_take_screenshot_async_returns_bytes_with_tab(self, tmp_path: Path) -> None:
    from unittest.mock import AsyncMock

    mock_tab = AsyncMock()
    # save_screenshot writes a file; we simulate by pre-creating it
    png_data = b"\x89PNG\r\n\x1a\nfake"

    async def fake_save_screenshot(path: Path) -> None:
        path.write_bytes(png_data)

    mock_tab.save_screenshot = fake_save_screenshot
    backend = NodriverCaptureBackend(browser=MagicMock(), get_tab=lambda: mock_tab)
    result = await backend.take_screenshot()
    assert result == png_data

@pytest.mark.asyncio
async def test_take_screenshot_async_handles_exception(self) -> None:
    from unittest.mock import AsyncMock

    mock_tab = AsyncMock()
    mock_tab.save_screenshot = AsyncMock(side_effect=RuntimeError("browser crashed"))
    backend = NodriverCaptureBackend(browser=MagicMock(), get_tab=lambda: mock_tab)
    result = await backend.take_screenshot()
    assert result is None

@pytest.mark.asyncio
async def test_get_page_source_async_returns_html(self) -> None:
    from unittest.mock import AsyncMock

    mock_tab = AsyncMock()
    mock_tab.get_content = AsyncMock(return_value="<html>hello</html>")
    backend = NodriverCaptureBackend(browser=MagicMock(), get_tab=lambda: mock_tab)
    result = await backend.get_page_source()
    assert result == "<html>hello</html>"

@pytest.mark.asyncio
async def test_get_page_source_async_returns_none_without_tab(self) -> None:
    backend = NodriverCaptureBackend(browser=MagicMock())
    result = await backend.get_page_source()
    assert result is None
```

Also add tests for SeleniumCaptureBackend async wrappers:

```python
# In TestSeleniumCaptureBackend:

@pytest.mark.asyncio
async def test_take_screenshot_async_wraps_sync(self) -> None:
    driver = MagicMock()
    driver.get_screenshot_as_png.return_value = b"png-data"
    backend = SeleniumCaptureBackend(driver)
    result = await backend.take_screenshot()
    assert result == b"png-data"

@pytest.mark.asyncio
async def test_get_page_source_async(self) -> None:
    driver = MagicMock()
    driver.page_source = "<html>test</html>"
    backend = SeleniumCaptureBackend(driver)
    result = await backend.get_page_source()
    assert result == "<html>test</html>"
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_observe.py -v -k "async"`
Expected: FAIL (methods don't exist yet)

**Step 3: Implement async methods**

In `capture.py`:

1. Add to `CaptureBackend` protocol:
```python
async def take_screenshot(self) -> bytes | None:
    """Take a screenshot asynchronously.

    Returns:
        PNG image data as bytes, or None if capture failed.
    """
    ...

async def get_page_source(self) -> str | None:
    """Get the current page HTML source asynchronously.

    Returns:
        HTML source string, or None if capture failed.
    """
    ...
```

2. Update `NodriverCaptureBackend.__init__` to accept `get_tab` callable:
```python
from collections.abc import Callable

def __init__(self, browser: Any, get_tab: Callable[[], Any] | None = None) -> None:
    self._browser = browser
    self._get_tab = get_tab
    self._har_entries: list[dict[str, Any]] = []
    self._console_logs: list[dict[str, Any]] = []
    self._warned_no_screenshots: bool = False

@property
def _tab(self) -> Any | None:
    return self._get_tab() if self._get_tab else None
```

3. Add async methods to `NodriverCaptureBackend`:
```python
async def take_screenshot(self) -> bytes | None:
    """Take a screenshot via nodriver CDP.

    Returns:
        PNG image data as bytes, or None if no tab or capture failed.
    """
    tab = self._tab
    if tab is None:
        return None
    try:
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            path = Path(f.name)
        await tab.save_screenshot(path)
        data = path.read_bytes()
        path.unlink()
        return data
    except Exception as exc:
        LOG.error("nodriver_screenshot_failed", error=str(exc))
        return None

async def get_page_source(self) -> str | None:
    """Get page source via nodriver.

    Returns:
        HTML source string, or None if no tab or capture failed.
    """
    tab = self._tab
    if tab is None:
        return None
    try:
        return await tab.get_content()
    except Exception as exc:
        LOG.error("nodriver_page_source_failed", error=str(exc))
        return None
```

4. Add async wrappers to `SeleniumCaptureBackend`:
```python
async def take_screenshot(self) -> bytes | None:
    """Take a screenshot (sync wrapper for async interface)."""
    return self.take_screenshot_sync()

async def get_page_source(self) -> str | None:
    """Get page source via Selenium."""
    try:
        return self._driver.page_source
    except selenium.common.exceptions.WebDriverException as exc:
        LOG.error("selenium_page_source_failed", error=str(exc), exc_type=type(exc).__name__)
        return None
```

5. Update `create_capture_backend` to accept `get_tab`:
```python
def create_capture_backend(
    backend_type: str, driver: Any, get_tab: Callable[[], Any] | None = None
) -> CaptureBackend:
```
Pass `get_tab` to `NodriverCaptureBackend`.

6. Add `start_capture_async()` to the protocol and implement CDP network monitoring on `NodriverCaptureBackend` for HAR data:

Add to `CaptureBackend` protocol:
```python
async def start_capture_async(self) -> None:
    """Begin capturing browser data asynchronously (CDP event listeners)."""
    ...
```

Add to `NodriverCaptureBackend`:
```python
async def start_capture_async(self) -> None:
    """Enable CDP network event collection for HAR data.

    Must be called after the tab is available. Adds a handler for
    Network.responseReceived events that populates self._har_entries.
    """
    tab = self._tab
    if tab is None:
        return
    import nodriver.cdp.network as cdp_net

    await tab.send(cdp_net.enable())
    tab.add_handler(cdp_net.ResponseReceived, self._on_response)
    LOG.debug("nodriver_network_capture_enabled")

    # TODO: Implement CDP-based console log capture.
    # This requires enabling cdp.runtime.enable() and adding a
    # handler for cdp.runtime.ConsoleAPICalled events on the tab.
    # See: nodriver-capture-backend-missing-async-screenshot.md

def _on_response(self, event: Any) -> None:
    """Handle Network.responseReceived CDP event."""
    try:
        self._har_entries.append({
            "url": event.response.url,
            "status": event.response.status,
            "mime_type": event.response.mime_type,
            "headers": dict(event.response.headers) if event.response.headers else {},
            "timestamp": event.timestamp,
        })
    except Exception as exc:
        LOG.error("nodriver_har_entry_failed", error=str(exc))
```

Add to `SeleniumCaptureBackend` (trivial wrapper):
```python
async def start_capture_async(self) -> None:
    """Begin capturing (no-op for Selenium, uses sync start_capture)."""
    self.start_capture()
```

7. Add tests for HAR capture:

```python
# In TestNodriverCaptureBackend:

@pytest.mark.asyncio
async def test_start_capture_async_enables_network(self) -> None:
    from unittest.mock import AsyncMock, call

    mock_tab = AsyncMock()
    mock_tab.add_handler = MagicMock()
    backend = NodriverCaptureBackend(browser=MagicMock(), get_tab=lambda: mock_tab)
    await backend.start_capture_async()
    mock_tab.send.assert_called_once()
    mock_tab.add_handler.assert_called_once()

def test_on_response_appends_har_entry(self) -> None:
    from unittest.mock import MagicMock

    backend = NodriverCaptureBackend(browser=MagicMock())
    mock_event = MagicMock()
    mock_event.response.url = "https://example.com/api"
    mock_event.response.status = 200
    mock_event.response.mime_type = "application/json"
    mock_event.response.headers = {"content-type": "application/json"}
    mock_event.timestamp = 1234567890.0

    backend._on_response(mock_event)
    assert len(backend.get_har_entries()) == 1
    assert backend.get_har_entries()[0]["url"] == "https://example.com/api"
    assert backend.get_har_entries()[0]["status"] == 200

def test_on_response_handles_exception_gracefully(self) -> None:
    from unittest.mock import MagicMock

    backend = NodriverCaptureBackend(browser=MagicMock())
    mock_event = MagicMock()
    mock_event.response = None  # Will cause AttributeError

    backend._on_response(mock_event)  # Should not raise
    assert len(backend.get_har_entries()) == 0
```

8. Add a TODO comment for deferred console log capture (only console logs are deferred now):
```python
# TODO: Implement CDP-based console log capture.
# This requires enabling cdp.runtime.enable() and adding a
# handler for cdp.runtime.ConsoleAPICalled events on the tab.
```

**Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_observe.py -v`
Expected: All PASS

**Step 5: Commit**

```bash
git add src/graftpunk/observe/capture.py tests/unit/test_observe.py
git commit -m "feat: add async screenshot and page source to capture backends"
```

---

### Task 2: Add `screenshot_async()` to ObservabilityContext

**Files:**
- Modify: `src/graftpunk/observe/context.py`
- Test: `tests/unit/test_observe.py`

**Step 1: Write the failing tests**

```python
# In TestObservabilityContext (new async tests):

@pytest.mark.asyncio
async def test_screenshot_async_saves_file(self, tmp_path: Path) -> None:
    from unittest.mock import AsyncMock

    storage = ObserveStorage(tmp_path, "test-session", "run-001")
    capture = AsyncMock()
    capture.take_screenshot = AsyncMock(return_value=b"\x89PNGfake")

    ctx = ObservabilityContext(capture=capture, storage=storage, mode="full")
    path = await ctx.screenshot_async("test-label")
    assert path is not None
    assert path.exists()
    assert b"\x89PNGfake" in path.read_bytes()

@pytest.mark.asyncio
async def test_screenshot_async_noop_when_off(self) -> None:
    ctx = NoOpObservabilityContext()
    result = await ctx.screenshot_async("test")
    assert result is None

@pytest.mark.asyncio
async def test_screenshot_async_returns_none_on_capture_failure(self, tmp_path: Path) -> None:
    from unittest.mock import AsyncMock

    storage = ObserveStorage(tmp_path, "test-session", "run-001")
    capture = AsyncMock()
    capture.take_screenshot = AsyncMock(return_value=None)

    ctx = ObservabilityContext(capture=capture, storage=storage, mode="full")
    result = await ctx.screenshot_async("fail-label")
    assert result is None
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_observe.py -v -k "screenshot_async"`
Expected: FAIL

**Step 3: Implement**

In `context.py`, add to `ObservabilityContext`:

```python
async def screenshot_async(self, label: str) -> Path | None:
    """Take a screenshot asynchronously (works with nodriver).

    Args:
        label: Descriptive label for the screenshot file.

    Returns:
        Path to saved screenshot, or None if observability is off or capture failed.
    """
    if self._mode == "off" or self._capture is None or self._storage is None:
        return None
    self._counter += 1
    data = await self._capture.take_screenshot()
    if data is None:
        return None
    return self._storage.save_screenshot(self._counter, label, data)
```

**Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_observe.py -v`
Expected: All PASS

**Step 5: Commit**

```bash
git add src/graftpunk/observe/context.py tests/unit/test_observe.py
git commit -m "feat: add screenshot_async to ObservabilityContext"
```

---

### Task 3: Add active session context (`gp session use/unset`)

**Files:**
- Create: `src/graftpunk/session_context.py`
- Modify: `src/graftpunk/cli/session_commands.py`
- Test: `tests/unit/test_session_context.py`
- Test: `tests/unit/test_cli.py` (add tests for use/unset commands)

**Step 1: Write the failing tests for session_context.py**

Create `tests/unit/test_session_context.py`:

```python
"""Tests for active session context management."""

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from graftpunk.session_context import (
    clear_active_session,
    get_active_session,
    resolve_session,
    set_active_session,
)


class TestGetActiveSession:
    def test_returns_none_when_no_env_and_no_file(self, tmp_path: Path) -> None:
        with patch.dict(os.environ, {}, clear=True):
            result = get_active_session(search_dir=tmp_path)
        assert result is None

    def test_env_var_takes_priority(self, tmp_path: Path) -> None:
        (tmp_path / ".gp-session").write_text("from-file")
        with patch.dict(os.environ, {"GRAFTPUNK_SESSION": "from-env"}):
            result = get_active_session(search_dir=tmp_path)
        assert result == "from-env"

    def test_reads_file_when_no_env(self, tmp_path: Path) -> None:
        (tmp_path / ".gp-session").write_text("mysession\n")
        with patch.dict(os.environ, {}, clear=True):
            result = get_active_session(search_dir=tmp_path)
        assert result == "mysession"

    def test_strips_whitespace_from_file(self, tmp_path: Path) -> None:
        (tmp_path / ".gp-session").write_text("  mysession  \n")
        with patch.dict(os.environ, {}, clear=True):
            result = get_active_session(search_dir=tmp_path)
        assert result == "mysession"

    def test_returns_none_for_empty_file(self, tmp_path: Path) -> None:
        (tmp_path / ".gp-session").write_text("")
        with patch.dict(os.environ, {}, clear=True):
            result = get_active_session(search_dir=tmp_path)
        assert result is None


class TestSetActiveSession:
    def test_writes_session_file(self, tmp_path: Path) -> None:
        set_active_session("mysite", directory=tmp_path)
        assert (tmp_path / ".gp-session").read_text() == "mysite"

    def test_overwrites_existing(self, tmp_path: Path) -> None:
        set_active_session("first", directory=tmp_path)
        set_active_session("second", directory=tmp_path)
        assert (tmp_path / ".gp-session").read_text() == "second"


class TestClearActiveSession:
    def test_removes_file(self, tmp_path: Path) -> None:
        (tmp_path / ".gp-session").write_text("mysession")
        clear_active_session(directory=tmp_path)
        assert not (tmp_path / ".gp-session").exists()

    def test_noop_when_no_file(self, tmp_path: Path) -> None:
        clear_active_session(directory=tmp_path)  # Should not raise


class TestResolveSession:
    def test_explicit_wins(self, tmp_path: Path) -> None:
        (tmp_path / ".gp-session").write_text("from-file")
        result = resolve_session(explicit="explicit", search_dir=tmp_path)
        assert result == "explicit"

    def test_falls_back_to_active(self, tmp_path: Path) -> None:
        (tmp_path / ".gp-session").write_text("from-file")
        with patch.dict(os.environ, {}, clear=True):
            result = resolve_session(explicit=None, search_dir=tmp_path)
        assert result == "from-file"

    def test_returns_none_when_nothing(self, tmp_path: Path) -> None:
        with patch.dict(os.environ, {}, clear=True):
            result = resolve_session(explicit=None, search_dir=tmp_path)
        assert result is None
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_session_context.py -v`
Expected: FAIL (module doesn't exist)

**Step 3: Implement session_context.py**

Create `src/graftpunk/session_context.py`:

```python
"""Active session context management.

Resolution order: GRAFTPUNK_SESSION env var > .gp-session file in cwd > None.
"""

from __future__ import annotations

import os
from pathlib import Path

SESSION_FILE_NAME = ".gp-session"


def get_active_session(search_dir: Path | None = None) -> str | None:
    """Read the active session from env var or .gp-session file.

    Resolution order:
        1. GRAFTPUNK_SESSION environment variable (per-shell override)
        2. .gp-session file in search_dir (or cwd if not specified)

    Args:
        search_dir: Directory to look for .gp-session file. Defaults to cwd.

    Returns:
        Session name, or None if not set.
    """
    env = os.environ.get("GRAFTPUNK_SESSION")
    if env:
        return env.strip()

    directory = search_dir or Path.cwd()
    session_file = directory / SESSION_FILE_NAME
    if session_file.is_file():
        content = session_file.read_text().strip()
        return content or None
    return None


def set_active_session(name: str, directory: Path | None = None) -> Path:
    """Write the active session to .gp-session file.

    Args:
        name: Session name to set as active.
        directory: Directory to write .gp-session file in. Defaults to cwd.

    Returns:
        Path to the written file.
    """
    target = (directory or Path.cwd()) / SESSION_FILE_NAME
    target.write_text(name)
    return target


def clear_active_session(directory: Path | None = None) -> None:
    """Remove the .gp-session file.

    Args:
        directory: Directory containing the .gp-session file. Defaults to cwd.
    """
    target = (directory or Path.cwd()) / SESSION_FILE_NAME
    target.unlink(missing_ok=True)


def resolve_session(explicit: str | None, search_dir: Path | None = None) -> str | None:
    """Resolve session name: explicit flag > active session > None.

    Args:
        explicit: Explicit --session flag value, or None.
        search_dir: Directory for .gp-session lookup. Defaults to cwd.

    Returns:
        Resolved session name, or None.
    """
    if explicit:
        return explicit
    return get_active_session(search_dir=search_dir)
```

**Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_session_context.py -v`
Expected: All PASS

**Step 5: Write failing tests for CLI commands (use/unset)**

Add to `tests/unit/test_cli.py`:

```python
class TestSessionUseCommand:
    def test_sets_active_session(self, cli_runner, tmp_path):
        with patch("graftpunk.cli.session_commands.Path.cwd", return_value=tmp_path):
            with patch("graftpunk.cli.session_commands.resolve_session_name", return_value="bekentree"):
                result = cli_runner.invoke(app, ["session", "use", "bek"])
        assert result.exit_code == 0
        assert "bekentree" in result.output

    def test_use_shows_resolved_name(self, cli_runner, tmp_path):
        with patch("graftpunk.cli.session_commands.Path.cwd", return_value=tmp_path):
            with patch("graftpunk.cli.session_commands.resolve_session_name", return_value="bekentree"):
                result = cli_runner.invoke(app, ["session", "use", "bek"])
        assert "bekentree" in result.output
        assert (tmp_path / ".gp-session").read_text() == "bekentree"


class TestSessionUnsetCommand:
    def test_clears_active_session(self, cli_runner, tmp_path):
        (tmp_path / ".gp-session").write_text("bekentree")
        with patch("graftpunk.cli.session_commands.Path.cwd", return_value=tmp_path):
            result = cli_runner.invoke(app, ["session", "unset"])
        assert result.exit_code == 0
        assert not (tmp_path / ".gp-session").exists()

    def test_unset_when_no_session(self, cli_runner, tmp_path):
        with patch("graftpunk.cli.session_commands.Path.cwd", return_value=tmp_path):
            result = cli_runner.invoke(app, ["session", "unset"])
        assert result.exit_code == 0
```

**Step 6: Implement use/unset commands**

Add to `src/graftpunk/cli/session_commands.py`:

```python
from graftpunk.session_context import clear_active_session, get_active_session, set_active_session

@session_app.command("use")
def session_use(
    name: Annotated[
        str,
        typer.Argument(help="Session name or plugin alias to set as active", metavar="SESSION"),
    ],
) -> None:
    """Set the active session for subsequent commands.

    Writes a .gp-session file in the current directory.
    Override per-shell with GRAFTPUNK_SESSION env var.
    """
    resolved = resolve_session_name(name)
    path = set_active_session(resolved)
    console.print(f"[green]Active session set to '{resolved}'[/green]")
    if resolved != name:
        console.print(f"[dim](resolved from plugin '{name}')[/dim]")
    console.print(f"[dim]Written to: {path}[/dim]")


@session_app.command("unset")
def session_unset() -> None:
    """Clear the active session.

    Removes the .gp-session file in the current directory.
    """
    clear_active_session()
    console.print("[green]Active session cleared[/green]")
```

**Step 7: Run all tests**

Run: `uv run pytest tests/unit/test_session_context.py tests/unit/test_cli.py -v`
Expected: All PASS

**Step 8: Commit**

```bash
git add src/graftpunk/session_context.py tests/unit/test_session_context.py src/graftpunk/cli/session_commands.py tests/unit/test_cli.py
git commit -m "feat: add active session context (gp session use/unset)"
```

---

### Task 4: Add `--session` flag to observe group callback

**Files:**
- Modify: `src/graftpunk/cli/main.py`
- Test: `tests/unit/test_cli.py`

**Step 1: Write the failing tests**

```python
class TestObserveSessionFlag:
    def test_observe_list_with_session_flag(self, cli_runner, tmp_path):
        """--session on observe should scope list to that session."""
        # Create observe data for two sessions
        base = tmp_path / "observe"
        (base / "site-a" / "run-001").mkdir(parents=True)
        (base / "site-b" / "run-001").mkdir(parents=True)

        with patch("graftpunk.cli.main.OBSERVE_BASE_DIR", base):
            with patch("graftpunk.cli.main.resolve_session_name", return_value="site-a"):
                result = cli_runner.invoke(app, ["observe", "--session", "site-a", "list"])
        assert result.exit_code == 0
        assert "site-a" in result.output
        # site-b should NOT appear when scoped
        assert "site-b" not in result.output

    def test_observe_list_without_session_shows_all(self, cli_runner, tmp_path):
        base = tmp_path / "observe"
        (base / "site-a" / "run-001").mkdir(parents=True)
        (base / "site-b" / "run-001").mkdir(parents=True)

        with patch("graftpunk.cli.main.OBSERVE_BASE_DIR", base):
            result = cli_runner.invoke(app, ["observe", "list"])
        assert result.exit_code == 0
        assert "site-a" in result.output
        assert "site-b" in result.output
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_cli.py -v -k "ObserveSession"`
Expected: FAIL

**Step 3: Implement**

In `main.py`, add a callback to observe_app that captures `--session`:

```python
from graftpunk.cli.plugin_commands import resolve_session_name
from graftpunk.session_context import resolve_session

@observe_app.callback()
def observe_callback(
    ctx: typer.Context,
    session: Annotated[
        str | None,
        typer.Option("--session", "-s", help="Session name to scope observe commands to"),
    ] = None,
) -> None:
    """View and manage observability data."""
    # resolve_session checks explicit > env > .gp-session > None
    resolved = resolve_session(session)
    if resolved and session:
        resolved = resolve_session_name(resolved)
    ctx.ensure_object(dict)["observe_session"] = resolved
```

Then update `observe_list`, `observe_show`, and `observe_clean` to use `ctx.obj["observe_session"]` when set.

For `observe_list`: if session is set, only iterate that session's subdirectory.
For `observe_show`: use session from context if positional `session_name` not provided (make it optional).
For `observe_clean`: use session from context if positional `session_name` not provided.

**Step 4: Run tests**

Run: `uv run pytest tests/unit/test_cli.py -v`
Expected: All PASS

**Step 5: Commit**

```bash
git add src/graftpunk/cli/main.py tests/unit/test_cli.py
git commit -m "feat: add --session flag to observe subcommand group"
```

---

### Task 5: Add cookie injection (session → browser)

**Files:**
- Modify: `src/graftpunk/session.py`
- Test: `tests/unit/test_session.py`

**Step 1: Write the failing tests**

```python
class TestInjectCookiesToNodriver:
    @pytest.mark.asyncio
    async def test_injects_cookies_via_cdp(self) -> None:
        from unittest.mock import AsyncMock, MagicMock

        from requests.cookies import RequestsCookieJar

        from graftpunk.session import inject_cookies_to_nodriver

        jar = RequestsCookieJar()
        jar.set("session_id", "abc123", domain=".example.com", path="/")
        jar.set("csrf", "xyz", domain=".example.com", path="/")

        mock_tab = AsyncMock()
        result = await inject_cookies_to_nodriver(mock_tab, jar)
        assert result == 2
        mock_tab.send.assert_called_once()

    @pytest.mark.asyncio
    async def test_returns_zero_for_empty_jar(self) -> None:
        from unittest.mock import AsyncMock

        from requests.cookies import RequestsCookieJar

        from graftpunk.session import inject_cookies_to_nodriver

        jar = RequestsCookieJar()
        mock_tab = AsyncMock()
        result = await inject_cookies_to_nodriver(mock_tab, jar)
        assert result == 0
        mock_tab.send.assert_not_called()
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_session.py -v -k "inject_cookies"`
Expected: FAIL

**Step 3: Implement**

Add to `session.py` as a standalone async function:

```python
async def inject_cookies_to_nodriver(
    tab: Any, cookies: requests.cookies.RequestsCookieJar
) -> int:
    """Inject cached session cookies into a nodriver browser tab via CDP.

    This is the inverse of transfer_nodriver_cookies_to_session() — it loads
    cookies FROM a cached RequestsCookieJar INTO a nodriver browser.

    Can be called before any navigation. CookieParam includes the domain
    field, so CDP can set cookies on any domain without needing to be on
    that domain first.

    Args:
        tab: nodriver Tab instance (the active browser tab).
        cookies: RequestsCookieJar from a cached session.

    Returns:
        Number of cookies injected.
    """
    import nodriver.cdp.network as cdp_net
    import nodriver.cdp.storage as cdp_storage

    # IMPORTANT: Do NOT use nodriver's browser.cookies.set_all() — it has a bug
    # where set_all() calls get_all() first, then overwrites the caller's
    # cookies parameter with existing browser cookies, effectively ignoring input.
    # Use the low-level CDP approach instead.
    cookie_params = []
    for cookie in cookies:
        cookie_params.append(
            cdp_net.CookieParam(
                name=cookie.name,
                value=cookie.value,
                domain=cookie.domain,
                path=cookie.path,
            )
        )
    if cookie_params:
        await tab.send(cdp_storage.set_cookies(cookie_params))
    return len(cookie_params)
```

**Step 4: Run tests**

Run: `uv run pytest tests/unit/test_session.py -v -k "inject_cookies"`
Expected: All PASS

**Step 5: Commit**

```bash
git add src/graftpunk/session.py tests/unit/test_session.py
git commit -m "feat: add inject_cookies_to_nodriver for session-to-browser cookie transfer"
```

---

### Task 6: Implement `gp observe go <URL>`

**Files:**
- Modify: `src/graftpunk/cli/main.py`
- Test: `tests/unit/test_cli.py`

**Step 1: Write the failing tests**

```python
class TestObserveGoCommand:
    def test_requires_session(self, cli_runner):
        """observe go without session should error."""
        with patch("graftpunk.cli.main.resolve_session", return_value=None):
            result = cli_runner.invoke(app, ["observe", "go", "https://example.com"])
        assert result.exit_code != 0
        assert "session" in result.output.lower()

    def test_observe_go_with_session_flag(self, cli_runner):
        """observe go --session should run the capture flow."""
        with patch("graftpunk.cli.main.resolve_session_name", return_value="mysite"):
            with patch("graftpunk.cli.main._run_observe_go") as mock_run:
                result = cli_runner.invoke(
                    app, ["observe", "--session", "mysite", "go", "https://example.com"]
                )
        # The actual async flow is tested separately;
        # here we verify the CLI wiring calls _run_observe_go
        mock_run.assert_called_once()
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_cli.py -v -k "ObserveGo"`
Expected: FAIL

**Step 3: Implement**

Add to `main.py`:

```python
import asyncio

@observe_app.command("go")
def observe_go(
    ctx: typer.Context,
    url: Annotated[
        str,
        typer.Argument(help="The URL to navigate to and capture"),
    ],
    wait: Annotated[
        float,
        typer.Option("--wait", "-w", help="Seconds to wait after page load"),
    ] = 3.0,
) -> None:
    """Open a URL in an authenticated browser and capture observability data.

    Loads the cached session cookies, opens a nodriver browser, injects
    cookies, navigates to the URL, and captures screenshots and page source.

    Requires an active session (via --session, GRAFTPUNK_SESSION, or .gp-session).
    """
    session_name = ctx.obj.get("observe_session") if ctx.obj else None
    if not session_name:
        console.print("[red]No session specified. Use --session, GRAFTPUNK_SESSION, or gp session use.[/red]")
        raise typer.Exit(1)

    asyncio.run(_run_observe_go(session_name, url, wait))


async def _run_observe_go(session_name: str, url: str, wait: float) -> None:
    """Async implementation of observe go.

    1. Load cached session cookies
    2. Start nodriver browser
    3. Inject cookies via CDP (before any navigation — CookieParam has domain)
    4. Enable HAR capture (CDP network monitoring)
    5. Navigate to target URL
    6. Wait for page load
    7. Capture screenshot, page source, and HAR
    8. Save to observability storage
    """
    import datetime

    import nodriver

    from graftpunk.observe.capture import NodriverCaptureBackend
    from graftpunk.observe.storage import ObserveStorage
    from graftpunk.session import inject_cookies_to_nodriver

    # 1. Load session
    try:
        session = load_session(session_name)
    except Exception as exc:
        console.print(f"[red]Failed to load session '{session_name}': {exc}[/red]")
        return

    # 2. Start browser
    browser = await nodriver.start()
    tab = browser.main_tab

    try:
        # 3. Inject cookies BEFORE navigation.
        # CookieParam includes the domain field, so CDP can set cookies
        # on any domain without needing to be on that domain first.
        count = await inject_cookies_to_nodriver(tab, session.cookies)
        console.print(f"[dim]Injected {count} cookie(s)[/dim]")

        # 4. Set up capture backend and enable HAR (network monitoring)
        run_id = datetime.datetime.now().strftime("%Y%m%d-%H%M%S") + f"-{os.getpid()}"
        storage = ObserveStorage(OBSERVE_BASE_DIR, session_name, run_id)
        backend = NodriverCaptureBackend(browser, get_tab=lambda: tab)
        await backend.start_capture_async()

        # 5. Navigate to target URL (cookies already set, HAR recording)
        tab = await browser.get(url)

        # 6. Wait for page load
        await tab.sleep(wait)

        # 7. Capture screenshot and page source
        screenshot_data = await backend.take_screenshot()
        if screenshot_data:
            path = storage.save_screenshot(1, "observe-go", screenshot_data)
            console.print(f"[green]Screenshot saved:[/green] {path}")
        else:
            console.print("[yellow]Screenshot capture failed[/yellow]")

        page_source = await backend.get_page_source()
        if page_source:
            source_path = storage.run_dir / "page-source.html"
            source_path.write_text(page_source, encoding="utf-8")
            console.print(f"[green]Page source saved:[/green] {source_path}")

        # 8. Write HAR and summary
        har_entries = backend.get_har_entries()
        if har_entries:
            storage.write_har(har_entries)
            console.print(f"[green]HAR data saved:[/green] {len(har_entries)} entries")

        # TODO: Capture console logs via CDP runtime monitoring.

        console.print(f"\n[bold]Observe run:[/bold] {storage.run_dir}")

    finally:
        browser.stop()
```

Also add `load_session` import at top of main.py:
```python
from graftpunk import load_session
```

**Step 4: Run tests**

Run: `uv run pytest tests/unit/test_cli.py -v -k "ObserveGo"`
Expected: All PASS

**Step 5: Run full test suite and quality checks**

```bash
uv run pytest tests/ -v
uvx ruff check .
uvx ruff format --check .
uvx ty check src/
```

**Step 6: Commit**

```bash
git add src/graftpunk/cli/main.py tests/unit/test_cli.py
git commit -m "feat: add gp observe go command for authenticated browser capture"
```

---

### Task 7: Full verification and push

**Step 1: Run full test suite**

```bash
uv run pytest tests/ -v
```

Expected: All tests pass.

**Step 2: Run quality checks**

```bash
uvx ruff check .
uvx ruff format --check .
uvx ty check src/
```

Expected: All clean.

**Step 3: Verify CLI commands work**

```bash
gp session use --help
gp session unset --help
gp observe go --help
gp observe --help  # Should show --session flag
```

**Step 4: Push**

```bash
git push
```

---

## Summary of features delivered

| Feature | Command / API | Files |
|---------|--------------|-------|
| Async screenshots (nodriver) | `backend.take_screenshot()` | `capture.py`, `context.py` |
| Async page source (nodriver) | `backend.get_page_source()` | `capture.py` |
| CDP HAR capture (nodriver) | `backend.start_capture_async()` + `get_har_entries()` | `capture.py` |
| Async screenshot context | `ctx.screenshot_async(label)` | `context.py` |
| Active session context | `gp session use/unset` | `session_context.py`, `session_commands.py` |
| Session resolution | env > .gp-session > None | `session_context.py` |
| Observe session scoping | `gp observe --session` | `main.py` |
| Cookie injection | `inject_cookies_to_nodriver()` | `session.py` |
| Interactive observability | `gp observe go <URL>` | `main.py` |

## Deferred (with TODO comments in code)

- CDP-based console log capture for nodriver
- Wiring `get_tab` into `BrowserSession._start_observe()` (currently only used by `observe go`)
