<div align="center">

# 🔌 graftpunk

**Turn any website into an API.**

*Graft scriptable access onto authenticated web services.*

[![PyPI](https://img.shields.io/pypi/v/graftpunk.svg)](https://pypi.org/project/graftpunk/)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Code style: ruff](https://img.shields.io/badge/code%20style-ruff-000000.svg)](https://github.com/astral-sh/ruff)
[![Typed](https://img.shields.io/badge/typed-ty-blue.svg)](https://github.com/astral-sh/ty)

[Installation](#installation) • [Quick Start](#quick-start) • [Plugins](#plugins) • [CLI Reference](#cli-reference) • [Examples](examples/README.md) • [Architecture](docs/HOW_IT_WORKS.md)

</div>

---

## The Problem

That service has your data—but no API.

Your ISP account. Your kid's school portal. Your local library. That niche e-commerce site you order from. Your medical records. They all have data that belongs to *you*, locked behind a login page with no API in sight.

You're left with two options: click through the UI manually every time, or give up.

**graftpunk gives you a third option.**

## The Solution

Log in once, script forever.

```
  1. LOG IN              2. CACHE               3. SCRIPT

  +-------------+       +-------------+       +-------------+
  |   Browser   |       |  Encrypted  |       |   Python    |
  |   Session   |------>|   Storage   |------>|   Script    |
  |             |       |             |       |             |
  +-------------+       +-------------+       +-------------+

  Log in manually       Session cached        Use the session
  or declaratively      with AES-128          with real browser
  via plugin config     encryption            headers replayed
```

Once your session is cached, you can:

- **Make HTTP requests** with your authenticated cookies *and* real browser headers
- **Reverse-engineer XHR calls** from browser dev tools
- **Build CLI tools** that feel like real APIs
- **Automate downloads** of documents and data
- **Keep sessions alive** with background daemons
- **Capture network traffic** for debugging and auditing

## What You Can Build

With graftpunk as your foundation, you can turn any authenticated website into a terminal-based interface:

```bash
# Pull your kid's grades and assignments
gp schoolportal grades --student emma --format table

# Download your medical lab results
gp mychart labs --after 2024-06-01 --output ./results/

# Export your energy usage data
gp utility usage --months 12 --format csv > energy.csv

# Scrape your property tax history
gp county assessor --parcel 12345 --format json

# Make ad-hoc requests with cached session cookies + browser headers
gp http get -s mychart https://mychart.example.com/api/appointments
```

These aren't real APIs—they're commands defined in graftpunk plugins that replay the same XHR calls the website makes. To the server, it looks like a browser. To you, it's just automation.

## Installation

```bash
pip install graftpunk
```

**With cloud storage:**

```bash
pip install graftpunk[supabase]   # Supabase backend
pip install graftpunk[s3]         # AWS S3 backend
pip install graftpunk[all]        # Everything
```

## Quick Start

### 1. Cache a Session

The fastest way is with a plugin. Here's the httpbin example (no auth needed):

```bash
# Drop a YAML plugin into your plugins directory
mkdir -p ~/.config/graftpunk/plugins
cp examples/plugins/httpbin.yaml ~/.config/graftpunk/plugins/

# Use it immediately
gp httpbin ip
gp httpbin headers
gp httpbin status --code 418  # I'm a teapot!
```

For sites that require authentication, plugins can define declarative login:

```bash
# Log in via auto-generated command (opens browser, fills form, caches session)
gp quotes login

# Use the cached session for API calls
gp quotes list
gp quotes random
```

### 2. Use It Programmatically

```python
from graftpunk import GraftpunkClient

# Use plugin commands from Python — same session, tokens, and retries as the CLI
with GraftpunkClient("mybank") as client:
    accounts = client.accounts()
    statements = client.statements(month="january", year=2024)

    # Grouped commands use nested attribute access
    detail = client.accounts.detail(id=42)
```

For lower-level access without plugins, load a session directly:

```python
from graftpunk import load_session_for_api

# Returns a GraftpunkSession with browser headers pre-loaded
api = load_session_for_api("mysite")
response = api.get("https://app.example.com/api/internal/documents")
```

### 3. Keep It Alive

Sessions expire. graftpunk can keep them alive in the background with the keepalive daemon.

## Features

| | Feature | Why It Matters |
|:--|:--|:--|
| 🥷 | **Stealth Mode** | Multiple backends: Selenium with undetected-chromedriver, or NoDriver for CDP-direct automation without WebDriver detection. Bot-detection cookies (Akamai, etc.) are automatically filtered during cookie injection to prevent WAF rejection. |
| 🔒 | **Encrypted Storage** | Sessions encrypted with AES-128 (Fernet). Local by default, optional cloud storage. |
| 🔑 | **Declarative Login** | Define login flows with CSS selectors. graftpunk opens the browser, fills the form, and caches the session. Works in both Python and YAML plugins. |
| 🌐 | **Browser Header Replay** | Captures real browser headers during login and replays them in API calls. Requests look like they came from Chrome, not Python. |
| 🔌 | **Plugin System** | Full command framework with `CommandContext`, resource limits, output formatting, and auto-generated CLI. Python for complex logic, YAML for simple calls. |
| 🛡️ | **Token & CSRF Support** | Declarative token extraction from cookies, headers, or page content. EAFP injection with automatic 403 retry. Tokens cached through session serialization. |
| 📡 | **Observability** | Capture screenshots, HAR files, console logs, and network traffic. Interactive mode lets you browse manually while recording. |
| 🔄 | **Keepalive Daemon** | Background daemon pings sites periodically to prevent session timeout. |
| 🛠️ | **Ad-hoc HTTP** | `gp http get -s <session> <url>` — make one-off authenticated requests without writing a plugin. |
| 🎨 | **Beautiful CLI** | Rich terminal output with spinners, tables, and color. `--format json\|table\|raw` on all commands. |

## Plugins

graftpunk is extensible via Python classes or YAML configuration. Both support declarative login, resource limits, and output formatting.

### YAML Plugin (Simple REST Calls)

For straightforward HTTP calls, no Python needed:

```yaml
# ~/.config/graftpunk/plugins/mybank.yaml
site_name: mybank
base_url: "https://secure.mybank.com"

login:
  url: /login
  fields:
    username: "input#email"
    password: "input#password"
  submit: "button[type=submit]"

commands:
  accounts:
    help: "List all accounts"
    method: GET
    url: "/api/accounts"
    jmespath: "accounts[].{id: id, name: name, balance: balance}"

  statements:
    help: "Get statements for a month"
    method: GET
    url: "/api/statements"
    params:
      - name: month
        required: true
        help: "Month name"
      - name: year
        type: int
        default: 2024
    timeout: 30
    max_retries: 2
```

### Python Plugin (Complex Logic)

```python
from graftpunk.plugins import CommandContext, LoginConfig, SitePlugin, command

class MyBankPlugin(SitePlugin):
    site_name = "mybank"
    base_url = "https://secure.mybank.com"
    backend = "nodriver"  # or "selenium"
    api_version = 1

    login_config = LoginConfig(
        url="/login",
        fields={"username": "input#email", "password": "input#password"},
        submit="button[type=submit]",
        success=".dashboard",
    )

    @command(help="List all accounts")
    def accounts(self, ctx: CommandContext):
        return ctx.session.get(f"{self.base_url}/api/accounts").json()

    @command(help="Get statements for a month")
    def statements(self, ctx: CommandContext, month: str, year: int = 2024):
        url = f"{self.base_url}/api/statements/{year}/{month}"
        return ctx.session.get(url).json()
```

### Using Plugins

```bash
# Login (auto-generated from declarative config)
gp mybank login

# Run commands
gp mybank accounts
gp mybank statements --month january --year 2024 --format table

# List all discovered plugins
gp plugins
```

### Plugin Discovery

Plugins are discovered from three sources:

1. **Entry points** — Python packages registered via `pyproject.toml`
2. **YAML files** — `~/.config/graftpunk/plugins/*.yaml` and `*.yml`
3. **Python files** — `~/.config/graftpunk/plugins/*.py`

If two plugins share the same `site_name`, registration fails with an error showing both sources. No silent shadowing.

See [examples/](examples/README.md) for working plugins and templates.

## CLI Reference

```
$ gp --help

 🔌 graftpunk - turn any website into an API

Commands:
  session     Manage encrypted browser sessions
  http        Make ad-hoc HTTP requests with cached session cookies
  observe     Capture and view browser observability data
  plugins     List discovered plugins
  import-har  Import HAR file and generate a plugin
  config      Show current configuration
  keepalive   Manage the session keepalive daemon
  version     Show version info
```

### Session Management

```bash
gp session list              # List all cached sessions
gp session show <name>       # Session metadata (domain, cookies, expiry)
gp session clear <name>      # Remove a session (or --all)
gp session export <name>     # Export cookies to HTTPie session format
gp session use <name>        # Set active session for subsequent commands
gp session unset             # Clear active session
```

### Ad-hoc HTTP Requests

Make authenticated requests using cached sessions without writing a plugin:

```bash
gp http get -s mybank https://secure.mybank.com/api/accounts
gp http post -s mybank https://secure.mybank.com/api/transfer --data '{"amount": 100}'
```

Use `--profile` to set browser header profiles (built-in or plugin-defined):

```bash
gp http get -s mybank --profile xhr https://secure.mybank.com/api/status
gp http get -s mybank --profile api https://secure.mybank.com/v2/data  # custom plugin profile
```

Supports all HTTP methods: `get`, `post`, `put`, `patch`, `delete`, `head`, `options`.

### Observability

Capture browser activity for debugging:

```bash
# Open authenticated browser and capture network traffic
gp observe -s mybank go https://secure.mybank.com/dashboard

# Interactive mode — browse manually, Ctrl+C to save
gp observe -s mybank interactive https://secure.mybank.com/dashboard

# Or use the --interactive flag on observe go
gp observe -s mybank go --interactive https://secure.mybank.com/dashboard

# View captured data
gp observe list
gp observe show mybank
gp observe clean mybank
```

Interactive mode opens an authenticated browser and records all network traffic (including response bodies) while you click around. Press Ctrl+C to stop — HAR files, screenshots, page source, and console logs are saved automatically.

Pass `--observe full` to any command to capture screenshots, HAR files, and console logs.

### HAR Import

Generate plugins from browser network captures:

```bash
gp import-har auth-flow.har --name mybank
```

## Configuration

| Variable | Default | Description |
|:---------|:--------|:------------|
| `GRAFTPUNK_STORAGE_BACKEND` | `local` | Storage: `local`, `supabase`, or `s3` |
| `GRAFTPUNK_CONFIG_DIR` | `~/.config/graftpunk` | Config and encryption key location |
| `GRAFTPUNK_SESSION_TTL_HOURS` | `720` | Session lifetime (30 days) |
| `GRAFTPUNK_LOG_LEVEL` | `WARNING` | Logging verbosity |
| `GRAFTPUNK_LOG_FORMAT` | `console` | Log format: `console` or `json` |

CLI flags: `-v` (info), `-vv` (debug), `--log-format json`, `--observe full`, `--network-debug` (wire-level HTTP tracing).

## Browser Backends

graftpunk supports two browser automation backends (both included by default):

| Backend | Best For |
|---------|----------|
| `selenium` | Simple sites, backward compatibility |
| `nodriver` | Enterprise sites, better anti-detection |

**Why NoDriver?** NoDriver uses Chrome DevTools Protocol (CDP) directly without the WebDriver binary, eliminating a common detection vector used by anti-bot systems.

**Bot-detection cookie filtering:** When injecting session cookies into a nodriver browser (for observe mode, token extraction, etc.), graftpunk automatically skips known WAF tracking cookies (Akamai `bm_*`, `ak_bmsc`, `_abck`). These cookies carry stale bot-classification state that causes WAFs to reject the browser with `ERR_HTTP2_PROTOCOL_ERROR`. Disable with `skip_bot_cookies=False` if needed.

```python
from graftpunk import BrowserSession

# Use BrowserSession with explicit backend
session = BrowserSession(backend="nodriver", headless=False)
```

## Security

### Your Data, Your Rules

graftpunk is for automating access to **your own accounts**. You're not scraping other people's data—you're building tools to access information that already belongs to you.

Some services may consider automation a ToS violation. Use your judgment.

### Encryption

- **Algorithm:** Fernet (AES-128-CBC + HMAC-SHA256)
- **Key storage:** `~/.config/graftpunk/.session_key` with `0600` permissions
- **Integrity:** SHA-256 checksum validated before deserializing

### Best Practices

- Keep your encryption key secure
- Don't share session files
- Run graftpunk on trusted machines
- Use unique, strong passwords for automated accounts

**Pickle warning:** graftpunk uses Python's `pickle` for serialization. Only load sessions you created.

## Development

```bash
git clone https://github.com/stavxyz/graftpunk.git
cd graftpunk
just setup    # Install deps with uv
just check    # Run lint, typecheck, tests
just build    # Build for PyPI
```

Requires [uv](https://docs.astral.sh/uv/) for development. See [CONTRIBUTING.md](CONTRIBUTING.md) for full guidelines.

## License

MIT License—see [LICENSE](LICENSE).

## Acknowledgments

- [requestium](https://github.com/tryolabs/requestium) – Selenium + Requests integration
- [undetected-chromedriver](https://github.com/ultrafunkamsterdam/undetected-chromedriver) – Anti-detection ChromeDriver
- [nodriver](https://github.com/ultrafunkamsterdam/nodriver) – CDP-direct browser automation
- [cryptography](https://cryptography.io/) – Encryption primitives
- [rich](https://github.com/Textualize/rich) – Beautiful terminal output
- [typer](https://typer.tiangolo.com/) – CLI framework

---

<div align="center">
<sub>Built for automating your own data access.</sub>
</div>
