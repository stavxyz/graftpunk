# Contributing to graftpunk

Thank you for your interest in contributing to graftpunk! This document provides guidelines and instructions for contributing.

## Code of Conduct

Be respectful, inclusive, and constructive. We're all here to build something useful.

## Getting Started

### Prerequisites

- Python 3.11 or later
- Chrome browser (for running browser-based tests)
- Git
- [uv](https://docs.astral.sh/uv/) (fast Python package manager)

### Development Setup

1. **Clone the repository**

   ```bash
   git clone https://github.com/stavxyz/graftpunk.git
   cd graftpunk
   ```

2. **Install uv** (if not already installed)

   ```bash
   # macOS/Linux
   curl -LsSf https://astral.sh/uv/install.sh | sh

   # Or with Homebrew
   brew install uv
   ```

3. **Install dependencies**

   ```bash
   uv sync --all-extras
   ```

4. **Verify the installation**

   ```bash
   uv run gp --help
   uv run pytest tests/ -v
   ```

## Development Workflow

### Making Changes

1. **Create a feature branch**

   ```bash
   git checkout -b feature/your-feature-name
   ```

2. **Make your changes** following the code style guidelines below

3. **Write or update tests** for your changes

4. **Run the test suite**

   ```bash
   pytest tests/ -v
   ```

5. **Run code quality checks**

   ```bash
   # Preferred: use just check (runs all checks)
   just check

   # Or run individually:
   uvx ruff check .
   uvx ruff format .
   uvx ty@0.0.75 check src/
   ```

6. **Commit your changes** with a descriptive message

   ```bash
   git commit -m "feat: add support for Redis storage backend"
   ```

### Commit Message Format

We follow [Conventional Commits](https://www.conventionalcommits.org/):

- `feat:` - New features
- `fix:` - Bug fixes
- `docs:` - Documentation changes
- `test:` - Test additions or modifications
- `refactor:` - Code refactoring without feature changes
- `chore:` - Maintenance tasks

Examples:

```bash
git commit -m "feat: add S3 storage backend support"
git commit -m "fix: handle expired session gracefully"
git commit -m "docs: improve plugin development guide"
```

## Code Style

### Python

- **Formatter**: Ruff
- **Linter**: Ruff
- **Type checker**: ty (Astral's fast type checker)
- **Line length**: 100 characters

### Style Guidelines

1. **Use type hints** for all function signatures

   ```python
   def cache_session(session: BrowserSession, name: str) -> str: ...
   ```

2. **Use docstrings** for public functions and classes (Google style)

   ```python
   def load_session(name: str) -> BrowserSession:
       """Load a cached browser session.

       Args:
           name: The session identifier.

       Returns:
           The restored BrowserSession instance.

       Raises:
           SessionNotFoundError: If no session exists with the given name.
       """
   ```

3. **Prefer explicit over implicit**

   ```python
   # Good
   from graftpunk.exceptions import SessionNotFoundError

   # Avoid
   from graftpunk.exceptions import *
   ```

4. **Use structured logging**

   ```python
   from graftpunk.logging import get_logger

   LOG = get_logger(__name__)
   LOG.info("session_cached", name=name, storage_backend="local")
   ```

## Testing

### Running Tests

```bash
# Run all tests
pytest tests/ -v

# Run with coverage
pytest tests/ --cov=src/graftpunk --cov-report=term-missing

# Run specific test file
pytest tests/unit/test_cache.py -v

# Run tests matching a pattern
pytest tests/ -k "test_encryption" -v
```

### A test that fails in CI and passes alone

A CI runner is a loaded machine, so a test whose outcome depends on how fast the machine runs can pass on a quiet laptop and fail there. `scripts/repeat_under_load.sh` runs one test many times while busy loops hold every CPU, and counts the failures:

```bash
scripts/repeat_under_load.sh 50 tests/unit/test_login_verdict.py::TestVerdictComesLast::test_a_rate_limit_page_is_named
```

Run it before calling a CI failure a flake, and after fixing one. It first runs the test once with no load and stops with exit 2 if that fails, since a test that fails unloaded is broken rather than flaky. The default load is four busy loops per CPU; one per CPU was too light to reproduce the login-wait race this script was written for (0 failures in 30 runs at 10 loops, 4 in 40 at 40 loops). So a fix is shown when the same load that reproduced the failure before the fix gives zero failures over enough runs (100 is a good number); zero at a load that never reproduced it shows nothing. It exits 1 when any run failed, and keeps each run's output for reading the failure. The login tests' time budgets are an example: `tests/unit/conftest.py`'s `_login_hard_end_out_of_reach` fixture moves a nodriver login wait's hard end out of reach so a slow tick cannot end the wait before its last page read.

### Does a session still send what the last release sent?

`scripts/header_parity.py` answers that for `GraftpunkSession`. It runs a fixed set of request cases (no roles, captured roles in canonical and HTTP/2 spelling, declared roles for commands and for `gp http`, `request_with_role` with caller headers and `referer=`, a default role, a header the user set, on the site's origin, another origin of its site, and another site) against a `git archive` of a ref and against the checkout. Each case goes through a fake requests adapter that records the request and answers 200, so nothing leaves the machine. It prints every header that differs (value, spelling, added, gone, or the order of the names). A difference the release means to make is listed in `scripts/header_parity_allowed.json` with its case pattern, the headers allowed to differ, and a one-line reason.

```bash
python scripts/header_parity.py                 # against v1.46.0, the default
python scripts/header_parity.py --ref v1.45.0   # against another release
```

Run it after any change to `src/graftpunk/graftpunk_session.py` or `src/graftpunk/observe/headers.py`. It exits 0 when every difference is allowed, 1 when one is not, and 2 on a usage error (a ref git cannot archive, an unreadable allowlist). A new difference is either a bug or an entry for the allowlist and the changelog, never both unstated.

### Does the ExtraInfo correlator still give each request its own sent headers?

`scripts/correlator_sweep.py` replays every order of the CDP events of several two-hop request chains (a form POST and its redirect GET on HTTP/1.1 and HTTP/2, POSTs with an empty body, 307 and 308 POST pairs, XHR GETs that name a `Content-Type`), with each chain's request `ExtraInfo` dropped in turn, through `SeleniumCaptureBackend`, in a `git archive` of each ref and in the checkout. For every order it records which entry holds whose sent headers and the header roles, read mid-capture and after the drain, and compares.

```bash
python scripts/correlator_sweep.py --ref v1.46.0 --ref <the branch's base>
```

Run it after any change to `_ExtraInfoCorrelator` or `_request_headers_match` in `src/graftpunk/observe/capture.py`. It exits 0 when no order is worse in the checkout than in any ref (more entries holding another request's headers, or, where the ref attributed every entry rightly, a role lost or a role missing a header the ref's had), 1 otherwise, and 2 on a usage error. Each line also counts the reads with fewer entries holding another request's headers, the reads where the checkout builds a role the ref did not, and the roles the ref built from another request's headers that the checkout drops. It prints, per chain, the reads each side got wrong, so a residual both sides share (two GETs, or a 307 pair, whose sent headers carry the same body evidence) shows as equal counts.

### Writing Tests

- Place unit tests in `tests/unit/`
- Place integration tests in `tests/integration/`
- Use descriptive test names: `test_<what>_<when>_<expected>`
- Mock external dependencies (browsers, network, filesystem)

### Committed fixtures

`tests/fixtures/session_compat/<version>/` holds sessions saved by released
graftpunk versions, and `tests/unit/test_session_compat.py` loads each one
through every loader. `scripts/session_compat.py` makes them and checks the
other direction:

```bash
# Cache every case with a release (installed from PyPI into a scratch venv).
# --out must not hold files; a new release's fixtures go to
# tests/fixtures/session_compat/<version>.
python scripts/session_compat.py generate --graftpunk 1.44.0 --out "$(mktemp -d)/1.44.0"
# Can a release load a session this checkout stored? Exit 0 prints its cookies
# and tokens; exit 3 prints the exception it raised.
python scripts/session_compat.py read --graftpunk 1.44.0 --store DIR --name myshop
# Write that session with this checkout first. Without --format the setting
# is left unset, so the checkout's own default decides.
python scripts/session_compat.py write --store DIR --name myshop [--format record]
# Does this checkout, with the setting unset, read and write back every
# fixture exactly as a release does? Exit 0 prints "no differences"; exit 1
# prints each value that differs.
python scripts/session_compat.py compare --graftpunk 1.44.0
# What does each public reader do with each kind of stored value no session
# can use, on a release and on this checkout? Prints every outcome; "!" marks
# one that differs. Exits 0.
python scripts/session_compat.py bad-payloads --graftpunk 1.44.0
```

`compare` loads every fixture under `tests/fixtures/session_compat/<version>/`
with `load_session_for_api_from_bytes` and `load_session_for_api`, writes each
back with `update_session_cookies` after adding a cookie, and records the API
sessions' cookies, policy, headers and riders, the sha256 of the payload the
write-back stored, and the class `load_session` then returns. It does this
once with the release and once with this checkout, both with
`PYTHONHASHSEED=0`, and prints the differences. The release is installed with
this checkout's `uv.lock` versions of the dependencies the two share, so only
graftpunk differs (`--no-pin` installs the newest instead); the first two
lines of output name the graftpunk, requests, urllib3 and dill versions each
side ran. If a release's own dependency ranges exclude a locked version, the
install fails with uv's error and exit 2; run it with `--no-pin` then. Run it after any change to a
loader, a write-back or the stored format, against the last release; the
`old-reader` job runs it for 1.44.0 and the newest release. Against 1.21.0 it
reports differences by design (that release gives API sessions another
cookie policy and has fewer riders).

`bad-payloads` builds each payload in `BAD_PAYLOADS` (an empty or unreadable
pickle, a class that refuses its state, cookies that are not a jar, a missing
class or module, a class that raises while restored, and invalid and newer
records) with each side's own graftpunk, and reads it with `load_session`, the
three API loaders, `copy_session` and `update_session_cookies`. Each line gives
what each side returned or raised and the warning and error log lines it
wrote. Run it after any change to a loader's error handling, against the last
release, and make the CHANGELOG's Upgrading notes name every line marked `!`.

The `old-reader` job in `.github/workflows/python-quality.yml` runs `write` and
`read` for 1.21.0, 1.44.0 and the newest release: the default setting must
write what each of them loads, tokens included. For 1.21.0 and 1.44.0 a record
must also be reported expired (exit 3, `SessionExpiredError`) with the stored
bytes unchanged; the newest release reads records, so that step is skipped
for it.

What it checks: `generate` stores one session per case with a released
graftpunk, so the fixtures are what that release wrote, and
`tests/unit/test_session_compat.py` loads each one through every loader with
this checkout. `write` stores the `tokens` session with this checkout, and
`read` loads it with a release, which proves the other direction: a session
stored under the default setting (`pickle`) must load on 1.21.0 and 1.44.0 with
its cookies and tokens, and a record (`write --format record`) must be refused
as `SessionExpiredError` with the stored bytes unchanged.

Run `generate` (to a scratch directory) and the `write` and `read` pair before
any change to the stored session format, to the session record
(`src/graftpunk/session_record.py`), or to `BrowserSession.__getstate__`, and
add a directory under `tests/fixtures/session_compat/` when a release changes
what a session saves. The fixtures are never regenerated in place.

A change to the record layout (`FIELDS` in `src/graftpunk/session_record.py`)
follows one rule, because a release refuses a key it does not know inside a
block it owns and reports that record as an expired session. A new top-level
key needs no new `RECORD_VERSION` only if a release that does not know it can
ignore it on read, keep it unchanged through a write-back, and lose it on a
fresh save without harm. Any other change (a key inside `riders` or
`requests`, a changed meaning or type, a removed key) raises `RECORD_VERSION`.
`test_the_record_layout_changes_only_with_the_version` pins the layout of each
version and fails until the change follows the rule.
`tests/fixtures/session_compat/legacy/` keeps the browser-free load fixture
(`load_session_for_api_from_bytes`, used under Pyodide and Cloudflare
Workers). See [`tests/fixtures/session_compat/README.md`](tests/fixtures/session_compat/README.md)
for what each fixture holds and why the legacy one is pickled by reference.

### graftpunk-lite must stay browser-free

The repository builds two distributions of the same `graftpunk` package from
one `pyproject.toml`. `graftpunk` installs the browser stack. `graftpunk-lite`,
built by `scripts/build_lite.py`, leaves out every package that
`[tool.graftpunk.lite] browser-only` names and offers them as its `[browser]`
extra, so it resolves under Pyodide / Cloudflare Python Workers, where that
stack has no installable wheels. `graftpunk[browser]` is an empty extra, kept so
existing requirements resolve. `just build` builds both, and the "Lite install"
CI job installs each wheel into a clean venv. Three rules follow:

- Never import browser modules (`graftpunk.session`, `graftpunk.stealth`,
  selenium, requestium, nodriver, httpie) at **module scope** in anything the CLI
  imports; import them inside the function that needs them. A module-level
  import there breaks *every* `gp` command on graftpunk-lite, not just the
  browser ones. `test_cli_import_stays_browser_free` guards this.
- A new browser-only dependency goes in `[project] dependencies` **and** in
  `browser-only`. `tests/unit/test_build_lite.py` fails when a known browser
  package is missing from `browser-only` or a name there matches no dependency.
- An install hint names the extra through `graftpunk._distribution.install_command`,
  so a graftpunk-lite user is told `graftpunk-lite[...]`.

Example:

```python
def test_cache_session_creates_encrypted_file(tmp_path, mock_session):
    """Verify that cache_session encrypts and persists the session."""
    with patch("graftpunk.cache.get_storage_backend") as mock_storage:
        mock_storage.return_value = LocalSessionStorage(tmp_path)

        result = cache_session(mock_session, "test-session")

        assert result == "test-session"
        mock_storage.return_value.save_session.assert_called_once()
```

## Plugin Development

graftpunk supports three types of plugins via entry points:

### 1. Storage Backends

Implement `SessionStorageBackend` protocol:

```python
from graftpunk.storage.base import SessionStorageBackend, SessionMetadata


class MyStorage:
    def save_session(self, name: str, data: bytes, metadata: SessionMetadata) -> str: ...
    def load_session(self, name: str) -> tuple[bytes, SessionMetadata]: ...
    def list_sessions(self) -> list[str]: ...
    def delete_session(self, name: str) -> bool: ...
```

Register in `pyproject.toml`:

```toml
[project.entry-points."graftpunk.storage"]
mystorage = "mypackage:MyStorage"
```

### 2. Keepalive Handlers

Implement `KeepaliveHandler` protocol:

```python
from graftpunk.keepalive.handler import KeepaliveHandler, SessionStatus


class MyHandler:
    site_name: str = "My Site"

    def touch_session(self, api_session) -> tuple[bool, SessionStatus | None]: ...
    def validate_session(self, api_session) -> bool: ...
```

Register in `pyproject.toml`:

```toml
[project.entry-points."graftpunk.keepalive_handlers"]
myhandler = "mypackage:MyHandler"
```

### 3. Site Plugins

Subclass `SitePlugin` and use `@command` to define CLI commands. Handlers receive a `CommandContext` with the session, plugin metadata, and observability context:

```python
from graftpunk.plugins import CommandContext, LoginConfig, SitePlugin, command


class MySitePlugin(SitePlugin):
    site_name = "mysite"
    base_url = "https://example.com"
    backend = "nodriver"  # or "selenium"
    api_version = 1

    # Declarative login: auto-generates `gp mysite login`
    login_config = LoginConfig(
        url="/login",
        fields={"username": "#email", "password": "#password"},
        submit="button[type=submit]",
    )

    @command(help="List items")
    def items(self, ctx: CommandContext, page: int = 1):
        return ctx.session.get(f"{self.base_url}/api/items?page={page}").json()
```

For YAML plugins, see `examples/templates/yaml_template.yaml`. Both support declarative login, resource limits (`timeout`, `max_retries`, `rate_limit`), and output formatting (`--format json|table|raw`).

## Pull Request Process

1. **Ensure all tests pass** and code quality checks succeed

2. **Update documentation** if you've changed behavior

3. **Create a pull request** with:
   - Clear title describing the change
   - Description of what and why
   - Link to any related issues

4. **Address review feedback** promptly

5. **Squash commits** if requested before merge

## Releasing the skill

The Claude Code skill under `skills/` is versioned apart from the graftpunk
package. Any change under `skills/` or `.claude-plugin/`, or to
`docs/PLUGIN_DEVELOPMENT.md` (the installed plugin carries the guide, and the
skill reads it), needs a patch bump of the `version` field in
`.claude-plugin/plugin.json`; a new skill or a changed invocation contract is a
minor bump. That field pins an installed plugin:
setting it "pins the plugin to that version until you change it"
([plugins reference](https://code.claude.com/docs/en/plugins-reference), the
`version` field), so people who installed the skill from GitHub receive a
change only when it moves. Three kinds of install are not pinned by it: a
plugin with a `command` source, a plugin from a marketplace hosted on
claude.ai, and a plugin loaded in place from a marketplace added from a local
path. `.claude-plugin/marketplace.json` carries no root `version`: that field
is the marketplace manifest's own version
([marketplace reference](https://code.claude.com/docs/en/plugins/marketplace-reference),
top-level fields), and nothing here needs it.

The `Skill version` workflow checks this on every pull request that touches
those paths: the branch's own changes are measured from the merge base, and its
version has to be higher than the base branch's, so a bump another merge already
used fails whenever the check runs against a base that holds it. Two open pull
requests can still both pass and both merge, since `main` does not require
branches to be up to date; rerun the check after rebasing onto a `main` that
moved. Run the same check before pushing with `just skill-version`, which
compares against `origin/main`, or name another base with
`just skill-version <commit>`. After a merge, users update with
`/plugin marketplace update graftpunk`, then update graftpunk on the Installed
tab of `/plugin` (or run `claude plugin update graftpunk@graftpunk` in a shell).
`skills/graft/scripts/preflight.sh` prints the same procedure when the skill is
older than the installed graftpunk, and a test holds the two to the same words.

## Reporting Issues

When reporting bugs, please include:

- graftpunk version (`gp --version`)
- Python version (`python --version`)
- Operating system
- Chrome version (if browser-related)
- Steps to reproduce
- Expected vs actual behavior
- Error messages or logs

## Questions?

- Open an [Issue](https://github.com/stavxyz/graftpunk/issues) for questions, bugs, or feature requests

Thank you for contributing!
