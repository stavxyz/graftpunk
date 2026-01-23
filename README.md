<div align="center">

# 🔐 BSC

**Browser Session Cache**

*Never re-authenticate your automated browsers again.*

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Code style: ruff](https://img.shields.io/badge/code%20style-ruff-000000.svg)](https://github.com/astral-sh/ruff)
[![Typed](https://img.shields.io/badge/typed-mypy-blue.svg)](https://mypy-lang.org/)

[Installation](#installation) • [Quick Start](#quick-start) • [CLI](#cli) • [Configuration](#configuration) • [Plugins](#plugins)

</div>

---

## The Problem

You're automating a browser workflow. It works great—until your script restarts and you're back at the login screen. Again.

**BSC solves this.** It captures your authenticated browser session—cookies, headers, everything—encrypts it, and stores it safely. Next time? Your session is restored instantly.

## Features

| | Feature | What It Does |
|:--|:--|:--|
| 🔒 | **Encrypted Storage** | AES-128 encryption (Fernet) with HMAC authentication |
| 🥷 | **Stealth Mode** | Undetected ChromeDriver + selenium-stealth to avoid bot detection |
| ☁️ | **Cloud Storage** | Store sessions in Supabase or S3 for multi-machine access |
| 🔄 | **Keepalive Daemon** | Background process that keeps sessions from expiring |
| 🔌 | **Plugin System** | Extend with custom storage backends and site handlers |
| 🛠️ | **Beautiful CLI** | Rich terminal output with session management commands |

## Installation

```bash
pip install bsc
```

**With cloud storage:**

```bash
pip install bsc[supabase]   # Supabase backend
pip install bsc[s3]         # AWS S3 backend
pip install bsc[all]        # Everything
```

## Quick Start

### Cache a Session

```python
from bsc import BrowserSession, cache_session

# Create a stealth browser
session = BrowserSession(headless=False, use_stealth=True)

# Log in manually or via automation
session.driver.get("https://app.example.com/login")
# ... authentication happens ...

# Cache it
cache_session(session, "example")
print("Session cached!")
```

### Restore Later

```python
from bsc import load_session

# Restore without logging in again
session = load_session("example")
session.driver.get("https://app.example.com/dashboard")
# You're already authenticated ✨
```

### API-Only Mode (No Browser)

```python
from bsc import load_session_for_api

# Get a requests session with all the cookies
api = load_session_for_api("example")
response = api.get("https://app.example.com/api/data")
print(response.json())
```

## CLI

BSC includes a beautiful command-line interface:

```
$ bsc --help

 🔐 BSC - Browser Session Cache

 Securely cache and restore authenticated browser sessions.
 Sessions are encrypted with Fernet (AES-128) and stored locally or in the cloud.

 Quick start:
   bsc list              Show all cached sessions
   bsc show <name>       View session details
   bsc clear <name>      Remove a session
   bsc config            Show current configuration

 Documentation: https://github.com/stavxyz/bsc

Commands:
  list       List all cached sessions with status and metadata.
  show       Show detailed information about a cached session.
  clear      Remove cached session(s).
  export     Export session cookies to HTTPie format.
  config     Show current BSC configuration.
  plugins    List discovered plugins (storage, handlers, sites).
  version    Show BSC version and installation info.
  keepalive  Manage the session keepalive daemon.
```

### List Sessions

```
$ bsc list

              🔐 Cached Sessions
┏━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━┳━━━━━━━━━┳━━━━━━━━━━━━━━━━━━┓
┃ Session     ┃ Domain           ┃   Status   ┃ Cookies ┃ Last Modified    ┃
┡━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━╇━━━━━━━━━╇━━━━━━━━━━━━━━━━━━┩
│ github      │ github.com       │  ● active  │      12 │ 2024-01-15 09:30 │
│ stripe      │ dashboard.stripe │  ● active  │       8 │ 2024-01-14 14:22 │
│ oldservice  │ app.oldsite.com  │ ○ expired  │       5 │ 2024-01-01 11:00 │
└─────────────┴──────────────────┴────────────┴─────────┴──────────────────┘

3 session(s) cached
```

### Show Session Details

```
$ bsc show github

╭──────────────────────────────────────────────────────────╮
│ ● github  active                                         │
│                                                          │
│ Domain:     github.com                                   │
│ Cookies:    12                                           │
│ Created:    2024-01-10 08:15:30                          │
│ Modified:   2024-01-15 09:30:45                          │
│ Expires:    2024-02-09 08:15:30                          │
│ Domains:    github.com, api.github.com                   │
╰──────────────────────────────────────────────────────────╯
```

### Configuration

```
$ bsc config

╭─────────────────── ⚙ Configuration ───────────────────╮
│ Config directory:   /home/user/.config/bsc            │
│ Sessions directory: /home/user/.config/bsc/sessions   │
│ Storage backend:    local (filesystem)                │
│ Session TTL:        720 hours (30 days)               │
│ Log level:          INFO                              │
╰───────────────────────────────────────────────────────╯
```

### Export to HTTPie

```
$ bsc export github

✓ Exported to: /home/user/.config/httpie/sessions/github.com/github.json

Usage:
  http --session=github https://api.github.com/user
```

## Configuration

BSC uses environment variables with the `BSC_` prefix:

| Variable | Default | Description |
|:---------|:--------|:------------|
| `BSC_STORAGE_BACKEND` | `local` | Storage: `local`, `supabase`, or `s3` |
| `BSC_CONFIG_DIR` | `~/.config/bsc` | Config and encryption key location |
| `BSC_SESSION_TTL_HOURS` | `720` | Session lifetime (default: 30 days) |
| `BSC_LOG_LEVEL` | `INFO` | Logging verbosity |

### Supabase Backend

```bash
export BSC_STORAGE_BACKEND=supabase
export BSC_SUPABASE_URL=https://xxx.supabase.co
export BSC_SUPABASE_SERVICE_KEY=your-key
```

### S3 Backend

```bash
export BSC_STORAGE_BACKEND=s3
export BSC_S3_BUCKET=my-sessions-bucket
export AWS_ACCESS_KEY_ID=xxx
export AWS_SECRET_ACCESS_KEY=xxx
```

## How It Works

```
┌─────────────────────────────────────────────────────────────┐
│                      Your Application                        │
├─────────────────────────────────────────────────────────────┤
│                                                              │
│    ┌────────────┐         ┌────────────┐                    │
│    │  Browser   │         │  API-only  │                    │
│    │  Session   │         │   Session  │                    │
│    └─────┬──────┘         └─────┬──────┘                    │
│          │                      │                            │
│          └──────────┬───────────┘                            │
│                     ▼                                        │
│    ┌────────────────────────────────────────┐               │
│    │          SessionCache                   │               │
│    │   • serialize with dill                 │               │
│    │   • encrypt with Fernet (AES-128)       │               │
│    │   • checksum with SHA-256               │               │
│    └────────────────────┬───────────────────┘               │
│                         ▼                                    │
│    ┌────────────────────────────────────────┐               │
│    │         Storage Backend                 │               │
│    │  ┌─────────┐ ┌──────────┐ ┌─────────┐  │               │
│    │  │  Local  │ │ Supabase │ │   S3    │  │               │
│    │  └─────────┘ └──────────┘ └─────────┘  │               │
│    └────────────────────────────────────────┘               │
│                                                              │
└─────────────────────────────────────────────────────────────┘
```

## Plugins

BSC is extensible via Python entry points.

### Custom Storage Backend

```python
# my_package/storage.py
from bsc.storage.base import SessionStorageBackend, SessionMetadata

class RedisStorage:
    def save_session(self, name: str, data: bytes, metadata: SessionMetadata) -> str:
        ...

    def load_session(self, name: str) -> tuple[bytes, SessionMetadata]:
        ...

    def list_sessions(self) -> list[str]:
        ...

    def delete_session(self, name: str) -> bool:
        ...
```

Register in `pyproject.toml`:

```toml
[project.entry-points."bsc.storage"]
redis = "my_package.storage:RedisStorage"
```

### Custom Keepalive Handler

```python
# my_package/handler.py
from bsc.keepalive.handler import KeepaliveHandler, SessionStatus

class MyAppHandler:
    site_name = "myapp"

    def touch_session(self, session) -> tuple[bool, SessionStatus | None]:
        """Ping the app to keep the session alive."""
        r = session.get("https://myapp.com/api/ping")
        return r.ok, SessionStatus(authenticated=r.ok)

    def validate_session(self, session) -> bool:
        """Verify we're actually logged in."""
        r = session.get("https://myapp.com/api/me")
        return r.status_code == 200
```

Register:

```toml
[project.entry-points."bsc.keepalive_handlers"]
myapp = "my_package.handler:MyAppHandler"
```

## Security

### Encryption

- **Algorithm:** Fernet (AES-128-CBC + HMAC-SHA256)
- **Key storage:** `~/.config/bsc/.session_key` with `0600` permissions
- **Integrity:** SHA-256 checksum validated before deserializing

### ⚠️ Important: Pickle Warning

BSC uses Python's `pickle` for session serialization. Pickle can execute arbitrary code during deserialization.

**Mitigations:**
- Sessions are encrypted—attackers need your key
- Checksums are validated before unpickling
- Only load sessions you created

**Best practices:**
- Keep your encryption key secure
- Don't share session files
- Run BSC on trusted machines only

### Thread Safety

BSC is **not thread-safe**. For concurrent use:
- Use separate `BrowserSession` instances per thread
- Or wrap operations with locks
- For async: use `asyncio.to_thread()`

## Development

```bash
# Clone and setup
git clone https://github.com/stavxyz/bsc.git
cd bsc
just setup

# Run checks
just check

# Run tests
just test

# Build for PyPI
just build
```

## License

MIT License—see [LICENSE](LICENSE).

## Acknowledgments

BSC builds on these excellent projects:

- [requestium](https://github.com/tryolabs/requestium) – Selenium + Requests integration
- [undetected-chromedriver](https://github.com/ultrafunkamsterdam/undetected-chromedriver) – Anti-detection ChromeDriver
- [selenium-stealth](https://github.com/diprajpatra/selenium-stealth) – Stealth patches
- [cryptography](https://cryptography.io/) – Encryption primitives
- [rich](https://github.com/Textualize/rich) – Beautiful terminal output
- [typer](https://typer.tiangolo.com/) – CLI framework

---

<div align="center">
<sub>Built with 🔐 by <a href="https://github.com/stavxyz">@stavxyz</a></sub>
</div>
