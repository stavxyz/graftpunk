<div align="center">

# 🔌 graftpunk

**Turn any website into an API.**

*Graft scriptable access onto authenticated web services.*

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Code style: ruff](https://img.shields.io/badge/code%20style-ruff-000000.svg)](https://github.com/astral-sh/ruff)
[![Typed](https://img.shields.io/badge/typed-ty-blue.svg)](https://github.com/astral-sh/ty)

[Installation](#installation) • [Quick Start](#quick-start) • [CLI](#cli) • [Roadmap](#roadmap) • [Plugins](#plugins)

</div>

---

## The Problem

That service has your data—but no API.

Your bank. Your 401k provider. Your insurance portal. Your HR system. They all have dashboards full of documents and data that belong to *you*, but no way to access them programmatically.

You're left with two options: click through the UI manually every time, or give up.

**graftpunk gives you a third option.**

## The Solution

Log in once, script forever.

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

With graftpunk as your foundation, you can turn any authenticated website into a terminal-based interface:

```bash
# Download your latest bank statements
gp mybank statements --month january --output ./statements/

# Export transactions to CSV
gp mybank transactions --start 2024-01-01 --format csv > transactions.csv

# Check your 401k balance
gp my401k balance
# → Total: $142,857.32 (+2.4% this month)

# Download insurance documents
gp insurance documents --type claims --year 2024
# → Downloaded 12 documents to ./claims/
```

These aren't real APIs—they're commands defined in graftpunk plugins that make the same XHR calls the website makes. To anyone watching, it looks like magic. To you, it's just automation.

## Installation

```bash
pip install graftpunk
```

**With cloud storage:**

```bash
pip install graftpunk[supabase]   # Supabase backend
pip install graftpunk[s3]         # AWS S3 backend
pip install graftpunk[nodriver]   # NoDriver backend (better anti-detection)
pip install graftpunk[standard]   # Recommended: NoDriver + stealth
pip install graftpunk[all]        # Everything
```

## Quick Start

### 1. Cache a Session

```python
from graftpunk import BrowserSession, cache_session

# Create a stealth browser (avoids bot detection)
# Options: backend="selenium" (default), "nodriver" (better anti-detection)
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
from graftpunk import load_session_for_api

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

Sessions expire. graftpunk can keep them alive in the background:

```python
# Your keepalive handler pings the site periodically
# to prevent session timeout
```

## Features

| | Feature | Why It Matters |
|:--|:--|:--|
| 🥷 | **Stealth Mode** | Many sites block automation. graftpunk supports multiple backends: Selenium with undetected-chromedriver, or NoDriver for CDP-direct automation without WebDriver detection. |
| 🔒 | **Encrypted Storage** | Sessions contain sensitive auth tokens. graftpunk encrypts everything with AES-128 (Fernet). |
| ☁️ | **Cloud Storage** | Access your sessions from anywhere. Store in Supabase or S3 for multi-machine workflows. |
| 🔄 | **Keepalive Daemon** | Sessions expire. graftpunk can ping sites in the background to keep you logged in. |
| 🔌 | **Plugin System** | Define commands for reverse-engineered APIs. Python for complex logic, YAML for simple calls. |
| 🛠️ | **Beautiful CLI** | Manage sessions from the terminal with rich, colorful output. |

## CLI

```
$ gp --help

 🔌 graftpunk - turn any website into an API

 Graft scriptable access onto authenticated web services.
 Log in once, script forever.

 Quick start:
   gp list              Show all cached sessions
   gp show <name>       View session details
   gp clear <name>      Remove a session
   gp config            Show current configuration

Commands:
  list        List all cached sessions with status and metadata.
  show        Show detailed information about a cached session.
  clear       Remove cached session(s).
  export      Export session cookies to HTTPie format.
  import-har  Import HAR file and generate a plugin.
  config      Show current graftpunk configuration.
  plugins     List discovered plugins.
  version     Show graftpunk version and installation info.
  keepalive   Manage the session keepalive daemon.
```

### List Sessions

```
$ gp list

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
$ gp export mybank

✓ Exported to: ~/.config/httpie/sessions/secure.mybank.com/mybank.json

Usage:
  http --session=mybank https://secure.mybank.com/api/accounts
```

### Import from HAR

Generate plugins from browser dev tools network captures:

```
$ gp import-har auth-flow.har --name mybank

Parsing HAR file: auth-flow.har
Found 127 HTTP requests

Site: mybank (secure.mybank.com)

Auth Flow Detected (3 steps):
  1. GET  /login (form page)
  2. POST /auth/login (credentials submitted)
  3. GET  /dashboard (authenticated, 2 cookies set)

Session Cookies: sessionId, authToken

API Endpoints (5 discovered):
  GET  /api/accounts
  GET  /api/transactions
  POST /api/transfer

Generated plugin: ~/.config/graftpunk/plugins/mybank.py
```

Options:
- `--format python|yaml` - Output format (default: python)
- `--output PATH` - Custom output path
- `--dry-run` - Preview without writing
- `--no-discover-api` - Skip API endpoint discovery

## Configuration

| Variable | Default | Description |
|:---------|:--------|:------------|
| `GRAFTPUNK_STORAGE_BACKEND` | `local` | Storage: `local`, `supabase`, or `s3` |
| `GRAFTPUNK_CONFIG_DIR` | `~/.config/graftpunk` | Config and encryption key location |
| `GRAFTPUNK_SESSION_TTL_HOURS` | `720` | Session lifetime (30 days) |
| `GRAFTPUNK_LOG_LEVEL` | `INFO` | Logging verbosity |

## Browser Backends

graftpunk supports multiple browser automation backends:

| Backend | Install | Best For |
|---------|---------|----------|
| `selenium` | Default | Simple sites, backward compatibility |
| `nodriver` | `pip install graftpunk[nodriver]` | Enterprise sites, better anti-detection |

```python
from graftpunk import BrowserSession, get_backend, list_backends

# See available backends
print(list_backends())  # ['legacy', 'nodriver', 'selenium']

# Use BrowserSession with explicit backend
session = BrowserSession(backend="nodriver", headless=False)

# Or use backends directly
from graftpunk import get_backend
backend = get_backend("nodriver", headless=False)
with backend:
    backend.navigate("https://example.com")
    cookies = backend.get_cookies()
```

**Why NoDriver?** NoDriver uses Chrome DevTools Protocol (CDP) directly without the WebDriver binary, eliminating a common detection vector used by anti-bot systems.

## Roadmap

graftpunk is actively developed. Here's what's coming:

### 🧙 Plugin Auto-Generation Wizard

*Stop writing plugins by hand.*

A built-in tool that watches you log in and generates the plugin code automatically:

```bash
$ gp wizard mybank
→ Opening browser to capture auth flow...
→ Log in normally (use dummy creds if you prefer)
→ Capturing cookies, headers, session validation...
→ Generated plugin: ~/.config/graftpunk/plugins/mybank.py

# Next time, login is automated:
$ gp login mybank
```

### 📚 Example Plugins

Templates and examples for common auth patterns (form login, OAuth, SSO).

## Plugins

graftpunk is extensible via Python entry points or YAML configuration.

### Python Plugin (Complex Logic)

```python
# my_plugins/mybank.py
from graftpunk.plugins import SitePlugin, command

class MyBankPlugin(SitePlugin):
    site_name = "mybank"
    session_name = "mybank"

    @command(help="List all accounts")
    def accounts(self, session):
        return session.get("https://mybank.com/api/accounts").json()

    @command(help="Get statements for a month")
    def statements(self, session, month: str, year: int = 2024):
        url = f"https://mybank.com/api/statements/{year}/{month}"
        return session.get(url).json()
```

Register in `pyproject.toml`:

```toml
[project.entry-points."graftpunk.cli_plugins"]
mybank = "my_plugins.mybank:MyBankPlugin"
```

### YAML Plugin (Simple Calls)

For straightforward GET/POST calls, no Python needed:

```yaml
# ~/.config/graftpunk/plugins/mybank.yaml
site_name: mybank
session_name: mybank
help: "Commands for MyBank"

commands:
  accounts:
    help: "List all accounts"
    method: GET
    url: "https://mybank.com/api/accounts"

  statements:
    help: "Get statements for a month"
    method: GET
    url: "https://mybank.com/api/statements/{year}/{month}"
    params:
      - name: month
        required: true
        help: "Month name"
      - name: year
        default: 2024
        help: "Year"
```

Then use directly:

```bash
gp mybank accounts
gp mybank statements --month january --year 2024
```

## Security

### Your Data, Your Rules

graftpunk is for automating access to **your own accounts**. You're not scraping other people's data—you're building tools to access information that already belongs to you.

Some services may consider automation a ToS violation. Use your judgment.

### Encryption

- **Algorithm:** Fernet (AES-128-CBC + HMAC-SHA256)
- **Key storage:** `~/.config/graftpunk/.session_key` with `0600` permissions
- **Integrity:** SHA-256 checksum validated before deserializing

### ⚠️ Pickle Warning

graftpunk uses Python's `pickle` for serialization. Only load sessions you created.

### Best Practices

- Keep your encryption key secure
- Don't share session files
- Run graftpunk on trusted machines
- Use unique, strong passwords for automated accounts

## Development

```bash
git clone https://github.com/stavxyz/graftpunk.git
cd graftpunk
just setup    # Install deps with uv
just check    # Run lint, typecheck, tests
just build    # Build for PyPI
```

Requires [uv](https://docs.astral.sh/uv/) for development.

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
