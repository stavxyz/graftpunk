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
| `httpbin.yaml` | YAML | — | None | Simplest example, no auth needed |
| `httpbin_auth.yaml` | YAML | — | Header | Environment variable injection |
| `quotes.py` | Python | Selenium | Form | Test site, any credentials work |
| `hackernews.py` | Python | NoDriver | Form | Real site, requires HN account |

### httpbin.yaml

The simplest possible plugin. No authentication, no session caching—just HTTP calls.

```bash
ln -s $(pwd)/examples/plugins/httpbin.yaml ~/.config/graftpunk/plugins/
gp httpbin ip
gp httpbin uuid
gp httpbin status --code 418  # I'm a teapot!
```

### httpbin_auth.yaml

Demonstrates header injection via environment variables.

```bash
ln -s $(pwd)/examples/plugins/httpbin_auth.yaml ~/.config/graftpunk/plugins/
export HTTPBIN_AUTH="dXNlcjpwYXNz"  # base64("user:pass")
gp httpbin-auth whoami
```

### quotes.py

Python plugin using Selenium for form login. Uses a test site that accepts any credentials.

```bash
ln -s $(pwd)/examples/plugins/quotes.py ~/.config/graftpunk/plugins/

# Log in (opens browser)
python -c "
from graftpunk.plugins.python_loader import discover_python_plugins
plugin = next(p for p in discover_python_plugins().plugins if p.site_name == 'quotes')
plugin.login()
"

# Use the cached session
gp quotes list
gp quotes list --page 2
```

### hackernews.py

Python plugin using NoDriver (better anti-detection) for a real site.

```bash
ln -s $(pwd)/examples/plugins/hackernews.py ~/.config/graftpunk/plugins/

# Log in (opens browser, requires real HN account)
python -c "
import asyncio
from graftpunk.plugins.python_loader import discover_python_plugins
plugin = next(p for p in discover_python_plugins().plugins if p.site_name == 'hn')
asyncio.run(plugin.login('your_username', 'your_password'))
"

# Use the cached session
gp hn front
gp hn newest
gp hn saved
```

## YAML vs Python

**Use YAML when:**
- Simple GET/POST to JSON APIs
- No login automation needed
- Quick prototyping

**Use Python when:**
- Login automation required
- Response parsing (BeautifulSoup, etc.)
- Complex conditional logic
- Multiple requests per command

## Creating Your Own

1. Copy a template from `templates/`:
   - `yaml_template.yaml` for YAML plugins
   - `python_template.py` for Python plugins

2. Save or symlink to `~/.config/graftpunk/plugins/`

3. Run `gp plugins` to verify discovery

## Plugin Discovery

graftpunk discovers plugins from three sources (in order):

1. **Entry points** — Python packages registered via `pyproject.toml`
2. **YAML files** — `~/.config/graftpunk/plugins/*.yaml`
3. **Python files** — `~/.config/graftpunk/plugins/*.py`

If two plugins have the same `site_name`, the first one wins.

## See Also

- [Main README](../README.md) — Full graftpunk documentation
- [Plugin System](../README.md#plugins) — Plugin architecture details
