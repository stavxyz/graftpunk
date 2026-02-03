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
   uvx ty check src/
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
   def cache_session(session: BrowserSession, name: str) -> str:
       ...
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

### Writing Tests

- Place unit tests in `tests/unit/`
- Place integration tests in `tests/integration/`
- Use descriptive test names: `test_<what>_<when>_<expected>`
- Mock external dependencies (browsers, network, filesystem)

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

    # Declarative login — auto-generates `gp mysite login`
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

- Open a [GitHub Discussion](https://github.com/stavxyz/graftpunk/discussions) for questions
- Open an [Issue](https://github.com/stavxyz/graftpunk/issues) for bugs or feature requests

Thank you for contributing!
