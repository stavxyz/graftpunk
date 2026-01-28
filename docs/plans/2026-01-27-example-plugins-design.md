# Example Plugins and Templates Design

**Issue:** #5
**Date:** 2026-01-27
**Status:** Approved

## Summary

Create example plugins and templates to help users build their own graftpunk integrations. Also add Python plugin auto-discovery so `.py` files work the same as `.yaml` files.

## Directory Structure

```
examples/
├── README.md                    # Quick start + plugin development guide
├── plugins/
│   ├── httpbin.yaml             # YAML, no auth
│   ├── httpbin_auth.yaml        # YAML, header auth via env var
│   ├── quotes.py                # Python, Selenium, test site
│   └── hackernews.py            # Python, NoDriver, real site
└── templates/
    ├── yaml_template.yaml       # Blank YAML template
    └── python_template.py       # Blank Python template
```

## Example Matrix

| Plugin | Format | Backend | Auth Type | Site |
|--------|--------|---------|-----------|------|
| `httpbin.yaml` | YAML | — | None | httpbin.org |
| `httpbin_auth.yaml` | YAML | — | Header | httpbin.org |
| `quotes.py` | Python | Selenium | Form login | quotes.toscrape.com |
| `hackernews.py` | Python | NoDriver | Form login | news.ycombinator.com |

## YAML Plugin Examples

### httpbin.yaml (no auth)

```yaml
site_name: httpbin
session_name: ""              # Empty = no session required
help: "httpbin.org test commands"
base_url: "https://httpbin.org"

commands:
  ip:
    help: "Get your public IP"
    method: GET
    url: "/ip"

  headers:
    help: "Echo request headers"
    method: GET
    url: "/headers"

  get:
    help: "Test GET with query params"
    method: GET
    url: "/get"

  status:
    help: "Return specific HTTP status"
    method: GET
    url: "/status/{code}"
    params:
      - name: code
        type: int
        required: true
        help: "HTTP status code to return"
```

### httpbin_auth.yaml (with header)

```yaml
site_name: httpbin-auth
session_name: ""
help: "httpbin.org with auth header"
base_url: "https://httpbin.org"

headers:
  Authorization: "Basic ${HTTPBIN_AUTH}"

commands:
  whoami:
    help: "Check auth header is sent"
    method: GET
    url: "/headers"
    jmespath: "headers.Authorization"
```

## Python Plugin Examples

### quotes.py (Selenium, test site)

```python
"""Quotes to Scrape plugin - Selenium example.

Demonstrates form login with Selenium backend.
Site accepts any username/password.
"""

from graftpunk import BrowserSession, cache_session, load_session_for_api
from graftpunk.plugins import SitePlugin, command


class QuotesPlugin(SitePlugin):
    site_name = "quotes"
    session_name = "quotes"
    help_text = "Quotes to Scrape commands"

    base_url = "https://quotes.toscrape.com"

    def login(self, username: str = "testuser", password: str = "testpass") -> bool:
        """Log in and cache session. Any credentials work."""
        session = BrowserSession(backend="selenium", headless=False)
        session.driver.get(f"{self.base_url}/login")

        session.driver.find_element("id", "username").send_keys(username)
        session.driver.find_element("id", "password").send_keys(password)
        session.driver.find_element("css selector", "input[type='submit']").click()

        # Verify login succeeded (redirects to home with Logout link)
        session.driver.find_element("css selector", "a[href='/logout']")

        cache_session(session, self.session_name)
        session.quit()
        return True

    @command(help="Get quotes from a page")
    def list(self, session, page: int = 1):
        """Fetch quotes from a page."""
        response = session.get(f"{self.base_url}/page/{page}/")
        # Parse with BeautifulSoup in real usage
        return {"url": response.url, "status": response.status_code}
```

### hackernews.py (NoDriver, real site)

```python
"""Hacker News plugin - NoDriver example.

Demonstrates form login with NoDriver backend on a real site.
Requires actual HN credentials.
"""

from graftpunk import BrowserSession, cache_session
from graftpunk.plugins import SitePlugin, command


class HackerNewsPlugin(SitePlugin):
    site_name = "hn"
    session_name = "hackernews"
    help_text = "Hacker News commands"

    base_url = "https://news.ycombinator.com"

    async def login(self, username: str, password: str) -> bool:
        """Log in to Hacker News and cache session."""
        session = BrowserSession(backend="nodriver", headless=False)

        await session.driver.get(f"{self.base_url}/login")

        # NoDriver uses async element finding
        acct_field = await session.driver.find("input[name='acct']")
        await acct_field.send_keys(username)

        pw_field = await session.driver.find("input[name='pw']")
        await pw_field.send_keys(password)

        submit = await session.driver.find("input[value='login']")
        await submit.click()

        # Wait for redirect to logged-in state
        await session.driver.find("a[href='logout']", timeout=10)

        cache_session(session, self.session_name)
        await session.quit()
        return True

    @command(help="Get front page stories")
    def front(self, session, page: int = 1):
        """Fetch front page stories."""
        response = session.get(f"{self.base_url}/news?p={page}")
        return {"url": response.url, "status": response.status_code}

    @command(help="Get saved stories (requires login)")
    def saved(self, session):
        """Fetch your saved stories."""
        response = session.get(f"{self.base_url}/saved")
        return {"url": response.url, "status": response.status_code}
```

## Templates

### yaml_template.yaml

