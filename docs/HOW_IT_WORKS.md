# How graftpunk Works

This document explains the internals of graftpunk: browser automation, session management, the plugin system, observability, and the CLI.

---

## Architecture Overview

graftpunk captures authenticated browser sessions and exposes them as CLI commands. The flow is:

1. **Login** — A browser opens, you log in (manually or via automation), cookies are captured.
2. **Cache** — The session (cookies + headers) is encrypted and stored locally.
3. **Use** — CLI commands load the cached session into a plain `requests.Session` and make API calls without a browser.

Plugins define both the login flow and the commands available for a site.

---

## Browser Backends

graftpunk supports two browser automation backends, selectable per plugin via the `backend` attribute (typed as `Literal["selenium", "nodriver"]`).

### Selenium (`backend = "selenium"`)

Uses [undetected-chromedriver](https://github.com/ultrafunkamsterdam/undetected-chromedriver) with [selenium-stealth](https://github.com/diprajpatra/selenium-stealth) for anti-detection. This is the default backend.

- Sync API (`driver.get()`, `driver.find_element()`)
- Screenshot support via `driver.get_screenshot_as_png()`
- Console and performance log collection via `driver.get_log()`
- Stealth mode enabled by default (`use_stealth=True`)
- Exceptions use `selenium.common.exceptions.WebDriverException` as the base class

### NoDriver (`backend = "nodriver"`)

Uses [nodriver](https://github.com/ultrafunkamsterdam/nodriver), a direct Chrome DevTools Protocol client. Better anti-detection than Selenium for sites with aggressive bot protection.

- Async API (`await tab.get()`, `await tab.select()`)
- Requires `await session.start_async()` before use
- No synchronous screenshot support (returns `None` from sync capture)
- Cookie transfer via `await session.transfer_nodriver_cookies_to_session()`
- `tab.select(selector)` returns `None` on timeout — it does **not** raise an exception. Code that checks element presence should test the return value, not catch exceptions.

### BrowserSession

`BrowserSession` (in `src/graftpunk/session.py`) wraps both backends with a unified interface. It extends `requestium.Session`, which itself extends `requests.Session`.

```python
session = BrowserSession(
    backend="selenium",    # or "nodriver"
    headless=False,        # show browser window
    use_stealth=True,      # anti-detection (selenium only)
    observe_mode="off",    # "off" or "full"
)
```

**Driver access:** The `session.driver` property raises `BrowserError` if no browser has been started. Callers that need the driver optionally should catch `BrowserError` rather than using `getattr()`.

**Context manager protocol:**

```python
# Selenium (sync)
with BrowserSession(backend="selenium") as session:
    session.driver.get("https://example.com")
    # On exit: captures observability data, quits browser

# NoDriver (async)
async with BrowserSession(backend="nodriver") as session:
    tab = await session.driver.get("https://example.com")
    # On exit: captures observability data, stops browser
```

The context manager handles:
- Starting observability capture on enter (skipped gracefully with a warning if no driver is available)
- Flushing observability data (screenshots, HAR, console logs) on exit — each flush step is isolated so one failure doesn't prevent others
- Browser cleanup (quit/stop) on exit

**Serialization:** `BrowserSession` implements `__getstate__` to strip browser-related state (driver handles, async loops) before pickling. The nodriver backend serializes only HTTP client state (cookies, headers, session name) — browser driver handles are excluded; the selenium backend delegates to `requests.Session.__getstate__`. Both backends preserve `_gp_cached_tokens` (token cache) and `_gp_header_profiles` (browser header profiles) through the pickle roundtrip.

---

## Session Management

### Caching

When a login completes, the session is cached:

```python
from graftpunk import cache_session
cache_session(session, "mysite")
```

This:
1. Serializes the session with `dill` (preserves cookies, headers, browser state)
2. Computes a SHA256 checksum
3. Encrypts with Fernet (AES-128-CBC + HMAC-SHA256)
4. Stores encrypted data + metadata via the storage backend

Sessions have a configurable TTL (`GRAFTPUNK_SESSION_TTL_HOURS`). Metadata tracks domain, cookie count, cookie domains, and status.

An HTTPie-compatible session file can also be exported alongside the cache, falling back gracefully (with a warning log) if cookie extraction fails due to browser state issues.

### Loading

For CLI commands, sessions are loaded without a browser:

```python
from graftpunk.cache import load_session_for_api
session = load_session_for_api("mysite")  # returns GraftpunkSession
```

This extracts cookies and headers from the cached `BrowserSession` into a `GraftpunkSession` (a `requests.Session` subclass) suitable for API calls.

### GraftpunkSession and Browser Header Replay

`GraftpunkSession` extends `requests.Session` with browser header profiles captured during login. When a session is cached after login, graftpunk captures the actual HTTP headers the browser sends (via CDP network events) and classifies them into profiles:

- **navigation** — Headers from top-level page loads (Accept, Accept-Language, Accept-Encoding, User-Agent, etc.)
- **xhr** — Headers from XMLHttpRequest/fetch calls (typically includes additional headers like X-Requested-With)
- **form** — Headers from form submissions

When `load_session_for_api()` loads a session, it returns a `GraftpunkSession` that automatically detects and applies the appropriate header profile (navigation, xhr, or form) for each request based on its characteristics. This means API calls look like they came from the same Chrome browser that logged in, not from Python's default `requests` User-Agent.

**Browser identity separation:** `GraftpunkSession` separates headers into two axes: *browser identity* (User-Agent, sec-ch-ua, sec-ch-ua-mobile, sec-ch-ua-platform) and *request type* (Accept, Sec-Fetch-*, X-Requested-With). Browser identity headers are extracted at init and set as session defaults — they can never leak, even when the detected request-type profile wasn't captured during login. When a detected profile is missing (e.g., login was an SPA that only produced xhr/form requests, but the plugin later makes a navigation-style request), canonical Chrome request-type headers are used as fallback.

```python
api = load_session_for_api("mysite")
# api.headers already contains real Chrome headers:
# User-Agent, Accept, Accept-Language, Accept-Encoding (including br), etc.
response = api.get("https://example.com/api/data")
```

Brotli (`br`) is included as a core dependency so `Accept-Encoding: gzip, deflate, br` is properly supported.

#### Request-Type Methods

For explicit control over which header profile is used, `GraftpunkSession` provides three methods:

```python
# XHR-style request (Accept: application/json, X-Requested-With, etc.)
resp = session.xhr("GET", "https://example.com/api/data", referer="/dashboard")

# Navigation-style request (Accept: text/html, Sec-Fetch-Mode: navigate, etc.)
resp = session.navigate("GET", "https://example.com/page")

# Form submission (Content-Type: application/x-www-form-urlencoded, etc.)
resp = session.form_submit("POST", "https://example.com/login", referer="/login", data={"user": "me"})
```

Each method:
1. Starts from the captured profile headers for that request type (if available)
2. Falls back to canonical Chrome request-type headers (if the profile wasn't captured during login)
3. Browser identity headers are already on `self.headers` from session init
4. The `referer` kwarg resolves paths against `gp_base_url` (paths like `"/invoice/list"` become full URLs)
5. Caller-supplied `headers=` override profile headers
6. All other `**kwargs` pass through to `requests.Session.request()`

This eliminates the need for plugins to maintain their own header-building infrastructure. A plugin that previously needed 150 lines of request helpers can reduce to one-line calls.

### Session Persistence After Commands

Commands can opt into saving session changes back to the cache. This is useful when API responses set new cookies (e.g., refreshed auth tokens):

```python
@command(help="Refresh token", saves_session=True)
def refresh(self, ctx: CommandContext):
    resp = ctx.session.post(f"{self.base_url}/api/refresh")
    # CommandContext.save_session() is called automatically after the handler
    return resp.json()
```

The `update_session_cookies()` function merges new cookies from the API session back into the cached `BrowserSession`.

### Bot-Detection Cookie Filtering

When injecting cookies into a nodriver browser (for observe mode, token extraction, etc.), graftpunk automatically skips known WAF bot-detection cookies. These cookies — set by services like Akamai Bot Manager — carry bot-classification state from previous sessions. Injecting stale copies causes WAFs to immediately flag the new browser, resulting in connection-level rejection (`ERR_HTTP2_PROTOCOL_ERROR`).

Filtered by default:
- `bm_*`, `ak_bmsc`, `_abck` (Akamai Bot Manager)

The filter lives in `inject_cookies_to_nodriver()`, the single chokepoint through which all cookies flow into nodriver browsers (observe mode, token extraction, interactive observe). The function returns a `tuple[int, int]` of (injected, skipped) so callers have visibility into what was filtered. If all cookies are filtered, a warning is logged indicating the session may not work.

Cookies with `None` name or value are also skipped with a debug log (malformed cookie guard).

The filter can be disabled per-call with `skip_bot_cookies=False`. The `BOT_DETECTION_COOKIE_PREFIXES` tuple can be extended with patterns for Cloudflare, Imperva, PerimeterX, DataDome, and other WAFs as they are encountered.

### Storage Backends

Controlled by `GRAFTPUNK_STORAGE_BACKEND`:
- `"local"` (default) — Filesystem storage
- `"supabase"` — Cloud storage via Supabase

### CLI Commands

```bash
gp session list              # List cached sessions
gp session show <session>    # Session metadata (domain, cookies, expiry)
gp session clear [session]   # Remove one or all sessions (--all for all)
gp session export <session>  # Export cookies to HTTPie session format
gp session use <session>     # Set active session for subsequent commands
gp session unset             # Clear active session
```

---

## Plugin System

Plugins define CLI command groups for specific sites. Each plugin has a `site_name` (the CLI subcommand) and a `session_name` (the cached session key).

### Plugin Types

#### Python Plugins

Subclass `SitePlugin` and use the `@command` decorator:

```python
from graftpunk.plugins import CommandContext, SitePlugin, command

class MyPlugin(SitePlugin):
    site_name = "mysite"
    session_name = "mysite"
    base_url = "https://example.com"
    api_version = 1

    @command(help="List items")
    def items(self, ctx: CommandContext, page: int = 1):
        return ctx.session.get(f"{self.base_url}/api/items?page={page}").json()
```

Commands receive a `CommandContext` (dataclass) with:
- `session` — A `requests.Session` loaded from the cache (cookies pre-loaded)
- `plugin_name` — The plugin's `site_name`
- `command_name` — The command being executed
- `api_version` — The plugin's API version
- `base_url` — The plugin's `base_url` (empty string if not set)
- `config` — The full `PluginConfig` object (or `None`)
- `observe` — An `ObservabilityContext` for screenshots, logging, and timing

Parameters are auto-introspected from the method signature. Type hints (`int`, `float`, `bool`, `str`) are used for CLI argument parsing. Parameters with defaults become `--option` flags; required parameters become positional arguments.

**Plugin protocol:** All plugin types implement `CLIPluginProtocol` (a structural typing `Protocol`). This defines the interface: `site_name`, `session_name`, `backend` (typed as `Literal["selenium", "nodriver"]`), `requires_session`, and `get_commands()`. Login capability is detected via duck typing (presence of `login()` method or `LoginConfig`).

#### YAML Plugins

Define commands declaratively in YAML:

```yaml
site_name: mysite
base_url: "https://example.com"
requires_session: true

commands:
  items:
    help: "List items"
    method: GET
    url: "/api/items"
    params:
      - name: page
        type: int
        default: 1
```

YAML plugins support:
- **HTTP methods:** GET, POST, PUT, DELETE, PATCH, HEAD, OPTIONS
- **URL parameters:** `{param}` in URL path become positional arguments
- **Query parameters:** Remaining params become query string / `--option` flags
- **Headers:** Per-command or plugin-wide, with `${ENV_VAR}` expansion
- **JMESPath filtering:** Extract nested data from JSON responses
- **Resource limits:** `timeout`, `max_retries`, `rate_limit` per command

### Plugin Discovery

graftpunk discovers plugins from three sources, in order:

1. **Entry points** — Python packages with `[project.entry-points."graftpunk.plugins"]` in `pyproject.toml`
2. **YAML files** — `~/.config/graftpunk/plugins/*.yaml` and `*.yml`
3. **Python files** — `~/.config/graftpunk/plugins/*.py` (files starting with `_` are skipped)

**Collision detection:** If two plugins share the same `site_name`, registration fails with a `PluginError` showing both sources (e.g., `yaml:/path/a.yaml` vs `entry_point:module.name`). The `GraftpunkApp.add_plugin_group()` method also rejects duplicate group names. This prevents silent shadowing.

Discovery errors are accumulated as `PluginDiscoveryError` / `YAMLDiscoveryError` / `PythonDiscoveryError` (all frozen dataclasses) — valid plugins from the same source still load even if others fail.

### Plugin Configuration

`PluginConfig` is a frozen dataclass — the canonical configuration object. It is constructed via the `build_plugin_config()` factory which handles defaulting and validation. Fields can be set explicitly or inferred:

- `site_name` — Required. Can be auto-inferred from `base_url` (e.g., `https://news.ycombinator.com` → `"ycombinator"`) or from the YAML plugin filename (e.g., `httpbin.yaml` → `"httpbin"`)
- `session_name` — Defaults to `site_name`
- `help_text` — Defaults to `"Commands for {site_name}"`
- `backend` — `"selenium"` (default) or `"nodriver"`
- `requires_session` — Whether commands need a cached session (default `True`)
- `api_version` — Plugin interface version (currently `1`)

`PluginParamSpec` is also a frozen dataclass defining CLI parameter specifications (name, type, required, default, help text, is_option flag).

---

## Login System

graftpunk supports three login approaches, from simplest to most flexible.

### 1. Declarative Login (YAML or Python)

Define login configuration with CSS selectors. The login engine generates the browser automation:

```python
from graftpunk.plugins import LoginConfig

class MyPlugin(SitePlugin):
    base_url = "https://example.com"
    backend = "selenium"

    login_config = LoginConfig(
        url="/login",
        fields={"username": "#email", "password": "#password"},
        submit="button[type=submit]",
        failure="Invalid credentials",  # Text that appears on failure
        success=".dashboard",           # CSS selector present on success
    )
```

`LoginConfig` is a frozen dataclass that enforces the all-or-nothing invariant: `url`, `fields`, and `submit` are all required. `failure` and `success` are optional validation hints.

For ergonomics, flat class attributes (`login_url`, `login_fields`, `login_submit`) are also supported — `SitePlugin.__init_subclass__` auto-constructs a `LoginConfig` and assigns it to `login_config`:

Or in YAML:

```yaml
login:
  url: /login
  fields:
    username: "input#email"
    password: "input#password"
  submit: "button[type=submit]"
  failure: "Invalid credentials"
  success: ".dashboard"
```

The declarative engine:
1. Opens the browser to `{base_url}{login.url}`
2. Clicks each field element, then types the credential value
3. Clicks the submit button
4. Waits for the page to settle
5. Checks for failure text in page content
6. Checks for success element via CSS selector
7. Transfers cookies and caches the session

Both `failure` and `success` checks run independently — you can use either or both. If neither is configured, a warning is logged advising you to add validation.

**Backend differences in success detection:**
- **Selenium:** Uses `driver.find_element()` with a try/except for `NoSuchElementException`
- **NoDriver:** Uses `await tab.select(selector)` and checks for `None` return (nodriver does not raise on timeout)

### 2. Custom Login Method (Python)

Define a `login()` method on your plugin. It receives a `credentials` dict:

```python
class MyPlugin(SitePlugin):
    def login(self, credentials: dict[str, str]) -> bool:
        with self.browser_session_sync() as (session, driver):
            driver.get(f"{self.base_url}/login")
            driver.find_element("id", "email").send_keys(credentials["username"])
            driver.find_element("id", "pass").send_keys(credentials["password"])
            driver.find_element("id", "submit").click()
            # browser_session_sync handles cookie transfer and caching
        return True
```

For nodriver (async):

```python
async def login(self, credentials: dict[str, str]) -> bool:
    async with self.browser_session() as (session, tab):
        await tab.get(f"{self.base_url}/login")
        # ... async login logic
    return True
```

The `browser_session()` and `browser_session_sync()` context managers handle browser lifecycle, cookie transfer, and session caching automatically.

**Return value contract:** `login()` should return `True` on success and `False` on failure. The CLI checks for `is False` specifically — returning `None` or other values triggers a warning log but is treated as success for backwards compatibility.

### 3. Auto-Generated Login Command

If a plugin has either a `login()` method or declarative login configuration, graftpunk auto-generates a `login` CLI command:

```bash
gp mysite login
```

Credentials are resolved in order:
1. **Environment variables** — `{SITE_PREFIX}_{FIELD_NAME}` (e.g., `MYSITE_USERNAME`, `MYSITE_PASSWORD`). Plugins can override envvar names via `username_envvar` / `password_envvar`. Empty environment variable values are treated as unset (with a debug log).
2. **Interactive prompts** — Fields containing "password", "secret", "token", or "key" use masked input.

```bash
MYSITE_USERNAME=x MYSITE_PASSWORD=y gp mysite login  # No prompts
```

---

## Command Execution

### Resource Limits

Commands can specify resource limits on `CommandSpec`:

```python
@command(help="Fetch data")
def fetch(self, ctx: CommandContext):
    ...

# Or set on the spec directly:
CommandSpec(name="fetch", handler=fn, timeout=30, max_retries=2, rate_limit=1.0)
```

- **`timeout`** — Request timeout in seconds (passed to the handler context)
- **`max_retries`** — Number of retry attempts on transient failures. Uses exponential backoff (2^attempt seconds). Only retries `requests.RequestException`, `ConnectionError`, `TimeoutError`, and `OSError`. Programming errors propagate immediately.
- **`rate_limit`** — Minimum seconds between consecutive calls to the same command.

YAML plugins set these per command:

```yaml
commands:
  fetch:
    timeout: 30
    max_retries: 2
    rate_limit: 1.0
```

### Output Formatting

All plugin commands support `--format` / `-f`:

- **`json`** (default) — Pretty-printed JSON
- **`table`** — Tabular format for list data
- **`raw`** — Unformatted output

If a command returns a `CommandResult`, the `data` field is extracted for formatting.

---

## Observability

The observability system captures browser activity during login and command execution for debugging and auditing.

### Modes

Set via `--observe` CLI flag or `observe_mode` on `BrowserSession`:

- **`off`** (default) — No capture. All observability calls are no-ops (`NoOpObservabilityContext`).
- **`full`** — Captures everything (screenshots, HAR network data, console logs, events).

### ObservabilityContext

Plugins interact with observability through the `ctx.observe` handle:

```python
@command(help="Transfer funds")
def transfer(self, ctx: CommandContext, amount: float):
    ctx.observe.mark("transfer_start")
    ctx.observe.screenshot("before-transfer")

    result = ctx.session.post(f"{self.base_url}/api/transfer", json={"amount": amount})

    ctx.observe.log("transfer_complete", {"amount": amount, "status": result.status_code})
    ctx.observe.screenshot("after-transfer")
    ctx.observe.mark("transfer_end")
    return result.json()
```

All methods are safe to call regardless of mode — they're no-ops when observability is off. This means plugins don't need conditional checks.

The `build_observe_context()` factory function creates the appropriate context based on mode and driver availability. If observability is enabled but no browser driver is available, it creates a context without capture capability and logs a warning.

### Capture Backends

Capture backends implement the `CaptureBackend` protocol and collect browser data:

- **SeleniumCaptureBackend** — Screenshots via `get_screenshot_as_png()`, console logs via `get_log("browser")`, HAR data via `get_log("performance")`. All Selenium calls catch `WebDriverException` specifically (not bare `Exception`), logging errors without crashing the session.
- **NodriverCaptureBackend** — Full async capture via CDP events. Listens for `Network.LoadingFinished` to eagerly fetch response bodies before Chrome's CDP buffer evicts them. Bodies are streamed to disk (configurable `max_body_size`). Supports `start_capture_async()` / `stop_capture_async()` for interactive mode, and sync `start_capture()` / `stop_capture()` for standard use.

The `create_capture_backend()` factory validates the backend type and creates the appropriate backend instance.

### Storage

Observability data is stored in a structured directory:

```
~/.local/share/graftpunk/observe/{session_name}/{run_id}/
    screenshots/
        001-before-login.png
        002-after-login.png
    events.jsonl
    network.har
    console.jsonl
    metadata.json
```

**Path safety:** `ObserveStorage` validates session names and run IDs on construction — rejecting empty strings, path separators (`/`, `\`), directory traversal (`..`), leading dots, spaces, and special characters. Only alphanumeric characters, hyphens, dots (non-leading), and underscores are allowed.

**Screenshot labels** are sanitized: non-alphanumeric characters (except `.`, `-`, `_`) are replaced with `-` to prevent filesystem issues.

**Events** are stored as JSONL (one JSON object per line). Corrupt lines are skipped on read, allowing partial recovery.

### Observability Teardown

When `BrowserSession` exits, observability data is flushed in isolated steps:

1. Final screenshot (if mode allows)
2. Stop capture backend
3. Write HAR entries
4. Write console logs

Each step has its own error handling — a failure in one (e.g., HAR collection fails) does not prevent the others from completing. Errors are logged at the error level.

### CLI Commands

```bash
gp observe list                          # List all observability runs
gp observe show <session>                # Show run details (file list, sizes)
gp observe clean [session]               # Remove observability data
gp observe -s <session> go <url>         # Automated capture (waits --wait seconds)
gp observe -s <session> interactive <url> # Interactive capture (Ctrl+C to stop)
```

---

## Token and CSRF Support

graftpunk supports declarative token extraction for sites that use CSRF tokens, dynamic session tokens, or other non-cookie authentication mechanisms.

### Token Configuration

Tokens are defined via `TokenConfig` on the plugin (Python) or `tokens:` block (YAML):

```yaml
tokens:
  - name: csrf_token
    source: cookie        # Extract from a cookie
    cookie_name: _csrf
  - name: api_key
    source: response_header  # Extract from a response header
    response_header: X-API-Key
  - name: session_token
    source: page           # Extract from page content via regex
    page_url: /dashboard
    pattern: 'data-token="([^"]+)"'
    cache_duration: 300    # Cache for 5 minutes
```

Token sources:
- **`cookie`** — Extract from a named cookie in the session
- **`response_header`** — Extract from a response header
- **`page`** — Fetch a page and extract via regex pattern

### EAFP Injection and 403 Retry

Token injection follows an EAFP (Easier to Ask Forgiveness than Permission) strategy:

1. Cached tokens are injected into request headers **even if expired** — the server is the arbiter of validity
2. If the server responds with 403, the token cache is cleared, tokens are re-extracted, and the request is retried once
3. This avoids unnecessary token refreshes when the server still accepts a technically-expired token

### Token Cache Persistence

Tokens extracted during login are persisted through the session serialization lifecycle:

- `BrowserSession.__getstate__` includes the `_gp_cached_tokens` attribute when serializing
- `BrowserSession.__setstate__` restores it on deserialization, defaulting to an empty dict if absent
- `update_session_cookies()` preserves the token cache when saving session changes back to the cache
- `load_session_for_api()` copies cached tokens from the stored `BrowserSession` to the `GraftpunkSession`

### Login-Time Token Extraction

When a plugin has both `login_config` and `token_config`, tokens are extracted during the login flow using the already-open browser session. This avoids launching a separate browser instance for token extraction:

- **Nodriver**: `_extract_and_cache_tokens_nodriver()` navigates the existing tab to token pages and extracts via regex polling
- **Selenium**: `_extract_and_cache_tokens_selenium()` uses the existing driver to navigate and extract

Both backends use `_build_token_cache()` to construct `CachedToken` instances from extracted values. Token extraction during login is best-effort — failures are logged but don't prevent session caching.

### Auto-Injection (Standard Flow)

When a plugin has `token_config`, the command executor:
1. Checks the token cache for each token (injecting even if expired — EAFP)
2. Extracts missing tokens via HTTP or browser (two-phase: HTTP first, then batch browser extraction for failures)
3. Injects all tokens into request headers
4. On 403 responses, clears the cache, re-extracts, and retries once

---

## Ad-hoc HTTP Requests (`gp http`)

The `gp http` command makes authenticated HTTP requests using cached session cookies and browser headers, without writing a plugin:

```bash
gp http get -s mybank https://secure.mybank.com/api/accounts
gp http post -s mybank https://secure.mybank.com/api/transfer --data '{"amount": 100}'
```

All HTTP methods are supported: `get`, `post`, `put`, `patch`, `delete`, `head`, `options`.

The session is loaded as a `GraftpunkSession` with full browser header replay, so requests have the same fingerprint as the browser that logged in.

---

## Authenticated Browser Capture (`gp observe go`)

`gp observe go` opens a URL in an authenticated browser session and captures network traffic, screenshots, and console logs:

```bash
gp observe -s mybank go https://secure.mybank.com/dashboard
```

This:
1. Loads the cached session cookies for the named session
2. Injects them into a fresh browser instance
3. Navigates to the URL
4. Captures all network requests, responses (with bodies), and console output
5. Stores everything in `~/.local/share/graftpunk/observe/`

Useful for debugging API interactions and discovering undocumented endpoints.

### Interactive Mode (`gp observe interactive`)

Interactive mode keeps the browser open for manual exploration while recording all network traffic:

```bash
gp observe -s mybank interactive https://secure.mybank.com/dashboard

# Or as a flag on observe go:
gp observe -s mybank go --interactive https://secure.mybank.com/dashboard
```

This:
1. Loads cookies and injects them into a visible browser
2. Navigates to the starting URL
3. Records all network traffic (with response bodies fetched eagerly via CDP)
4. Blocks until you press Ctrl+C
5. Saves HAR data, a final screenshot, page source, and console logs

The `--interactive` flag on `observe go` delegates to the same implementation — `--wait` is ignored when interactive mode is active.

Saved artifacts:
- `network.har` — Full HAR file with request/response bodies
- `screenshots/` — Final screenshot at time of Ctrl+C
- `page-source.html` — Page HTML at time of Ctrl+C
- `console.jsonl` — Browser console log entries
- `bodies/` — Raw response bodies streamed to disk during capture

---

## CLI Architecture

### GraftpunkApp

The CLI is built on Typer/Click. `GraftpunkApp` is a custom `typer.Typer` subclass that manages plugin group registration and provides a custom `__call__` that handles error output for unknown commands.

Plugin command groups are registered as Click subgroups via `add_plugin_group()`, which rejects duplicate names to prevent silent overwriting. Plugin subcommands use `TyperCommand` / `TyperGroup` for consistent rich help formatting across core and plugin commands. Nested subcommand groups created by `_ensure_group_hierarchy()` also use `TyperGroup` (not plain `click.Group`) to maintain rich help formatting at every nesting level.

### Logging

Structured logging via `structlog`. Quiet by default (`WARNING` level). Controlled by:

- `GRAFTPUNK_LOG_LEVEL` environment variable
- `-v` flag (sets `INFO`)
- `-vv` flag (sets `DEBUG`)
- `GRAFTPUNK_LOG_FORMAT` environment variable (`json` or `console`)
- `--network-debug` flag (enables wire-level HTTP tracing via `http.client`, `urllib3`, `httpx`, `httpcore`)

Logging is configured early (before plugin registration) so that plugin load messages respect the configured level. The `--network-debug` flag is independent of verbosity level — it enables stdlib DEBUG logging on network libraries and sets `HTTPConnection.debuglevel = 1` for raw HTTP traffic output.

---

## API Version Contract

`api_version = 1` defines the plugin interface contract:

- **Synchronous handlers** — Command handlers are regular methods (not `async def`). The framework calls them synchronously.
- **CommandContext** — Handlers receive a `CommandContext` with `session`, `base_url`, `config`, and `observe`.
- **LoginConfig-based declarative login** — Plugins declare login flows via `login_config = LoginConfig(...)` (a frozen dataclass).
- **`list[CommandSpec]` from `get_commands()`** — Plugins return a list of `CommandSpec` objects describing available commands.
- **`setup()` / `teardown()` lifecycle** — Plugins may define `setup()` (called once after registration) and `teardown()` (called during application shutdown).

New `api_version` values may introduce breaking changes to this contract (e.g., async-first handlers, different context shapes). Plugins that declare `api_version = 1` are guaranteed to work with the v1 interface.

---

## YAML Plugin Limitations

YAML plugins are ideal for simple REST API wrappers but have inherent limitations. When you outgrow YAML, "graduate" to a Python plugin for full flexibility:

- **No command groups** — YAML commands are flat; nested subcommands (`gp site group cmd`) require Python.
- **No request chaining** — Each command makes a single HTTP request. Multi-step workflows need Python.
- **No conditional logic** — No if/else branching based on response data or parameters.
- **No custom transforms** — Only JMESPath extraction is supported. Complex data reshaping needs Python.
- **No pagination** — Automatic page iteration requires Python loop logic.
- **No token refresh** — OAuth refresh flows or session renewal logic require Python.
- **No custom login flows** — YAML supports declarative `LoginConfig` only. Multi-step or CAPTCHA-handling login needs a Python `login()` method.

The Python template at `~/.config/graftpunk/plugins/` provides a starting point for migration.

---

## Output Format Precedence

The output format for command results follows this precedence (highest to lowest):

1. **`--format` CLI flag** — Explicit user choice always wins.
2. **`CommandResult.format_hint`** — Plugin author's suggested format (only applies when the user has not explicitly chosen a format, i.e., when the default `json` is in effect).
3. **Default (`json`)** — Pretty-printed JSON.

Example:

```python
@command(help="List users")
def users(self, ctx: CommandContext):
    data = ctx.session.get(f"{self.base_url}/api/users").json()
    return CommandResult(data=data, format_hint="table")
    # Displays as table by default, but --format json overrides
```

---

## Error Handling for Plugin Authors

Plugins should use the exception hierarchy for clear error reporting:

- **`CommandError("message")`** — Raise for expected failures: validation errors, missing resources, business rule violations. The framework displays the message cleanly without a traceback.
- **`PluginError("message")`** — Raise for infrastructure failures: missing configuration, broken dependencies, plugin setup errors.
- **Other exceptions** — Any unhandled exception is treated as a crash. The framework logs a full traceback and reports an internal error.

```python
from graftpunk.exceptions import CommandError

@command(help="Get account")
def account(self, ctx: CommandContext, account_id: int):
    resp = ctx.session.get(f"{self.base_url}/api/accounts/{account_id}")
    if resp.status_code == 404:
        raise CommandError(f"Account {account_id} not found")
    return resp.json()
```

---

## Async Handler Detection

Handlers must be synchronous in API version 1. If a handler is defined as `async def`, the framework auto-detects this and executes it using `asyncio.run()`, but this is not officially supported and may change in future versions. For reliable behavior, use synchronous handlers:

```python
# Supported (v1 contract)
@command(help="List items")
def items(self, ctx: CommandContext):
    return ctx.session.get(f"{self.base_url}/api/items").json()

# Auto-detected but not officially supported
@command(help="List items")
async def items(self, ctx: CommandContext):
    return ctx.session.get(f"{self.base_url}/api/items").json()
```

---

## Plugin Configuration Convention

Per-user plugin configuration files follow this convention:

```
~/.config/graftpunk/plugin-config/{site_name}.yaml
```

The graftpunk framework does **not** read this directory. It is a convention for plugin authors who need per-user settings (API keys, preferences, environment-specific URLs). Plugin code is responsible for loading and parsing its own config file:

```python
from pathlib import Path
import yaml

@command(help="List items")
def items(self, ctx: CommandContext):
    config_path = Path.home() / ".config/graftpunk/plugin-config" / f"{self.site_name}.yaml"
    if config_path.exists():
        plugin_settings = yaml.safe_load(config_path.read_text())
    # ... use plugin_settings
```

---

## Command Groups

The `@command` decorator supports both individual commands and command groups (nested subcommands). When applied to a class, it creates a command group; methods within the class become subcommands.

### Defining a Command Group

```python
from graftpunk.plugins import CommandContext, SitePlugin, command

class MyPlugin(SitePlugin):
    site_name = "bank"
    base_url = "https://api.bank.com"
    api_version = 1

    @command(help="Account operations")
    class Accounts:
        def list(self, ctx: CommandContext):
            return ctx.session.get(f"{ctx.base_url}/accounts").json()

        def detail(self, ctx: CommandContext, id: int):
            return ctx.session.get(f"{ctx.base_url}/accounts/{id}").json()
```

This creates CLI commands: `gp bank accounts list` and `gp bank accounts detail <id>`.

### Nesting Groups with `parent=`

Command groups can be nested using the `parent=` parameter:

```python
    @command(help="Account operations")
    class Accounts:
        def list(self, ctx: CommandContext):
            ...

        def detail(self, ctx: CommandContext, id: int):
            ...

    @command(help="Account statements", parent=Accounts)
    class Statements:
        def download(self, ctx: CommandContext, account_id: int):
            ...

        def summary(self, ctx: CommandContext, account_id: int):
            ...
```

This creates: `gp bank accounts list`, `gp bank accounts detail <id>`, `gp bank accounts statements download <account_id>`, etc.

### How It Works

- `@command` on a class stores `CommandGroupMeta` (name, help text, parent reference).
- `@command` on a method within a class stores `CommandMetadata` as usual.
- Methods starting with `_` are skipped during auto-discovery.
- `CommandSpec.group` links a command to its parent group name.
- `SitePlugin.get_commands()` flattens groups into `CommandSpec` objects with the `group` field set.

---

## Core Types Reference

| Type | Module | Frozen | Purpose |
|------|--------|--------|---------|
| `SitePlugin` | `plugins.cli_plugin` | N/A (class) | Base class for Python plugins |
| `CommandContext` | `plugins.cli_plugin` | No | Execution context: `session`, `plugin_name`, `command_name`, `api_version`, `base_url`, `config`, `observe` |
| `CommandResult` | `plugins.cli_plugin` | Yes | Optional structured return: `data`, `metadata`, `format_hint` |
| `CommandSpec` | `plugins.cli_plugin` | Yes | Command spec: `name`, `handler`, `help_text`, `params`, `timeout`, `max_retries`, `rate_limit`, `requires_session`, `group` |
| `CommandMetadata` | `plugins.cli_plugin` | Yes | Metadata stored by `@command` decorator on methods |
| `CommandGroupMeta` | `plugins.cli_plugin` | Yes | Metadata stored by `@command` decorator on classes (command groups) |
| `LoginConfig` | `plugins.cli_plugin` | Yes | Declarative browser login configuration |
| `PluginConfig` | `plugins.cli_plugin` | Yes | Canonical config: `site_name`, `session_name`, `help_text`, `base_url`, `requires_session`, `backend`, `api_version`, `username_envvar`, `password_envvar`, `login_config`, `plugin_version`, `plugin_author`, `plugin_url` |
| `PluginParamSpec` | `plugins.cli_plugin` | Yes | CLI parameter specification |
| `CLIPluginProtocol` | `plugins.cli_plugin` | N/A (Protocol) | Structural typing contract for all plugins |
| `CommandError` | `exceptions` | N/A | Expected command failure with user-facing message |
| `PluginError` | `exceptions` | N/A | Infrastructure/plugin loading failure |
| `ObservabilityContext` | `observe.context` | No | Plugin-facing observability handle |
| `NoOpObservabilityContext` | `observe.context` | No | Null object for disabled observability |
| `CaptureBackend` | `observe.capture` | N/A (Protocol) | Protocol for browser capture |
| `ObserveStorage` | `observe.storage` | No | File-based observability storage |
| `GraftpunkSession` | `session` | No | `requests.Session` subclass with browser header profiles |
| `BrowserSession` | `session` | No | Browser automation wrapper |
| `Token` | `tokens` | Yes | Token extraction configuration |
| `TokenConfig` | `tokens` | Yes | Collection of token extraction rules |
| `CachedToken` | `tokens` | Yes | Extracted token value with TTL |
| `PluginDiscoveryError` | `cli.plugin_commands` | Yes | Error during plugin discovery |
| `YAMLDiscoveryError` | `plugins.yaml_loader` | Yes | Error during YAML plugin loading |
| `PythonDiscoveryError` | `plugins.python_loader` | Yes | Error during Python plugin loading |

---

## Security

- **Session encryption** — All cached sessions are encrypted with Fernet (AES-128-CBC + HMAC-SHA256). Sessions are authenticated before decryption.
- **Checksum verification** — SHA256 checksums guard against data corruption.
- **TTL expiration** — Sessions expire after a configurable time (`GRAFTPUNK_SESSION_TTL_HOURS`).
- **Path validation** — Observability storage validates session names and run IDs against a strict allowlist pattern to prevent path traversal. Screenshot labels are sanitized.
- **Narrow exception handling** — Selenium operations catch `WebDriverException` specifically, not bare `Exception`. This prevents masking programming errors.
- **Frozen value types** — Configuration and metadata types (`PluginConfig`, `LoginConfig`, `CommandMetadata`, `PluginParamSpec`, discovery error types) are frozen dataclasses, preventing accidental mutation after construction.
- **Local-only by default** — Sessions are stored on the local filesystem. The Supabase backend is opt-in.

**Note:** Session deserialization uses `dill` (pickle). Only load sessions from trusted sources — this is by design for local-machine use.
