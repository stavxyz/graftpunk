<div align="center">

# 🔐 BSC

**Browser Session Cache**

*Turn any website into an API.*

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Code style: ruff](https://img.shields.io/badge/code%20style-ruff-000000.svg)](https://github.com/astral-sh/ruff)
[![Typed](https://img.shields.io/badge/typed-mypy-blue.svg)](https://mypy-lang.org/)

[Installation](#installation) • [Quick Start](#quick-start) • [CLI](#cli) • [Roadmap](#roadmap) • [Plugins](#plugins)

</div>

---

## The Problem

That service has your data—but no API.

Your bank. Your 401k provider. Your insurance portal. Your HR system. They all have dashboards full of documents and data that belong to *you*, but no way to access them programmatically.

You're left with two options: click through the UI manually every time, or give up.

**BSC gives you a third option.**

## The Solution

BSC lets you log in once and script forever.

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                                                                             │
│   1. LOG IN                2. CACHE                 3. SCRIPT               │
│                                                                             │
│   ┌─────────────┐         ┌─────────────┐         ┌─────────────┐          │
│   │   Browser   │         │  Encrypted  │         │   Python    │          │
│   │   Session   │───────▶ │   Storage   │───────▶ │   Script    │          │
│   │             │         │             │         │             │          │
│   └─────────────┘         └─────────────┘         └─────────────┘          │
│                                                                             │
│   Log in manually         Session cached          Use the session          │
│   or with a plugin        with AES-128            to make requests         │
│                           encryption              like a real API          │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

Once your session is cached, you can:

- **Make HTTP requests** with your authenticated cookies
- **Reverse-engineer XHR calls** from browser dev tools
- **Build CLI tools** that feel like real APIs
- **Automate downloads** of documents and data
- **Keep sessions alive** with background daemons

## What You Can Build

With BSC as your foundation, you can turn any authenticated website into a terminal-based interface:

```bash
# Download your latest bank statements
mybank statements --month january --output ./statements/

# Export transactions to CSV
mybank transactions --start 2024-01-01 --format csv > transactions.csv

# Check your 401k balance
my401k balance
# → Total: $142,857.32 (+2.4% this month)

# Download insurance documents
myinsurance documents --type claims --year 2024
# → Downloaded 12 documents to ./claims/
```

These aren't real APIs—they're Python scripts using BSC to maintain authenticated sessions and make the same XHR calls the website makes. To anyone watching, it looks like magic. To you, it's just automation.

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

### 1. Cache a Session

```python
from bsc import BrowserSession, cache_session

# Create a stealth browser (avoids bot detection)
session = BrowserSession(headless=False, use_stealth=True)

# Navigate to login page
session.driver.get("https://app.example.com/login")

# Log in manually in the browser window...
# (or automate it with a plugin)

# Cache the authenticated session
cache_session(session, "example")
```

### 2. Use It Like an API

```python
from bsc import load_session_for_api

# Load your cached session (no browser needed)
api = load_session_for_api("example")

# Make authenticated requests
response = api.get("https://app.example.com/api/internal/documents")
documents = response.json()

for doc in documents:
    print(f"Downloading {doc['name']}...")
    content = api.get(doc['download_url']).content
    with open(doc['name'], 'wb') as f:
        f.write(content)
```

### 3. Keep It Alive

Sessions expire. BSC can keep them alive in the background:

```python
# Your keepalive handler pings the site periodically
# to prevent session timeout
```

## Features

| | Feature | Why It Matters |
|:--|:--|:--|
| 🥷 | **Stealth Mode** | Many sites block automation. BSC uses undetected-chromedriver and selenium-stealth to fly under the radar. |
| 🔒 | **Encrypted Storage** | Sessions contain sensitive auth tokens. BSC encrypts everything with AES-128 (Fernet). |
| ☁️ | **Cloud Storage** | Access your sessions from anywhere. Store in Supabase or S3 for multi-machine workflows. |
| 🔄 | **Keepalive Daemon** | Sessions expire. BSC can ping sites in the background to keep you logged in. |
| 🔌 | **Plugin System** | Automate login flows for specific sites. Community can share plugins (you maintain your own). |
| 🛠️ | **Beautiful CLI** | Manage sessions from the terminal with rich, colorful output. |

## CLI

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

Commands:
  list       List all cached sessions with status and metadata.
  show       Show detailed information about a cached session.
  clear      Remove cached session(s).
  export     Export session cookies to HTTPie format.
  config     Show current BSC configuration.
  plugins    List discovered plugins.
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
│ mybank      │ secure.mybank.com│  ● active  │      18 │ 2024-01-15 09:30 │
│ my401k      │ participant.401k │  ● active  │      12 │ 2024-01-14 14:22 │
│ insurance   │ portal.ins.com   │ ○ expired  │       8 │ 2024-01-01 11:00 │
└─────────────┴──────────────────┴────────────┴─────────┴──────────────────┘

3 session(s) cached
```

### Export to HTTPie

```
$ bsc export mybank

✓ Exported to: ~/.config/httpie/sessions/secure.mybank.com/mybank.json

Usage:
  http --session=mybank https://secure.mybank.com/api/accounts
```

## Configuration

| Variable | Default | Description |
|:---------|:--------|:------------|
| `BSC_STORAGE_BACKEND` | `local` | Storage: `local`, `supabase`, or `s3` |
| `BSC_CONFIG_DIR` | `~/.config/bsc` | Config and encryption key location |
| `BSC_SESSION_TTL_HOURS` | `720` | Session lifetime (30 days) |
| `BSC_LOG_LEVEL` | `INFO` | Logging verbosity |

## Roadmap

BSC is actively developed. Here's what's coming:

### 🧙 Plugin Auto-Generation Wizard

*Stop writing plugins by hand.*

A built-in tool that watches you log in and generates the plugin code automatically:

```bash
$ bsc wizard mybank
→ Opening browser to capture auth flow...
→ Log in normally (use dummy creds if you prefer)
→ Capturing cookies, headers, session validation...
→ Generated plugin: ~/.config/bsc/plugins/mybank.py

# Next time, login is automated:
$ bsc login mybank
```

### 📦 HAR Import

Import authentication flows from browser dev tools:

```bash
$ bsc import-har mybank-login.har --name mybank
```

### 📚 Example Plugins

Templates and examples for common auth patterns (form login, OAuth, SSO).

## Plugins

BSC is extensible via Python entry points. Write plugins to automate login for specific sites.

### Custom Login Plugin

```python
# my_plugins/mybank.py
from bsc.plugins import SitePlugin

class MyBankPlugin(SitePlugin):
    site_name = "mybank"
    login_url = "https://secure.mybank.com/login"

    def login(self, session, username, password):
        """Automate the login flow."""
        session.driver.get(self.login_url)
        session.driver.find_element("id", "username").send_keys(username)
        session.driver.find_element("id", "password").send_keys(password)
        session.driver.find_element("id", "submit").click()
        # Handle MFA if needed...
        return session
```

Register in `pyproject.toml`:

```toml
[project.entry-points."bsc.plugins"]
mybank = "my_plugins.mybank:MyBankPlugin"
```

### Custom Keepalive Handler

```python
# my_plugins/mybank_keepalive.py
class MyBankKeepalive:
    site_name = "mybank"

    def touch_session(self, session):
        """Ping the site to prevent session timeout."""
        r = session.get("https://secure.mybank.com/api/ping")
        return r.ok, None

    def validate_session(self, session):
        """Verify we're still logged in."""
        r = session.get("https://secure.mybank.com/api/me")
        return r.status_code == 200
```

## Security

### Your Data, Your Rules

BSC is for automating access to **your own accounts**. You're not scraping other people's data—you're building tools to access information that already belongs to you.

Some services may consider automation a ToS violation. Use your judgment.

### Encryption

- **Algorithm:** Fernet (AES-128-CBC + HMAC-SHA256)
- **Key storage:** `~/.config/bsc/.session_key` with `0600` permissions
- **Integrity:** SHA-256 checksum validated before deserializing

### ⚠️ Pickle Warning

BSC uses Python's `pickle` for serialization. Only load sessions you created.

### Best Practices

- Keep your encryption key secure
- Don't share session files
- Run BSC on trusted machines
- Use unique, strong passwords for automated accounts

## Development

```bash
git clone https://github.com/stavxyz/bsc.git
cd bsc
just setup    # Create venv and install deps
just check    # Run lint, typecheck, tests
just build    # Build for PyPI
```

## License

MIT License—see [LICENSE](LICENSE).

## Acknowledgments

- [requestium](https://github.com/tryolabs/requestium) – Selenium + Requests integration
- [undetected-chromedriver](https://github.com/ultrafunkamsterdam/undetected-chromedriver) – Anti-detection ChromeDriver
- [selenium-stealth](https://github.com/diprajpatra/selenium-stealth) – Stealth patches
- [cryptography](https://cryptography.io/) – Encryption primitives
- [rich](https://github.com/Textualize/rich) – Beautiful terminal output
- [typer](https://typer.tiangolo.com/) – CLI framework

---

<div align="center">
<sub>Built for automating your own data access.</sub>
</div>
