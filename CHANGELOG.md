# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- **`--format` flag now overrides `format_hint`**: When a user explicitly passes `--format`/`-f` on the command line, the plugin's `CommandResult.format_hint` is ignored so the user's choice always wins (#94)

### Added

- **HTTP Request Header Roles** (`--role`): Set browser header roles on `gp http` commands (#92)
  - Built-in roles: `navigation`, `xhr`, `form` — registered via `register_role()`. CLI accepts `navigate` as shorthand for `navigation`
  - Plugin-defined custom roles: plugins can declare a `header_roles` dict with arbitrary names
  - `--role <name>` dispatches via `request_with_role()` for any role name
  - Replaces manual multi-header overrides with a single flag

## [1.4.0] - 2026-02-08

### Added

- **First-Class Python API** (`GraftpunkClient`): Programmatic access to plugin commands (#90)
  - `GraftpunkClient` — stateful, context-manager-friendly client that wraps a single plugin
  - Attribute-based dispatch: `client.invoice.list(status="OPEN")`
  - String dispatch: `client.execute("invoice", "list", status="OPEN")`
  - Lazy session loading, token injection, 403 retry, and session persistence — same pipeline as the CLI
  - Exported from top-level package: `from graftpunk import GraftpunkClient`

- **Shared Plugin Discovery API**: `discover_all_plugins()` and `get_plugin()` in `graftpunk.plugins`
  - Unified plugin lookup across entry points, YAML files, and Python files
  - Cached discovery with `lru_cache` (call `discover_all_plugins.cache_clear()` to force refresh)
  - Used by both the CLI and `GraftpunkClient`

- **Shared Execution Core**: `execute_plugin_command()` in `graftpunk.client`
  - Handles retry/rate-limit and `CommandResult` normalization
  - CLI callback delegates to this function instead of maintaining its own execution logic

### Changed

- CLI plugin registration now delegates to `discover_all_plugins()` instead of calling individual discovery functions directly
- Retry and rate-limit logic unified into `_run_handler_with_limits()` — single implementation shared by both the CLI and Python API paths
- `close()` on `GraftpunkClient` wraps session persistence in try-except so failures don't prevent plugin teardown

## [1.3.0] - 2026-02-07

### Added

- **S3-Compatible Storage Backend**: Session persistence on S3-compatible object storage
  - Supports Cloudflare R2 (zero egress fees), AWS S3, MinIO, and any S3-compatible service
  - Retry logic with exponential backoff and jitter for transient failures
  - Region='auto' handling for Cloudflare R2
  - Install with: `pip install graftpunk[s3]`

- **Structured Output System**: OutputConfig for declarative table/CSV formatting
  - `OutputConfig` dataclass with named views, column definitions, and default view selection
  - OutputConfig support in YAML plugins via `output:` block
  - `output_config` field on `CommandResult` for plugin-controlled formatting

- **Multi-Step Login Support**: Identifier-first authentication flows (#77)
  - `LoginStep` dataclass for defining individual steps in a login flow
  - `LoginConfig.steps` list replaces flat fields for multi-step scenarios
  - Nodriver and Selenium engines both support multi-step flows
  - YAML `login.steps:` block with the same capabilities

- **Resilient Element Selection**: Retry and wait_for in login engine (#67)
  - `wait_for` field on `LoginConfig` for post-login element waiting
  - `_select_with_retry` deadline-based retry helper for nodriver's `tab.select()`
  - Handles `ProtocolException` during page transitions

- **`--no-session` Flag**: Run `observe` and `http` commands without a pre-existing session (#54, #56)

- **First-Class CSV Output Formatter**: Dedicated `CsvFormatter` with fallback handling (#57)

- **Click Kwargs Passthrough**: Fine-grained plugin parameter control via `click_kwargs` on `CommandSpec` (#72)

- **Interactive Observe Mode**: Record browser sessions interactively with `gp observe interactive`
  - Opens authenticated browser at a URL, records all network traffic while you click around
  - Press Ctrl+C to stop and save HAR files, screenshots, page source, and console logs
  - Also available as `gp observe go --interactive` (`-i`) flag on existing command
  - Captures response bodies eagerly via CDP `LoadingFinished` events (prevents buffer eviction)

- **EAFP Token Injection**: Optimistic token injection with 403 retry
  - Cached tokens are injected even when expired — if the server rejects with 403, tokens are refreshed and the request retried once
  - Tokens extracted during login are persisted through session serialization (pickle roundtrip)
  - `update_session_cookies()` preserves the token cache when saving session changes

- **Token Polling with Retry**: Robust token extraction from dynamic pages
  - `_poll_for_tokens()` checks page content up to 6 times (0.5s intervals) for token patterns
  - Handles bot challenges (e.g., Akamai) and lazy-rendered pages without wasting time on fast ones
  - Checks content first, sleeps only between retries (no unconditional initial delay)

- **Login-Time Token Extraction**: Extract tokens during login without a separate browser launch
  - Nodriver and Selenium login engines extract tokens from the already-open browser session
  - Cookie-based and page-based token sources both supported during login
  - `_build_token_cache()` shared helper eliminates duplication between backends

- **Eager CDP Body Fetching**: Response bodies captured before Chrome evicts them
  - `NodriverCaptureBackend` listens for `Network.LoadingFinished` CDP events
  - Bodies fetched immediately via `Network.getResponseBody` and streamed to disk
  - Async capture with `start_capture_async()` / `stop_capture_async()` for interactive mode

- **Network Debug Flag**: `--network-debug` CLI flag for wire-level HTTP tracing
  - Enables `HTTPConnection.debuglevel = 1` for raw HTTP traffic on stderr
  - Sets `urllib3`, `httpx`, and `httpcore` loggers to DEBUG level
  - Independent of `-v`/`-vv` verbosity — can be combined with any log level

- **Bot-Detection Cookie Filtering**: Skip WAF tracking cookies when injecting into browsers
  - Akamai cookies (`bm_*`, `ak_bmsc`, `_abck`) filtered by default in `inject_cookies_to_nodriver()`
  - Prevents `ERR_HTTP2_PROTOCOL_ERROR` caused by stale bot-classification state
  - Opt-out via `skip_bot_cookies=False` parameter
  - Extensible to Cloudflare, Imperva, PerimeterX, and DataDome WAFs

- **Plugin Interface v1**: Full command framework for building CLI tools on top of authenticated sessions
  - `SitePlugin` base class with `@command` decorator for defining CLI commands
  - `CommandContext` dataclass injected into handlers with session, plugin metadata, and observability
  - `CommandSpec` with per-command `timeout`, `max_retries`, and `rate_limit` enforcement
  - `CommandResult` with `format_hint` for plugin-controlled output formatting
  - `CommandError` exception for user-facing error messages without tracebacks
  - `CLIPluginProtocol` runtime-checkable structural typing contract
  - `api_version` field for forward-compatible plugin interface negotiation
  - Command groups with `parent=` nesting via `@command` decorator on classes
  - `setup()` / `teardown()` lifecycle hooks
  - Async handler auto-detection (with deprecation warning for v1)

- **Declarative Login Engine**: Define login flows with CSS selectors instead of writing automation code
  - `LoginConfig` frozen dataclass: `url`, `fields`, `submit`, `failure`, `success`
  - Auto-generates `gp <plugin> login` CLI command from declarative config
  - Generates sync (Selenium) or async (NoDriver) login functions automatically
  - Supports flat class attributes (`login_url`, `login_fields`, etc.) for ergonomics
  - YAML `login:` block with the same capabilities
  - Customizable credential environment variable names per plugin
  - Interactive prompts with masked input for password fields

- **Browser Header Replay**: Requests look like they came from Chrome, not Python
  - `GraftpunkSession` subclass of `requests.Session` with browser header roles
  - Captures real browser headers during login via CDP network events
  - Classifies headers into navigation, XHR, and form roles
  - `load_session_for_api()` returns `GraftpunkSession` with browser header roles
  - Brotli support (`Accept-Encoding: gzip, deflate, br`)
  - **Request-type methods** (#50): `xhr()`, `navigate()`, `form_submit()` for explicit role control
    - Each method applies the correct captured (or registered fallback) headers for that request type
    - `referer` kwarg resolves paths against `gp_base_url` (e.g., `referer="/invoice/list"`)
    - Caller-supplied headers override role headers
    - Eliminates boilerplate: plugins no longer need to build request headers manually

- **Token and CSRF Support**: Declarative token extraction and auto-injection
  - `Token` and `TokenConfig` types for declarative token definitions
  - Extract tokens from cookies, response headers, or page content (regex)
  - Auto-inject tokens into request headers before each command
  - Auto-retry with fresh tokens on 403 responses
  - YAML `tokens:` block for declarative configuration

- **Ad-hoc HTTP Requests** (`gp http`): Make authenticated requests without writing a plugin
  - Supports all HTTP methods: `get`, `post`, `put`, `patch`, `delete`, `head`, `options`
  - Uses `GraftpunkSession` with full browser header replay

- **Observability System**: Capture browser activity for debugging and auditing
  - `ObservabilityContext` with `mark()`, `screenshot()`, `log()` methods
  - `gp observe go` — open authenticated browser and capture network traffic
  - Full network capture with request/response bodies and console logs
  - HAR file generation with disk-streamed body support
  - Screenshot capture (Selenium backend)
  - `NoOpObservabilityContext` for zero-overhead when disabled
  - `gp observe list/show/clean` for managing captured data
  - `--observe full` flag on all commands

- **Session Management Redesign**: All session commands under `gp session` subgroup
  - `gp session list` / `show` / `clear` / `export` (moved from top-level)
  - `gp session use <name>` / `gp session unset` — active session context
  - Session name validation (no dots allowed)
  - Plugin site_name resolution as alias in session commands
  - Session persistence after commands (`saves_session` flag, `update_session_cookies()`)

- **Plugin Discovery Improvements**
  - Python file auto-discovery from `~/.config/graftpunk/plugins/*.py`
  - Plugin collision detection (fail-fast on duplicate `site_name`)
  - `site_name` auto-inference from `base_url` domain or YAML filename
  - Partial success: valid plugins load even when others fail
  - Unified error collection across all discovery sources

- **Example Plugins and Templates**
  - `httpbin.yaml` — YAML plugin for httpbin.org (no auth, demonstrates all YAML features)
  - `quotes.py` — Python/Selenium plugin with declarative login (test site)
  - `hackernews.py` — Python/NoDriver plugin with declarative login (real site)
  - `yaml_template.yaml` and `python_template.py` starter templates

- **CLI Improvements**
  - `GraftpunkApp` custom Typer subclass with plugin group registration
  - Rich help formatting for all plugin commands (`TyperCommand` / `TyperGroup`)
  - Default log verbosity reduced to WARNING; `-v` (info), `-vv` (debug) flags
  - `GRAFTPUNK_LOG_FORMAT` env var and `--log-format` CLI flag
  - Clean error output for unknown commands
  - `gp_console` module for centralized Rich terminal output with Status spinners
  - Auto-introspection of Python plugin method parameters for CLI argument generation

### Changed

- **Supabase storage backend** refactored to pure file-based storage. No longer uses `session_cache` database table. Users with existing Supabase sessions will need to re-login.
- All storage backends now use the same file-pair pattern: `{session_name}/session.pickle` + `metadata.json`
- `LoginConfig` restructured to use `steps` list for multi-step login flows
- `format_output` writes to stdout instead of stderr (#60)
- Example plugins updated for steps-based LoginConfig API
- `PluginConfig` is now a frozen dataclass constructed via `build_plugin_config()` factory
- Login configuration extracted into `LoginConfig` frozen dataclass (replaces 5 flat fields)
- `get_commands()` returns `list[CommandSpec]` instead of `dict`
- `requires_session` flag replaces `session_name=""` hack for sessionless commands
- All metadata types are frozen dataclasses (`CommandMetadata`, `PluginParamSpec`, discovery errors)
- `BrowserSession` supports context manager protocol (sync and async)
- `inject_cookies_to_nodriver()` returns `tuple[int, int]` (injected, skipped) instead of `int`; callers can now see how many cookies were filtered
- `inject_cookies_to_nodriver()` logs a warning when all cookies are filtered (indicates the session may not work)
- `GraftpunkSession.__init__` now accepts `base_url` keyword argument for Referer path resolution
- `_detect_role()` classifies non-GET/POST methods as XHR (was: navigation); registered role headers used as fallback when a captured role is missing
- Chrome sandbox disabled by default for NoDriver; `--no-sandbox` warning suppressed
- Auto-detect Chrome version for matching ChromeDriver

### Fixed

- Graceful observe shutdown on Ctrl+C and browser close (#69)
- CDP eager body fetch failures (#64)
- Session headers contaminating GET requests (#65)
- `load_session_for_api` overwrites browser UA with python-requests default (#52)
- Silent failures in S3 storage replaced with explicit `StorageError` exceptions
- **Browser identity header leak** (#49): `GraftpunkSession` now separates browser identity headers (User-Agent, sec-ch-ua, Accept-Language, Accept-Encoding, etc.) from request-type headers (Accept, Sec-Fetch-*). Identity headers are set as session defaults at init, preventing `python-requests` User-Agent from ever reaching the wire when roles exist. When a detected role wasn't captured during login, registered role headers are used as fallback instead of silently applying no headers. `_detect_role()` now correctly classifies DELETE/PUT/PATCH/HEAD/OPTIONS as XHR per HTML spec §4.10.18.6 (forms only support GET and POST).
- Nested plugin subcommand groups (e.g. `gp bek invoice`) now use `TyperGroup` instead of plain `click.Group`, so `--help` output gets the same rich formatting as top-level commands

## [1.2.1] - 2026-01-28

### Changed

- **NoDriver now included by default**: `pip install graftpunk` includes both Selenium and NoDriver backends
- The `[nodriver]` extra is kept for backwards compatibility but is now a no-op

## [1.2.0] - 2026-01-27

### Added

- **Browser Abstraction Layer**: Pluggable browser backend architecture
  - `BrowserBackend` Protocol defining the browser automation interface
  - `SeleniumBackend` wrapping existing stealth stack (undetected-chromedriver + selenium-stealth)
  - `NoDriverBackend` for CDP-direct automation without WebDriver binary detection
  - Backend factory with `get_backend()`, `list_backends()`, `register_backend()`
  - `Cookie` TypedDict for type-safe cookie handling across backends

- **NoDriver Integration**: CDP-direct browser automation
  - Eliminates WebDriver binary detection vector
  - Async-to-sync bridging for consistent API
  - Better anti-detection for enterprise-protected sites

### Changed

- `BrowserSession` now accepts `backend` parameter ("selenium", "nodriver", or "legacy")
- Exported `BrowserBackend`, `get_backend`, `list_backends`, `register_backend` from main package

## [1.1.0] - 2026-01-25

### Added

- **HAR File Import** (`gp import-har`): Generate plugins from browser network captures
  - Parse HAR files exported from browser dev tools
  - Detect authentication flows (login forms, OAuth, redirects, session cookies)
  - Discover API endpoints from captured JSON responses
  - Generate plugins in Python or YAML format
  - Dry-run mode for previewing output without writing files

- **YAML Plugin System**: Declarative command definitions without Python
  - Define site commands in simple YAML files
  - Automatic parameter handling with type validation
  - Environment variable expansion for secrets
  - JMESPath support for response extraction
  - Drop-in plugin discovery from `~/.config/graftpunk/plugins/`

- **Python Plugin System**: Extensible command architecture
  - `@command` decorator for custom command logic
  - `SitePlugin` base class with session integration
  - Dynamic command registration at CLI startup
  - Output formatting (`--format json|table|raw`) on all plugin commands

### Changed

- Extracted keepalive subcommands to dedicated module for cleaner architecture
- Improved plugin discovery with structured error reporting

## [1.0.0] - 2026-01-23

### Added

- Initial release of graftpunk as a standalone package
- Encrypted session persistence with Fernet (AES-128-CBC + HMAC-SHA256)
- Stealth browser automation with undetected-chromedriver and selenium-stealth
- Pluggable storage backends: local filesystem, Supabase, S3
- Session keepalive daemon with customizable handlers
- Plugin architecture via Python entry points
- MFA support: TOTP generation, reCAPTCHA detection, magic link extraction
- CLI interface for session management
- Full type annotations with py.typed marker

### Storage Backends

- **Local**: File-based storage with configurable directory
- **Supabase**: Cloud storage with Vault integration for key management
- **S3**: AWS S3 bucket storage

### Security

- Fernet encryption for all session data
- SHA-256 checksum validation before deserialization
- 0600 permissions on local key files
- Supabase Vault integration for cloud key storage