```yaml
# Plugin template - copy and customize
# Save or symlink to: ~/.config/graftpunk/plugins/<name>.yaml

site_name: mysite                    # CLI command group: gp mysite <cmd>
session_name: mysite                 # Cached session to load (or "" for none)
help: "Description of your plugin"
base_url: "https://example.com"      # Optional, prepended to command URLs

# Optional headers applied to all requests
# headers:
#   Authorization: "Bearer ${MY_TOKEN}"

commands:
  example:
    help: "Description of this command"
    method: GET                      # GET, POST, PUT, DELETE
    url: "/api/endpoint"             # Appended to base_url
    # jmespath: "data[0].name"       # Optional: extract from JSON response
    # params:                        # Optional: command parameters
    #   - name: id
    #     type: int                  # str, int, float, bool
    #     required: true
    #     help: "Resource ID"
```

### python_template.py

```python
"""Plugin template - copy and customize.

Save or symlink to: ~/.config/graftpunk/plugins/<name>.py
"""

from graftpunk import BrowserSession, cache_session
from graftpunk.plugins import SitePlugin, command


class MySitePlugin(SitePlugin):
    site_name = "mysite"           # CLI command group: gp mysite <cmd>
    session_name = "mysite"        # Cached session to load
    help_text = "Description of your plugin"

    base_url = "https://example.com"

    def login(self, username: str, password: str) -> bool:
        """Log in and cache session."""
        session = BrowserSession(backend="selenium", headless=False)
        session.driver.get(f"{self.base_url}/login")

        # TODO: Fill login form
        # session.driver.find_element("id", "username").send_keys(username)
        # session.driver.find_element("id", "password").send_keys(password)
        # session.driver.find_element("id", "submit").click()

        cache_session(session, self.session_name)
        session.quit()
        return True

    @command(help="Example command")
    def example(self, session, param: str = "default"):
        """Fetch something from the API."""
        response = session.get(f"{self.base_url}/api/endpoint")
        return response.json()
```

## New Feature: Python Plugin Auto-Discovery

### Current State

- YAML plugins: Auto-discovered from `~/.config/graftpunk/plugins/*.yaml`
- Python plugins: Require entry point registration in `pyproject.toml`

### New Behavior

Add auto-discovery for `.py` files in `~/.config/graftpunk/plugins/`:

1. Scan `~/.config/graftpunk/plugins/*.py`
2. Skip files starting with `_` (e.g., `__init__.py`, `_helpers.py`)
3. Import each file as a module using `importlib`
4. Find classes that subclass `SitePlugin` (or implement `CLIPluginProtocol`)
5. Register them alongside YAML plugins and entry-point plugins
6. Handle import errors gracefully (log warning, continue with other plugins)
7. Entry-point plugins take precedence if same `site_name` exists

### Implementation

New file: `src/graftpunk/plugins/python_loader.py`

Mirrors the structure of `yaml_loader.py`:
- `PythonDiscoveryError` dataclass
- `PythonDiscoveryResult` dataclass
- `discover_python_plugins()` function

Update: `src/graftpunk/cli/plugin_commands.py`
- Import and call `discover_python_plugins()`
- Merge results with YAML and entry-point plugins

## README.md Content

```markdown
# graftpunk Examples

Example plugins and templates for building your own graftpunk integrations.

## Quick Start

1. Symlink an example to your plugins directory:
   ```bash
   mkdir -p ~/.config/graftpunk/plugins
   ln -s $(pwd)/plugins/httpbin.yaml ~/.config/graftpunk/plugins/
   ```

2. Run it:
   ```bash
   gp httpbin ip
   gp httpbin headers
   gp httpbin status --code 418
   ```

## Examples

| Plugin | Type | Backend | Auth | Description |
|--------|------|---------|------|-------------|
| `httpbin.yaml` | YAML | — | None | Simplest example, no auth needed |
| `httpbin_auth.yaml` | YAML | — | Header | Environment variable injection |
| `quotes.py` | Python | Selenium | Form | Test site, any credentials work |
| `hackernews.py` | Python | NoDriver | Form | Real site, requires HN account |

## YAML vs Python

**Use YAML when:**
- Simple GET/POST to JSON APIs
- No complex logic needed
- Quick prototyping

**Use Python when:**
- Login automation required
- Response parsing with BeautifulSoup
- Complex conditional logic
- Multiple requests per command

## Creating Your Own

1. Copy a template from `templates/`
2. Save or symlink to `~/.config/graftpunk/plugins/`
   - YAML: `mysite.yaml`
   - Python: `mysite.py`
3. Run `gp plugins` to verify discovery

See the main graftpunk README for full documentation.
```

## User Workflow

After implementation:

```bash
# Symlink example
ln -s $(pwd)/examples/plugins/httpbin.yaml ~/.config/graftpunk/plugins/

# Use immediately
gp httpbin ip
gp httpbin headers
gp httpbin status --code 418

# For Python examples
ln -s $(pwd)/examples/plugins/quotes.py ~/.config/graftpunk/plugins/
gp quotes list --page 1
```

## Implementation Order

1. Create `examples/` directory structure
2. Add `httpbin.yaml` and `httpbin_auth.yaml`
3. Implement `python_loader.py` (auto-discovery)
4. Integrate Python discovery into `plugin_commands.py`
5. Add `quotes.py` and `hackernews.py`
6. Add templates
7. Write `examples/README.md`
8. Test end-to-end

## Acceptance Criteria

- [ ] `gp httpbin ip` works after symlinking
- [ ] `gp httpbin status --code 418` returns a teapot
- [ ] `gp quotes list` works after symlinking `.py` file
- [ ] `gp plugins` shows all discovered plugins
- [ ] Examples have comprehensive inline comments
- [ ] README explains each example clearly
