# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

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
  - `GraftpunkSession` subclass of `requests.Session` with browser header profiles
  - Captures real browser headers during login via CDP network events
  - Classifies headers into navigation, XHR, and form profiles
  - `load_session_for_api()` returns `GraftpunkSession` with XHR profile pre-loaded
  - Brotli support (`Accept-Encoding: gzip, deflate, br`)

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

- `PluginConfig` is now a frozen dataclass constructed via `build_plugin_config()` factory
- Login configuration extracted into `LoginConfig` frozen dataclass (replaces 5 flat fields)
- `get_commands()` returns `list[CommandSpec]` instead of `dict`
- `requires_session` flag replaces `session_name=""` hack for sessionless commands
- All metadata types are frozen dataclasses (`CommandMetadata`, `PluginParamSpec`, discovery errors)
- `BrowserSession` supports context manager protocol (sync and async)
- NoDriver cookie transfer improved with `inject_cookies_to_nodriver()`
- Chrome sandbox disabled by default for NoDriver; `--no-sandbox` warning suppressed
- Auto-detect Chrome version for matching ChromeDriver

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
