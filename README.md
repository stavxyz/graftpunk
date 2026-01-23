<div align="center">

# BSC

**Browser Session Cache**

*Encrypted browser session persistence with stealth automation and pluggable storage*

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Code style: ruff](https://img.shields.io/badge/code%20style-ruff-000000.svg)](https://github.com/astral-sh/ruff)
[![Typed](https://img.shields.io/badge/typed-mypy-blue.svg)](https://mypy-lang.org/)

</div>

---

## What is BSC?

BSC is a Python library for persisting authenticated browser sessions across runs. It captures cookies, headers, and session state from a browser, encrypts them, and stores them locally or in the cloud. Later, sessions can be restored without re-authenticating.

**Key use cases:**

- **Automated workflows** that need to survive restarts
- **CI/CD pipelines** requiring authenticated API access
- **Session keepalive daemons** that maintain login state
- **Multi-machine deployments** sharing session state via cloud storage

## Features

| Feature | Description |
|---------|-------------|
| **Encrypted Persistence** | Fernet AES-128-CBC + HMAC-SHA256 encryption |
| **Stealth Automation** | undetected-chromedriver + selenium-stealth to avoid bot detection |
| **Pluggable Storage** | Local filesystem, Supabase, S3 (extensible) |
| **Keepalive Daemon** | Automatic session refresh with customizable handlers |
| **Plugin Architecture** | Entry points for site-specific authentication |
| **MFA Support** | TOTP generation, reCAPTCHA detection, magic link extraction |
| **CLI Interface** | Session management and daemon control |
| **Type Safe** | Full type annotations with py.typed marker |

## Installation

```bash
# Core package
pip install bsc

# With Supabase storage backend
pip install bsc[supabase]

# With S3 storage backend
pip install bsc[s3]

# With all extras (including dev tools)
pip install bsc[all]
```

## Quick Start

### Basic Session Caching

```python
from bsc import BrowserSession, cache_session, load_session

# Create a stealth browser session
session = BrowserSession(headless=True, use_stealth=True)

# Navigate and authenticate
session.driver.get("https://example.com/login")
# ... perform login steps ...

# Cache the authenticated session
cache_session(session, "example")

# Later: restore without re-authenticating
session = load_session("example")
session.driver.get("https://example.com/dashboard")
```

### API-Only Usage (No Browser)

```python
from bsc import load_session_for_api

# Load session for HTTP requests without spawning a browser
api_session = load_session_for_api("example")
response = api_session.get("https://example.com/api/data")
print(response.json())
```

### CLI Usage

```bash
# List all cached sessions
bsc list

# Show session details (cookies, expiry, metadata)
bsc show example

# Export cookies to HTTPie session format
bsc export example

# Clear a session
bsc clear example

# Run keepalive daemon
bsc keepalive start example --handler mypackage:MyHandler
```

## Configuration

BSC uses environment variables with the `BSC_` prefix:

| Variable | Default | Description |
|----------|---------|-------------|
| `BSC_STORAGE_BACKEND` | `local` | Storage backend: `local`, `supabase`, `s3` |
| `BSC_CONFIG_DIR` | `~/.config/bsc` | Configuration and key storage directory |
| `BSC_SESSION_TTL_HOURS` | `720` | Session time-to-live (30 days default) |
| `BSC_LOG_LEVEL` | `INFO` | Logging level |

### Supabase Backend

```bash
export BSC_STORAGE_BACKEND=supabase
export SUPABASE_URL=https://your-project.supabase.co
export SUPABASE_SERVICE_KEY=your-service-key
export BSC_SESSION_KEY_VAULT_NAME=session-encryption-key
```

### S3 Backend

```bash
export BSC_STORAGE_BACKEND=s3
export AWS_ACCESS_KEY_ID=your-access-key
export AWS_SECRET_ACCESS_KEY=your-secret-key
export BSC_S3_BUCKET=your-bucket-name
```

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                         Application                              │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│   ┌──────────────┐    ┌──────────────┐    ┌──────────────┐     │
│   │ BrowserSession│    │ cache_session │    │ load_session │     │
│   │              │───▶│              │    │              │     │
│   │ (Requestium) │    │              │◀───│              │     │
│   └──────────────┘    └──────┬───────┘    └──────┬───────┘     │
│                              │                    │              │
│   ┌──────────────────────────┴────────────────────┘              │
│   │                                                              │
│   ▼                                                              │
│   ┌──────────────────────────────────────────────────────────┐  │
│   │                    Encryption Layer                       │  │
│   │              (Fernet AES-128-CBC + HMAC)                 │  │
│   └──────────────────────────┬───────────────────────────────┘  │
│                              │                                   │
│   ┌──────────────────────────┴───────────────────────────────┐  │
│   │                    Storage Backend                        │  │
│   │  ┌─────────┐    ┌──────────┐    ┌────────┐              │  │
│   │  │  Local  │    │ Supabase │    │   S3   │              │  │
│   │  │  Files  │    │  + Vault │    │        │              │  │
│   │  └─────────┘    └──────────┘    └────────┘              │  │
│   └──────────────────────────────────────────────────────────┘  │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

## Plugin Development

BSC is extensible via Python entry points. Create custom storage backends, keepalive handlers, or site-specific plugins.

### Custom Storage Backend

Implement the `SessionStorageBackend` protocol:

```python
from bsc.storage.base import SessionStorageBackend, SessionMetadata

class RedisStorage:
    """Store sessions in Redis."""

    def save_session(self, name: str, data: bytes, metadata: SessionMetadata) -> str:
        # Save encrypted data and metadata
        ...
        return session_id

    def load_session(self, name: str) -> tuple[bytes, SessionMetadata]:
        # Load and return encrypted data with metadata
        ...

    def list_sessions(self) -> list[str]:
        # Return list of session names
        ...

    def delete_session(self, name: str) -> bool:
        # Delete session, return True if existed
        ...
```

Register via entry points in `pyproject.toml`:

```toml
[project.entry-points."bsc.storage"]
redis = "mypackage.storage:RedisStorage"
```

### Custom Keepalive Handler

Implement the `KeepaliveHandler` protocol to keep sessions alive for specific sites:

```python
from bsc.keepalive.handler import KeepaliveHandler, SessionStatus

class MyAppHandler:
    """Keepalive handler for MyApp."""

    site_name: str = "MyApp"

    def touch_session(self, api_session) -> tuple[bool, SessionStatus | None]:
        """Touch session to prevent expiry.

        Returns:
            Tuple of (success, optional status with details)
        """
        response = api_session.get("https://myapp.com/api/ping")
        return response.ok, SessionStatus(authenticated=response.ok)

    def validate_session(self, api_session) -> bool:
        """Verify session is truly authenticated."""
        response = api_session.get("https://myapp.com/api/me")
        return response.status_code == 200
```

Register via entry points:

```toml
[project.entry-points."bsc.keepalive_handlers"]
myapp = "mypackage.handler:MyAppHandler"
```

## Security

### Encryption Model

- **Algorithm**: Fernet (AES-128-CBC + HMAC-SHA256)
- **Key Storage**: Local file with 0600 permissions, or cloud vault (Supabase Vault)
- **Defense in Depth**: SHA-256 checksum validation before unpickling
- **Runtime Validation**: Session objects validated after deserialization

### Threat Model

BSC assumes you control the machines running it. The encryption protects session confidentiality at rest and in transit. Key compromise + file access would be required for an attacker to craft malicious payloads.

### Security Recommendations

1. **Run on trusted machines only** - BSC stores authentication credentials
2. **Protect your encryption key** - The key file (`~/.config/bsc/.session_key`) must be kept confidential
3. **Use cloud storage for multi-machine** - Supabase/S3 provide transport encryption
4. **Rotate sessions periodically** - Clear and re-create sessions to limit exposure

### ⚠️ Security Warning: Pickle Deserialization

BSC uses Python's pickle module for session serialization. **Pickle deserialization can execute arbitrary code** - this is a well-known attack vector.

While BSC protects sessions with encryption and SHA-256 checksum validation before unpickling, these safeguards assume:
- The encryption key remains confidential
- The machine running BSC is trusted
- Session files originate from trusted sources

**Only run BSC on trusted machines and never load sessions from untrusted sources.**

## Thread Safety

BSC uses global caches for encryption keys and storage backends. These are **NOT thread-safe**. For multi-threaded applications:

- **Recommended**: Use separate `BrowserSession` and storage instances per thread
- **Alternative**: Implement external synchronization (locks) around BSC operations
- **For async code**: BSC is synchronous; use `asyncio.to_thread()` for concurrent operations

## License

MIT License - see [LICENSE](LICENSE) for details.

## Contributing

Contributions are welcome! Please read [CONTRIBUTING.md](CONTRIBUTING.md) for:

- Development setup instructions
- Code style guidelines
- Testing requirements
- Pull request process

## Acknowledgments

Built on the shoulders of giants:

- [requestium](https://github.com/tryolabs/requestium) - Selenium + requests integration
- [undetected-chromedriver](https://github.com/ultrafunkamsterdam/undetected-chromedriver) - Anti-detection ChromeDriver
- [selenium-stealth](https://github.com/diprajpatra/selenium-stealth) - Stealth mode for Selenium
- [cryptography](https://cryptography.io/) - Cryptographic recipes

---

<div align="center">
<sub>Made with determination and too much coffee</sub>
</div>
