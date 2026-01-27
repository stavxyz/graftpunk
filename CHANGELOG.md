# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
  - Install with `pip install graftpunk[nodriver]` or `pip install graftpunk[standard]`

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
