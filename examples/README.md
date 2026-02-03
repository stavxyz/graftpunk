# graftpunk Examples

Example plugins and templates for building your own graftpunk integrations.

## Quick Start

1. Symlink an example to your plugins directory:

   ```bash
   mkdir -p ~/.config/graftpunk/plugins
   ln -s $(pwd)/examples/plugins/httpbin.yaml ~/.config/graftpunk/plugins/
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
| `httpbin.yaml` | YAML | — | None / Header | Simplest example, includes env var auth commands |
| `quotes.py` | Python | Selenium | Declarative | Test site, any credentials work |
| `hackernews.py` | Python | NoDriver | Declarative | Real site, requires HN account |

### httpbin.yaml

The simplest possible plugin. No authentication needed for most commands. Includes examples of resource limits (`timeout`, `max_retries`), JMESPath filtering, URL parameters, and environment variable header injection.

```bash
ln -s $(pwd)/examples/plugins/httpbin.yaml ~/.config/graftpunk/plugins/
gp httpbin ip
gp httpbin uuid
gp httpbin status --code 418  # I'm a teapot!
gp httpbin delay 3            # With timeout and retry
```

Authenticated commands require the `HTTPBIN_AUTH` environment variable:

```bash
export HTTPBIN_AUTH="dXNlcjpwYXNz"  # base64("user:pass")
gp httpbin auth-whoami
gp httpbin auth-bearer
```

### quotes.py

Python plugin using **declarative login** with Selenium. Uses a test site that accepts any credentials — perfect for testing the login flow without a real account.

```bash
ln -s $(pwd)/examples/plugins/quotes.py ~/.config/graftpunk/plugins/

# Log in (auto-generated login command, opens browser)
gp quotes login

# Use the cached session
gp quotes list
gp quotes list --page 2
gp quotes random
```

### hackernews.py

Python plugin using **declarative login** with NoDriver (better anti-detection). Requires a real Hacker News account.

```bash
ln -s $(pwd)/examples/plugins/hackernews.py ~/.config/graftpunk/plugins/

# Log in (auto-generated login command, opens browser)
gp hn login

# Use the cached session
gp hn front
gp hn newest
gp hn saved
```

## YAML vs Python

**Use YAML when:**
- Simple HTTP calls to JSON APIs
- Header-based authentication (API tokens, bearer tokens)
- Declarative login is sufficient
- Quick prototyping

**Use Python when:**
- Custom login flow beyond what declarative login supports
- Response parsing (BeautifulSoup, etc.)
- Complex conditional logic
- Multiple requests per command

Both YAML and Python plugins support declarative login configuration.

## Creating Your Own

1. Copy a template from `templates/`:
   - `yaml_template.yaml` for YAML plugins
   - `python_template.py` for Python plugins

2. Save or symlink to `~/.config/graftpunk/plugins/`

3. Run `gp plugins` to verify discovery

## Plugin Discovery

graftpunk discovers plugins from three sources (in order):

1. **Entry points** — Python packages registered via `pyproject.toml`
2. **YAML files** — `~/.config/graftpunk/plugins/*.yaml` and `*.yml`
3. **Python files** — `~/.config/graftpunk/plugins/*.py` (files starting with `_` are skipped)

If two plugins have the same `site_name`, registration **fails with an error** showing both plugin sources. This prevents silent shadowing.

## Key Concepts

- **`site_name`** — The CLI subcommand group name (e.g., `gp hn ...`)
- **`session_name`** — The cached session key (can differ from `site_name`)
- **`CommandContext`** — Injected into all command handlers with the session, plugin name, and observability context
- **`api_version`** — Set to `1` for all new plugins
- **`--observe`** — Pass `--observe full` to any command to capture screenshots, network logs, and events

## See Also

- [How It Works](../docs/HOW_IT_WORKS.md) — Full architecture documentation
- [Main README](../README.md) — graftpunk overview
