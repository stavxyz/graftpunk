# Session Command Group Implementation Plan

**Goal:** Move `gp list/show/clear/export` under `gp session` subcommand group, redesign `clear` to support name/domain/all targeting, add session name validation (no dots allowed).

**Architecture:** Create `session_commands.py` with a Typer subapp (same pattern as `observe_app`, `keepalive_app`). Move all 4 session commands there. Redesign `clear` with smart target resolution: no dots = name, dots = domain, `--all` = everything. Add `validate_session_name()` in `cache.py` and call it from `cache_session()`. Remove old top-level commands from `main.py`.

**Tech Stack:** Typer, Rich (Console/Table/Panel), pytest with CliRunner

---

### Task 1: Session name validation in cache.py

Add `validate_session_name()` and wire it into `cache_session()`.

**Files:**
- Modify: `src/graftpunk/cache.py:171-224`
- Modify: `tests/unit/test_cache.py`

**Step 1: Write the failing tests**

Add to `tests/unit/test_cache.py`:

```python
import re
import pytest
from graftpunk.cache import validate_session_name

class TestValidateSessionName:
    """Tests for session name validation."""

    def test_valid_names(self):
        """Valid session names don't raise."""
        for name in ["hackernews", "my-site", "my_site", "site123", "a", "a1b2"]:
            validate_session_name(name)  # Should not raise

    def test_rejects_dots(self):
        """Session names with dots are rejected (dots indicate domains)."""
        with pytest.raises(ValueError, match="cannot contain dots"):
            validate_session_name("example.com")

    def test_rejects_empty(self):
        """Empty session names are rejected."""
        with pytest.raises(ValueError, match="must be non-empty"):
            validate_session_name("")

    def test_rejects_invalid_characters(self):
        """Session names with invalid characters are rejected."""
        for name in ["my site", "my/site", "MY_SITE", "Site", "@site"]:
            with pytest.raises(ValueError, match="must match"):
                validate_session_name(name)

    def test_rejects_leading_hyphen(self):
        """Session names starting with hyphen are rejected."""
        with pytest.raises(ValueError, match="must match"):
            validate_session_name("-my-site")

    def test_rejects_leading_underscore(self):
        """Session names starting with underscore are rejected."""
        with pytest.raises(ValueError, match="must match"):
            validate_session_name("_my-site")
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_cache.py::TestValidateSessionName -v`
Expected: FAIL (ImportError — `validate_session_name` doesn't exist yet)

**Step 3: Implement validate_session_name**

Add to `src/graftpunk/cache.py` after the imports (before `_get_session_storage_backend`):

```python
import re

# Session names: lowercase alphanumeric, hyphens, underscores. No dots (dots indicate domains).
_SESSION_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


def validate_session_name(name: str) -> None:
    """Validate a session name.

    Session names must be lowercase alphanumeric with hyphens/underscores,
    starting with a letter or digit. Dots are not allowed (they indicate domains).

    Raises:
        ValueError: If name is invalid.
    """
    if not name:
        raise ValueError("Session name must be non-empty")
    if "." in name:
        raise ValueError(
            f"Session name {name!r} cannot contain dots. "
            "Dots are reserved for domain matching in 'gp session clear'."
        )
    if not _SESSION_NAME_RE.match(name):
        raise ValueError(
            f"Session name {name!r} must match pattern [a-z0-9][a-z0-9_-]* "
            "(lowercase alphanumeric, hyphens, underscores)"
        )
```

Wire it into `cache_session()` — add `validate_session_name(session_name)` right after `session_name` is resolved (after line ~187, before `backend = _get_session_storage_backend()`):

```python
def cache_session(session: T, session_name: str | None = None) -> str:
    ...
    if session_name is None:
        session_name = getattr(session, "session_name", "default")

    validate_session_name(session_name)  # <-- ADD THIS LINE

    backend = _get_session_storage_backend()
    ...
```

**Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_cache.py::TestValidateSessionName -v`
Expected: PASS (all 6 tests)

Also run: `uv run pytest tests/ -v --tb=short` to make sure nothing else breaks.

**Step 5: Commit**

```bash
git add src/graftpunk/cache.py tests/unit/test_cache.py
git commit -m "feat: add session name validation (no dots allowed)

Session names must match [a-z0-9][a-z0-9_-]* — lowercase alphanumeric
with hyphens/underscores. Dots are reserved for domain matching in
the upcoming 'gp session clear' command.

Co-Authored-By: stavxyz <hi@stav.xyz>"
```

---

### Task 2: Create session_commands.py with list command

Move `gp list` to `gp session list`.

**Files:**
- Create: `src/graftpunk/cli/session_commands.py`
- Modify: `src/graftpunk/cli/main.py` (remove `list_cmd`, register `session_app`)
- Modify: `tests/unit/test_cli.py` (update `TestListCommand`)

**Step 1: Write the updated tests**

In `tests/unit/test_cli.py`, update `TestListCommand` to use `["session", "list"]` instead of `["list"]`. Also update mock paths from `graftpunk.cli.main` to `graftpunk.cli.session_commands`:

```python
class TestListCommand:
    """Tests for session list command."""

    @patch("graftpunk.cli.session_commands.list_sessions_with_metadata")
    def test_list_empty(self, mock_list):
        """Test list command with no sessions."""
        mock_list.return_value = []
        result = runner.invoke(app, ["session", "list"])
        assert result.exit_code == 0
        assert "No Sessions" in result.output or "No sessions" in result.output

    @patch("graftpunk.cli.session_commands.list_sessions_with_metadata")
    def test_list_with_sessions(self, mock_list):
        """Test list command with sessions."""
        mock_list.return_value = [
            {
                "name": "test-session",
                "domain": "example.com",
                "status": "active",
                "cookie_count": 5,
                "modified_at": "2026-01-01T00:00:00",
            }
        ]
        result = runner.invoke(app, ["session", "list"])
        assert result.exit_code == 0
        assert "test-session" in result.output

    @patch("graftpunk.cli.session_commands.list_sessions_with_metadata")
    def test_list_json_output(self, mock_list):
        """Test list command with JSON output."""
        mock_list.return_value = [{"name": "test", "domain": "example.com"}]
        result = runner.invoke(app, ["session", "list", "--json"])
        assert result.exit_code == 0
        assert '"name": "test"' in result.output

    @patch("graftpunk.cli.session_commands.list_sessions_with_metadata")
    def test_list_logged_out_status(self, mock_list):
        """Test list command displays logged_out status correctly."""
        mock_list.return_value = [
            {
                "name": "old-session",
                "domain": "example.com",
                "status": "logged_out",
                "cookie_count": 0,
                "modified_at": "2026-01-01T00:00:00",
            }
        ]
        result = runner.invoke(app, ["session", "list"])
        assert result.exit_code == 0
        output = strip_ansi(result.output)
        assert "logged out" in output

    @patch("graftpunk.cli.session_commands.list_sessions_with_metadata")
    def test_list_unknown_status(self, mock_list):
        """Test list command displays unknown/custom status correctly."""
        mock_list.return_value = [
            {
                "name": "weird-session",
                "domain": "example.com",
                "status": "unknown",
                "cookie_count": 1,
                "modified_at": "",
            }
        ]
        result = runner.invoke(app, ["session", "list"])
        assert result.exit_code == 0
        output = strip_ansi(result.output)
        assert "unknown" in output
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_cli.py::TestListCommand -v`
Expected: FAIL (command "session" doesn't exist yet)

**Step 3: Create session_commands.py with list command**

Create `src/graftpunk/cli/session_commands.py`:

```python
"""Session management commands — gp session list/show/clear/export."""

from typing import Annotated

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from graftpunk import list_sessions_with_metadata

session_app = typer.Typer(
    name="session",
    help="Manage encrypted browser sessions.",
    no_args_is_help=True,
)
console = Console()


@session_app.command("list")
def session_list(
    json_output: Annotated[
        bool,
        typer.Option("--json", "-j", help="Output as JSON for scripting"),
    ] = False,
) -> None:
    """List all cached sessions with status and metadata."""
    sessions = list_sessions_with_metadata()

    if not sessions:
        console.print(
            Panel(
                "[dim]No sessions cached yet.[/dim]\n\n"
                "Use a plugin to log in and create a session:\n"
                "[cyan]gp <plugin> login[/cyan]",
                title="No Sessions",
                border_style="yellow",
            )
        )
        return

    if json_output:
        import json

        console.print(json.dumps(sessions, indent=2, default=str))
        return

    table = Table(
        title="Cached Sessions",
        title_style="bold",
        header_style="bold cyan",
        border_style="dim",
    )
    table.add_column("Session", style="cyan", no_wrap=True)
    table.add_column("Domain", style="white")
    table.add_column("Status", justify="center")
    table.add_column("Cookies", justify="right", style="dim")
    table.add_column("Last Modified", style="dim")

    for session in sessions:
        status = session.get("status", "unknown")
        if status == "active":
            status_display = "[green]● active[/green]"
        elif status == "logged_out":
            status_display = "[red]○ logged out[/red]"
        else:
            status_display = f"[yellow]? {status}[/yellow]"

        modified = session.get("modified_at", "")
        if modified:
            modified = modified[:16].replace("T", " ")

        table.add_row(
            session.get("name", ""),
            session.get("domain") or "[dim]—[/dim]",
            status_display,
            str(session.get("cookie_count", 0)),
            modified or "[dim]—[/dim]",
        )

    console.print(table)
    console.print(f"\n[dim]{len(sessions)} session(s) cached[/dim]")
```

In `src/graftpunk/cli/main.py`:
1. Remove the `list_cmd` function (lines 136-202) and its `@app.command("list")` decorator
2. Add import: `from graftpunk.cli.session_commands import session_app`
3. Add registration: `app.add_typer(session_app)` (next to the `observe_app` and `keepalive_app` registrations)
4. Remove `list_sessions_with_metadata` from the `graftpunk` import if no longer used in main.py

**Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_cli.py::TestListCommand -v`
Expected: PASS

Also run: `uv run pytest tests/ -v --tb=short`

**Step 5: Commit**

```bash
git add src/graftpunk/cli/session_commands.py src/graftpunk/cli/main.py tests/unit/test_cli.py
git commit -m "feat: move list command to 'gp session list'

Create session_commands.py with Typer subapp. Move list command
from top-level to session group.

Co-Authored-By: stavxyz <hi@stav.xyz>"
```

---

### Task 3: Move show and export commands

Move `gp show` and `gp export` to `gp session show` and `gp session export`.

**Files:**
- Modify: `src/graftpunk/cli/session_commands.py`
- Modify: `src/graftpunk/cli/main.py` (remove `show` and `export`)
- Modify: `tests/unit/test_cli.py` (update `TestShowCommand`, `TestExportCommand`)

**Step 1: Update tests**

In `tests/unit/test_cli.py`:
- `TestShowCommand`: Change all `["show", ...]` to `["session", "show", ...]` and mock paths from `graftpunk.cli.main` to `graftpunk.cli.session_commands`
- `TestExportCommand`: Change all `["export", ...]` to `["session", "export", ...]` and mock paths from `graftpunk.cli.main` to `graftpunk.cli.session_commands`

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_cli.py::TestShowCommand tests/unit/test_cli.py::TestExportCommand -v`
Expected: FAIL

**Step 3: Move show and export to session_commands.py**

Add to `src/graftpunk/cli/session_commands.py`:

```python
from typing import Annotated, cast, TYPE_CHECKING
from graftpunk import get_session_metadata, load_session
from graftpunk.cli.plugin_commands import resolve_session_name
from graftpunk.exceptions import GraftpunkError, SessionExpiredError, SessionNotFoundError

if TYPE_CHECKING:
    from graftpunk.session import BrowserSession
```

Then add the `show` and `export` command functions (copy from main.py, updating `console` reference to use the module-level one).

Remove from `main.py`:
- The `show` function and its decorator (lines 205-274)
- The `export` function and its decorator (lines 328-374)
- Remove unused imports: `get_session_metadata`, `load_session`, `cast`, `Path` (if no longer used), `GraftpunkError`, `SessionExpiredError`, `SessionNotFoundError` (if no longer used)

**Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_cli.py::TestShowCommand tests/unit/test_cli.py::TestExportCommand -v`
Expected: PASS

Also run: `uv run pytest tests/ -v --tb=short`

**Step 5: Commit**

```bash
git add src/graftpunk/cli/session_commands.py src/graftpunk/cli/main.py tests/unit/test_cli.py
git commit -m "feat: move show and export to 'gp session show/export'

Co-Authored-By: stavxyz <hi@stav.xyz>"
```

---

### Task 4: Redesign clear command with name/domain/all targeting

Replace `gp clear` with `gp session clear` supporting smart target resolution.

**Files:**
- Modify: `src/graftpunk/cli/session_commands.py`
- Modify: `src/graftpunk/cli/main.py` (remove old `clear`)
- Modify: `tests/unit/test_cli.py` (rewrite `TestClearCommand`)

**Step 1: Write the new tests**

Replace `TestClearCommand` in `tests/unit/test_cli.py`:

```python
class TestClearCommand:
    """Tests for session clear command."""

    @patch("graftpunk.cli.session_commands.list_sessions_with_metadata")
    @patch("graftpunk.cli.session_commands.clear_session_cache")
    def test_clear_by_name(self, mock_clear, mock_list_meta):
        """Clear a specific session by name (no dots = name)."""
        mock_list_meta.return_value = [
            {"name": "hackernews", "domain": "news.ycombinator.com", "cookie_count": 3,
             "modified_at": "2026-01-01T00:00:00"},
        ]
        mock_clear.return_value = ["hackernews"]

        result = runner.invoke(app, ["session", "clear", "hackernews", "-f"])

        assert result.exit_code == 0
        assert "hackernews" in result.output
        mock_clear.assert_called_once_with("hackernews")

    @patch("graftpunk.cli.session_commands.list_sessions_with_metadata")
    @patch("graftpunk.cli.session_commands.clear_session_cache")
    def test_clear_by_name_not_found(self, mock_clear, mock_list_meta):
        """Clear by name when session doesn't exist."""
        mock_list_meta.return_value = []
        mock_clear.return_value = []

        result = runner.invoke(app, ["session", "clear", "nonexistent", "-f"])

        assert result.exit_code == 1
        assert "not found" in result.output.lower()

    @patch("graftpunk.cli.session_commands.list_sessions_with_metadata")
    @patch("graftpunk.cli.session_commands.clear_session_cache")
    def test_clear_by_domain(self, mock_clear, mock_list_meta):
        """Clear sessions matching a domain (dots = domain)."""
        mock_list_meta.return_value = [
            {"name": "app1", "domain": "example.com", "cookie_count": 2,
             "modified_at": "2026-01-01T00:00:00"},
            {"name": "app2", "domain": "example.com", "cookie_count": 5,
             "modified_at": "2026-01-01T00:00:00"},
            {"name": "other", "domain": "other.com", "cookie_count": 1,
             "modified_at": "2026-01-01T00:00:00"},
        ]
        mock_clear.side_effect = lambda n: [n]

        result = runner.invoke(app, ["session", "clear", "example.com", "-f"])

        assert result.exit_code == 0
        assert "app1" in result.output
        assert "app2" in result.output
        assert "other" not in result.output
        assert mock_clear.call_count == 2

    @patch("graftpunk.cli.session_commands.list_sessions_with_metadata")
    def test_clear_by_domain_no_matches(self, mock_list_meta):
        """Clear by domain when no sessions match."""
        mock_list_meta.return_value = [
            {"name": "app1", "domain": "other.com", "cookie_count": 1,
             "modified_at": "2026-01-01T00:00:00"},
        ]

        result = runner.invoke(app, ["session", "clear", "example.com", "-f"])

        assert result.exit_code == 1
        assert "no sessions" in result.output.lower() or "not found" in result.output.lower()

    @patch("graftpunk.cli.session_commands.list_sessions_with_metadata")
    @patch("graftpunk.cli.session_commands.clear_session_cache")
    def test_clear_all_with_force(self, mock_clear, mock_list_meta):
        """Clear all sessions with --all --force."""
        mock_list_meta.return_value = [
            {"name": "s1", "domain": "a.com", "cookie_count": 1,
             "modified_at": "2026-01-01T00:00:00"},
            {"name": "s2", "domain": "b.com", "cookie_count": 2,
             "modified_at": "2026-01-01T00:00:00"},
        ]
        mock_clear.side_effect = lambda n: [n]

        result = runner.invoke(app, ["session", "clear", "--all", "--force"])

        assert result.exit_code == 0
        assert "s1" in result.output
        assert "s2" in result.output
        assert mock_clear.call_count == 2

    @patch("graftpunk.cli.session_commands.list_sessions_with_metadata")
    @patch("graftpunk.cli.session_commands.clear_session_cache")
    def test_clear_all_prompts_without_force(self, mock_clear, mock_list_meta):
        """Clear all prompts for confirmation without --force."""
        mock_list_meta.return_value = [
            {"name": "s1", "domain": "a.com", "cookie_count": 1,
             "modified_at": "2026-01-01T00:00:00"},
        ]

        result = runner.invoke(app, ["session", "clear", "--all"], input="n\n")

        assert result.exit_code == 0
        assert "Cancelled" in result.output
        mock_clear.assert_not_called()

    @patch("graftpunk.cli.session_commands.list_sessions_with_metadata")
    def test_clear_all_empty(self, mock_list_meta):
        """Clear all when no sessions exist."""
        mock_list_meta.return_value = []

        result = runner.invoke(app, ["session", "clear", "--all", "-f"])

        assert result.exit_code == 0
        assert "No sessions" in result.output

    def test_clear_no_target_no_all(self):
        """Clear with no target and no --all shows error."""
        result = runner.invoke(app, ["session", "clear"])

        assert result.exit_code != 0 or "specify" in result.output.lower() or "Usage" in result.output

    @patch("graftpunk.cli.session_commands.list_sessions_with_metadata")
    @patch("graftpunk.cli.session_commands.clear_session_cache")
    def test_clear_by_name_prompts_without_force(self, mock_clear, mock_list_meta):
        """Clear by name prompts for confirmation without --force."""
        mock_list_meta.return_value = [
            {"name": "hackernews", "domain": "news.ycombinator.com", "cookie_count": 3,
             "modified_at": "2026-01-01T00:00:00"},
        ]

        result = runner.invoke(app, ["session", "clear", "hackernews"], input="n\n")

        assert result.exit_code == 0
        assert "Cancelled" in result.output
        mock_clear.assert_not_called()

    @patch("graftpunk.cli.session_commands.list_sessions_with_metadata")
    @patch("graftpunk.cli.session_commands.clear_session_cache")
    def test_clear_always_shows_removed_list(self, mock_clear, mock_list_meta):
        """Clear always prints list of removed sessions."""
        mock_list_meta.return_value = [
            {"name": "hackernews", "domain": "news.ycombinator.com", "cookie_count": 3,
             "modified_at": "2026-01-01T00:00:00"},
        ]
        mock_clear.return_value = ["hackernews"]

        result = runner.invoke(app, ["session", "clear", "hackernews", "-f"])

        assert result.exit_code == 0
        # Must show the session name and domain in the removal output
        output = strip_ansi(result.output)
        assert "hackernews" in output
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_cli.py::TestClearCommand -v`
Expected: FAIL

**Step 3: Implement the new clear command**

Add to `src/graftpunk/cli/session_commands.py`:

```python
from graftpunk import clear_session_cache

@session_app.command("clear")
def session_clear(
    target: Annotated[
        str | None,
        typer.Argument(
            help="Session name or domain to clear",
            metavar="TARGET",
        ),
    ] = None,
    all_sessions: Annotated[
        bool,
        typer.Option("--all", "-a", help="Clear all sessions"),
    ] = False,
    force: Annotated[
        bool,
        typer.Option("--force", "-f", help="Skip confirmation prompt"),
    ] = False,
) -> None:
    """Remove cached session(s).

    Clear by session name, domain, or all:

        gp session clear hackernews       Clear session named "hackernews"
        gp session clear example.com      Clear all sessions for domain
        gp session clear --all            Clear everything (prompts first)
        gp session clear -af              Clear everything without prompt
    """
    if not target and not all_sessions:
        console.print("[red]Specify a session name, domain, or use --all[/red]")
        raise typer.Exit(1)

    all_metadata = list_sessions_with_metadata()

    if all_sessions:
        # Clear everything
        if not all_metadata:
            console.print("[dim]No sessions to clear[/dim]")
            return

        if not force:
            console.print(f"[yellow]This will remove {len(all_metadata)} session(s):[/yellow]")
            for s in all_metadata:
                console.print(f"  - {s['name']} ({s.get('domain') or 'no domain'})")
            if not typer.confirm("Remove all sessions?"):
                console.print("[dim]Cancelled[/dim]")
                return

        removed = []
        for s in all_metadata:
            result = clear_session_cache(s["name"])
            if result:
                removed.append(s)

        _print_removed(removed)
        return

    # target is set — determine if name or domain
    assert target is not None
    is_domain = "." in target

    if is_domain:
        # Match sessions by domain
        matches = [s for s in all_metadata if s.get("domain") == target]
        if not matches:
            console.print(f"[red]No sessions found for domain '{target}'[/red]")
            raise typer.Exit(1)

        if not force:
            console.print(f"[yellow]Found {len(matches)} session(s) for domain '{target}':[/yellow]")
            for s in matches:
                console.print(f"  - {s['name']} ({s.get('cookie_count', 0)} cookies)")
            if not typer.confirm(f"Remove {len(matches)} session(s)?"):
                console.print("[dim]Cancelled[/dim]")
                return

        removed = []
        for s in matches:
            result = clear_session_cache(s["name"])
            if result:
                removed.append(s)

        _print_removed(removed)
    else:
        # Match session by name
        target = resolve_session_name(target)
        match = next((s for s in all_metadata if s["name"] == target), None)

        if not match:
            console.print(f"[red]Session '{target}' not found[/red]")
            raise typer.Exit(1)

        if not force:
            console.print(f"[yellow]Session to remove:[/yellow]")
            console.print(f"  - {match['name']} ({match.get('domain') or 'no domain'})")
            if not typer.confirm("Remove this session?"):
                console.print("[dim]Cancelled[/dim]")
                return

        clear_session_cache(target)
        _print_removed([match])


def _print_removed(removed: list[dict]) -> None:
    """Print the list of removed sessions."""
    if not removed:
        console.print("[dim]No sessions removed[/dim]")
        return

    console.print(f"\n[green]Removed {len(removed)} session(s):[/green]")
    for s in removed:
        console.print(f"  - {s.get('name', '?')} ({s.get('domain') or 'no domain'})")
```

Remove from `main.py`:
- The `clear` function and its decorator (lines 277-325)
- Remove unused imports: `clear_session_cache`, `list_sessions` (if no longer used)

**Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_cli.py::TestClearCommand -v`
Expected: PASS

Also run: `uv run pytest tests/ -v --tb=short`

**Step 5: Commit**

```bash
git add src/graftpunk/cli/session_commands.py src/graftpunk/cli/main.py tests/unit/test_cli.py
git commit -m "feat: redesign clear as 'gp session clear' with name/domain/all targeting

Target resolution: no dots = session name, dots = domain match,
--all = clear everything. Always shows list of removed sessions.
Prompts for confirmation unless --force/-f is given.

Co-Authored-By: stavxyz <hi@stav.xyz>"
```

---

### Task 5: Update main.py help text and clean up imports

Update the app help text to reflect new command structure, clean up any remaining dead imports.

**Files:**
- Modify: `src/graftpunk/cli/main.py`

**Step 1: Run full test suite first to confirm baseline**

Run: `uv run pytest tests/ -v --tb=short`
Expected: All pass

**Step 2: Update help text and clean up**

In `main.py`, update the app help string (lines 55-69):

```python
app = GraftpunkApp(
    name="graftpunk",
    help="""
    graftpunk - turn any website into an API

    Graft scriptable access onto authenticated web services.
    Log in once, script forever.

    \b
    Quick start:
      gp session list        Show all cached sessions
      gp session show <name> View session details
      gp session clear <name> Remove a session
      gp config              Show current configuration

    \b
    Documentation: https://github.com/stavxyz/graftpunk
    """,
    no_args_is_help=True,
    rich_markup_mode="rich",
    context_settings={"help_option_names": ["-h", "--help"]},
)
```

Remove any now-unused imports from main.py (e.g. `list_sessions_with_metadata`, `clear_session_cache`, `list_sessions`, `get_session_metadata`, `load_session`, `cast`, `Path`, `GraftpunkError`, `SessionExpiredError`, `SessionNotFoundError` — only if they're not used elsewhere in main.py).

**Step 3: Run quality checks**

Run: `uvx ruff check . && uvx ruff format --check . && uvx ty check src/`
Expected: All clean

Run: `uv run pytest tests/ -v --tb=short`
Expected: All pass

**Step 4: Commit**

```bash
git add src/graftpunk/cli/main.py
git commit -m "chore: update help text for session command group, clean up imports

Co-Authored-By: stavxyz <hi@stav.xyz>"
```

---

### Task 6: Run full verification

**Step 1: Full test suite**

Run: `uv run pytest tests/ -v --tb=short`
Expected: All pass

**Step 2: Quality checks**

Run: `uvx ruff check . && uvx ruff format . && uvx ty check src/`
Expected: All clean

**Step 3: Manual smoke test**

```bash
gp session list
gp session show hackernews  # (if session exists)
gp session clear --help
gp --help  # verify session subcommand appears
```

**Step 4: Verify old commands are gone**

```bash
gp list    # Should fail with "No such command"
gp clear   # Should fail with "No such command"
gp show    # Should fail with "No such command"
gp export  # Should fail with "No such command"
```
