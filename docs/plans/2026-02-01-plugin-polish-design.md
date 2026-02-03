# Plugin Polish: Declarative Login & Rich Terminal Output

## Summary

Two changes to the plugin system:

1. **Declarative login** — Plugins describe login form selectors; the framework handles all browser lifecycle, cookie transfer, and session caching.
2. **Rich terminal output** — All user-facing output uses Rich. Stderr for status/progress, stdout for data.

## Declarative Login Spec

### Python Plugins

New optional class attributes on `SitePlugin`:

- `backend` — `"selenium"` (default) or `"nodriver"`
- `login_url` — path relative to `base_url` (e.g. `"/login"`)
- `login_fields` — dict mapping `{"username": "<css-selector>", "password": "<css-selector>"}`
- `login_submit` — CSS selector for the submit button
- `login_failure` — text on page when login fails (checked via page content)
- `login_success` — CSS selector present when login succeeds (alternative to `login_failure`)

Only one of `login_failure` or `login_success` is needed. If neither is set, the framework assumes success if no exception is thrown.

If all login attributes are set, the framework auto-generates the `login()` method. If the plugin defines its own `login()`, it takes precedence.

### YAML Plugins

New `login:` block:

```yaml
login:
  url: /login
  backend: nodriver
  fields:
    username: "input[name='acct']"
    password: "input[name='pw']"
  submit: "input[value='login']"
  failure: "Bad login."
```

### Login Engine

The framework handles:

1. Create `BrowserSession(backend=..., headless=False)`
2. Call `start_async()` for nodriver
3. Navigate to `{base_url}{login_url}`
4. Click each field, then send keys (click-before-type prevents keystroke loss)
5. Click submit
6. Wait for page load
7. Check success/failure
8. Transfer cookies (nodriver: `transfer_nodriver_cookies_to_session()`)
9. Call `cache_session()`
10. Quit browser

## Context Manager Escape Hatch

For complex flows (CAPTCHAs, multi-step, OAuth), plugins override `login()` but use a managed context:

```python
async def login(self, username: str, password: str) -> bool:
    async with self.browser_session() as (session, tab):
        tab = await session.driver.get(f"{self.base_url}/login")
        # ... custom logic ...
        return "Welcome" in await tab.get_content()
```

`self.browser_session()` handles:

- Browser creation and startup
- Yields `(session, tab)`
- On exit: cookie transfer, `cache_session()`, browser quit
- On exception: browser quit without caching

Sync equivalent: `self.browser_session_sync()` for selenium plugins.

## Rich Terminal Output

Key principle: **stderr for status/progress, stdout for data**. `gp hn front | jq .` always works.

### Login Flow (stderr)

```
$ gp hn login
  Username: stavxyz
  Password: ••••••••••••
  ⠋ Opening browser...
  ⠋ Logging in to hn...
  ✓ Logged in to hn (session cached)
```

Failure:

```
  ✗ Login failed for hn: Bad login.
```

### Command Output (stdout)

- JSON: pretty-printed with syntax highlighting when terminal, raw when piped
- `--format table`: list-of-dicts renders as Rich table
- Errors: styled panels on stderr, no raw tracebacks

### Log Verbosity

- Default: warnings and errors only (no plugin discovery spam)
- `-v`: info (which plugins loaded)
- `-vv`: debug (every command registered)

### Implementation

A thin `console` module in graftpunk core wraps Rich. Plugins never import Rich — the framework owns all terminal output. Plugin commands return data; the framework displays it.

## Before & After

### hackernews.py — Before (95 lines)

```python
import asyncio
from graftpunk import BrowserSession, cache_session
from graftpunk.plugins import SitePlugin, command

class HackerNewsPlugin(SitePlugin):
    site_name = "hn"
    session_name = "hackernews"
    help_text = "Hacker News commands"
    base_url = "https://news.ycombinator.com"

    async def login(self, username: str, password: str) -> bool:
        session = BrowserSession(backend="nodriver", headless=False)
        await session.start_async()
        tab = await session.driver.get(f"{self.base_url}/login")
        acct_field = await tab.select("input[name='acct']")
        await acct_field.click()
        await acct_field.send_keys(username)
        pw_field = await tab.select("input[name='pw']")
        await pw_field.click()
        await pw_field.send_keys(password)
        submit = await tab.select("input[value='login']")
        await submit.click()
        await asyncio.sleep(3)
        page_text = await tab.get_content()
        if "Bad login." in page_text:
            session.driver.stop()
            return False
        await session.transfer_nodriver_cookies_to_session()
        cache_session(session, self.session_name)
        session.driver.stop()
        return True

    @command(help="Get front page stories")
    def front(self, session, page: int = 1):
        # ...
```

### hackernews.py — After (25 lines)

```python
from graftpunk.plugins import SitePlugin, command

class HackerNewsPlugin(SitePlugin):
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
        # ...
```

### YAML — Fully Authenticated Plugin (zero Python)

```yaml
site_name: hn
session_name: hackernews
help: "Hacker News commands"
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
    help: "Get front page stories"
    url: "/news"
    params:
      - name: page
        type: int
        default: 1
```
