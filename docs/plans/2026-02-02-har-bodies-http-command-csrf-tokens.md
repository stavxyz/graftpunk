# HAR Bodies, `gp http` Command, and CSRF Token Support

**Goal:** Fix the broken capture→parse→analyze pipeline by adding request/response body capture with smart disk streaming, add `gp http` for ad-hoc authenticated requests with built-in observability, and add CSRF/dynamic token extraction with automatic injection.

**Architecture:** Three features that build on each other: (1) capture backends produce proper HAR 1.2 entries with bodies, closing the capture→parser→analyzer loop; (2) `gp http` uses cached sessions for authenticated CLI requests with observability on by default; (3) `Token`/`TokenConfig` types enable declarative token extraction with auto-injection in both plugin commands (Tier 2) and `gp http` (Tier 3).

**Tech Stack:** nodriver CDP (`network.enable`, `ResponseReceived`, `RequestWillBeSent`, `getResponseBody`, `getRequestPostData`, `runtime.enable`, `ConsoleAPICalled`), Selenium CDP (`execute_cdp_cmd`), requests, typer, HAR 1.2 spec.

---

## Task 1: Nodriver — full network capture with bodies

**Files:**
- Modify: `src/graftpunk/observe/capture.py`
- Test: `tests/unit/test_observe.py`

### What to build

Overhaul `NodriverCaptureBackend` to capture full request/response data:

1. Add `_on_request()` handler for `cdp.network.RequestWillBeSent` events
2. Replace `_har_entries` list with `_request_map: dict[str, dict]` keyed by request ID
3. Update `_on_response()` to correlate with `_request_map` by request ID
4. Add `stop_capture_async()` that fetches bodies:
   - POST bodies via `cdp.network.get_request_post_data(request_id)` for entries with `has_post_data` but no inline `postData`
   - Response bodies via `cdp.network.get_response_body(request_id)` for text-like MIME types under the size cap
5. Rewrite `get_har_entries()` to produce proper HAR 1.2 format entries from `_request_map`
6. Add console log capture: `cdp.runtime.enable()` + `ConsoleAPICalled` handler in `start_capture_async()`

Add `MAX_RESPONSE_BODY_SIZE` constant (default 5MB) to the module. Accept `max_body_size` parameter on the backend constructor and factory.

### Body streaming to disk

When a response should be streamed to disk instead of held in memory:
- **Binary/file MIME types** (always, regardless of size): `application/pdf`, `application/vnd.openxmlformats-*`, `application/vnd.ms-*`, `image/*`, `audio/*`, `video/*`, `application/zip`, `application/gzip`, `application/octet-stream`
- **Text-like responses over `max_body_size`**: JSON, HTML, XML, plain text, JavaScript

For disk-streamed bodies:
- The backend needs a `bodies_dir: Path | None` parameter (set by the caller who has access to `ObserveStorage.run_dir`)
- Write body to `bodies_dir/{request_id}.{ext}` (ext inferred from MIME type)
- In the HAR entry, set `response.content._bodyFile` to the relative path instead of `response.content.text`
- The `text` field should be `None` when `_bodyFile` is set

### HAR entry format (HAR 1.2 spec)

Each entry in `get_har_entries()` must produce:

```python
{
    "startedDateTime": "2026-02-02T10:30:00.000Z",  # from wall_time
    "time": 0,  # TODO: timing not tracked yet
    "request": {
        "method": "POST",
        "url": "https://example.com/api/data",
        "headers": [{"name": "Content-Type", "value": "application/json"}],
        "cookies": [],
        "queryString": [],
        "postData": {  # only if POST/PUT/PATCH with body
            "mimeType": "application/json",
            "text": '{"key": "value"}',
        },
    },
    "response": {
        "status": 200,
        "statusText": "OK",
        "headers": [{"name": "Content-Type", "value": "application/json"}],
        "cookies": [],
        "content": {
            "mimeType": "application/json",
            "size": 1234,
            "text": '{"results": [...]}',  # or None if _bodyFile is set
            "_bodyFile": None,  # or "bodies/abc123.json" for disk-streamed
        },
    },
}
```

### Console log capture

In `start_capture_async()`, after enabling network:

```python
import nodriver.cdp.runtime as cdp_runtime

await tab.send(cdp_runtime.enable())
tab.add_handler(cdp_runtime.ConsoleAPICalled, self._on_console)
```

The `_on_console` handler:

```python
def _on_console(self, event: Any) -> None:
    try:
        self._console_logs.append({
            "level": event.type_.value if hasattr(event.type_, 'value') else str(event.type_),
            "args": [
                getattr(arg, "value", str(arg))
                for arg in (event.args or [])
            ],
            "timestamp": getattr(event, "timestamp", None) or time.time(),
        })
    except Exception:
        LOG.exception("nodriver_on_console_failed")
```

### Protocol changes

Add to `CaptureBackend` protocol:

```python
async def stop_capture_async(self) -> None:
    """Stop capturing and fetch pending data (bodies, etc.) asynchronously."""
    ...
```

### Factory changes

Update `create_capture_backend`:

```python
def create_capture_backend(
    backend_type: str,
    driver: Any,
    get_tab: Callable[[], Any] | None = None,
    max_body_size: int = MAX_RESPONSE_BODY_SIZE,
    bodies_dir: Path | None = None,
) -> CaptureBackend:
```

### Tests

```python
# test_observe.py additions

class TestNodriverHARCapture:
    """Test nodriver backend produces proper HAR entries with bodies."""

    def test_on_request_stores_request_data(self):
        """_on_request populates _request_map with URL, method, headers, postData."""

    def test_on_response_correlates_with_request(self):
        """_on_response updates matching _request_map entry with status, headers."""

    def test_on_response_ignores_unknown_request_id(self):
        """_on_response for unknown request ID does not raise."""

    @pytest.mark.asyncio
    async def test_stop_capture_fetches_post_body(self):
        """stop_capture_async fetches postData via CDP for entries with has_post_data."""

    @pytest.mark.asyncio
    async def test_stop_capture_fetches_response_body(self):
        """stop_capture_async fetches response body for text-like MIME types."""

    @pytest.mark.asyncio
    async def test_stop_capture_skips_binary_response(self):
        """stop_capture_async does not fetch body for image/png."""

    @pytest.mark.asyncio
    async def test_stop_capture_skips_large_response(self):
        """stop_capture_async skips body over max_body_size for text types."""

    @pytest.mark.asyncio
    async def test_stop_capture_streams_binary_to_disk(self):
        """Binary MIME types are written to bodies_dir, _bodyFile set in HAR entry."""

    @pytest.mark.asyncio
    async def test_stop_capture_streams_large_text_to_disk(self):
        """Text responses over max_body_size are written to bodies_dir."""

    def test_get_har_entries_produces_valid_har_format(self):
        """get_har_entries returns HAR 1.2 spec entries."""

    def test_get_har_entries_includes_post_data(self):
        """HAR entries include postData for POST requests."""


class TestNodriverConsoleCapture:
    """Test nodriver console log capture via CDP."""

    @pytest.mark.asyncio
    async def test_start_capture_enables_runtime(self):
        """start_capture_async calls cdp.runtime.enable()."""

    def test_on_console_stores_log(self):
        """_on_console appends log entry with level, args, timestamp."""

    def test_on_console_handles_missing_args(self):
        """_on_console handles event with no args gracefully."""
```

### Commit

```
feat: add full network capture with request/response bodies and console logs to nodriver backend
```

---

## Task 2: Selenium — full network capture with bodies

**Files:**
- Modify: `src/graftpunk/observe/capture.py`
- Test: `tests/unit/test_observe.py`

### What to build

Overhaul `SeleniumCaptureBackend` to produce proper HAR entries:

1. Add `_request_map: dict[str, dict]` to `__init__`
2. In `stop_capture()`, parse performance log CDP events (`Network.requestWillBeSent`, `Network.responseReceived`), correlate by request ID
3. Fetch POST bodies via `driver.execute_cdp_cmd("Network.getRequestPostData", {"requestId": ...})`
4. Fetch response bodies via `driver.execute_cdp_cmd("Network.getResponseBody", {"requestId": ...})` for text-like MIME types under size cap
5. Apply same disk streaming logic as nodriver backend (binary types + large text → `bodies_dir`)
6. Rewrite `get_har_entries()` to produce HAR 1.2 format from `_request_map`
7. Parse console logs from performance log (`Runtime.consoleAPICalled` events) in `stop_capture()`
8. Add `stop_capture_async()` that wraps `stop_capture()` for protocol compliance

### Shared helpers

Extract shared logic into module-level helpers to avoid duplication between backends:

```python
# MIME type constants
BINARY_MIME_PREFIXES = (
    "image/", "audio/", "video/",
    "application/pdf", "application/zip", "application/gzip",
    "application/octet-stream", "application/vnd.openxmlformats-",
    "application/vnd.ms-",
)

TEXT_MIME_KEYWORDS = ("json", "html", "text", "xml", "javascript", "css")

def _is_binary_mime(mime: str) -> bool:
    """Check if MIME type represents a binary/file format."""

def _is_text_mime(mime: str) -> bool:
    """Check if MIME type represents a text format worth capturing body for."""

def _should_stream_to_disk(mime: str, body_size: int, max_body_size: int) -> bool:
    """Determine if a response body should be streamed to disk vs held in memory."""

def _mime_to_extension(mime: str) -> str:
    """Map MIME type to file extension for disk-streamed bodies."""

def _build_har_entry(request_data: dict, request_id: str) -> dict:
    """Build a HAR 1.2 entry dict from correlated request/response data."""

def _wall_time_to_iso(wall_time: float | None) -> str:
    """Convert CDP wall_time (unix epoch float) to ISO 8601 string."""
```

### Constructor changes

```python
class SeleniumCaptureBackend:
    def __init__(
        self,
        driver: Any,
        max_body_size: int = MAX_RESPONSE_BODY_SIZE,
        bodies_dir: Path | None = None,
    ) -> None:
```

### Tests

```python
class TestSeleniumHARCapture:
    """Test Selenium backend produces proper HAR entries with bodies."""

    def test_stop_capture_parses_cdp_request_events(self):
        """Performance log requestWillBeSent events are parsed into _request_map."""

    def test_stop_capture_parses_cdp_response_events(self):
        """Performance log responseReceived events correlate with requests."""

    def test_stop_capture_fetches_post_body(self):
        """POST body fetched via execute_cdp_cmd for entries with hasPostData."""

    def test_stop_capture_fetches_response_body(self):
        """Response body fetched via execute_cdp_cmd for text MIME types."""

    def test_stop_capture_skips_binary_response(self):
        """Binary MIME types don't get body fetched (but do get disk-streamed)."""

    def test_stop_capture_streams_binary_to_disk(self):
        """Binary MIME types written to bodies_dir."""

    def test_stop_capture_streams_large_text_to_disk(self):
        """Text responses over max_body_size written to bodies_dir."""

    def test_get_har_entries_produces_valid_har_format(self):
        """get_har_entries produces HAR 1.2 spec entries after stop_capture."""

    def test_stop_capture_handles_evicted_request(self):
        """CDP call failure (evicted data) is handled gracefully."""

    def test_stop_capture_parses_console_logs(self):
        """Runtime.consoleAPICalled events are parsed from performance log."""


class TestSharedHARHelpers:
    """Test shared MIME type and HAR formatting helpers."""

    def test_is_binary_mime_pdf(self):
    def test_is_binary_mime_image(self):
    def test_is_text_mime_json(self):
    def test_is_text_mime_html(self):
    def test_should_stream_binary_always(self):
    def test_should_stream_large_text(self):
    def test_should_not_stream_small_text(self):
    def test_wall_time_to_iso(self):
    def test_build_har_entry_with_post_data(self):
    def test_build_har_entry_with_body_file(self):
    def test_mime_to_extension(self):
```

### Commit

```
feat: add full network capture with bodies and console logs to Selenium backend
```

---

## Task 3: HAR parser — support `_bodyFile` references

**Files:**
- Modify: `src/graftpunk/har/parser.py`
- Test: `tests/unit/test_har_parser.py`

### What to build

Update `_parse_response()` to handle `_bodyFile` references in HAR entries:

```python
def _parse_response(response_data: dict[str, Any], base_dir: Path | None = None) -> HARResponse:
    content = response_data.get("content", {})
    if isinstance(content, dict):
        body = content.get("text")
        body_file = content.get("_bodyFile")
        body_size = content.get("size", 0)

        # If body is stored on disk, load it (or store reference)
        if body_file and base_dir:
            body_path = base_dir / body_file
            if body_path.exists():
                # For text files, read content; for binary, store path reference
                if _is_text_mime(content.get("mimeType", "")):
                    body = body_path.read_text(encoding="utf-8", errors="replace")
                else:
                    body = f"[binary file: {body_file}]"
                body_size = body_path.stat().st_size
```

Update `parse_har_file()` to pass `base_dir` (the directory containing the HAR file) to `_parse_response()`:

```python
def parse_har_file(filepath: Path | str) -> HARParseResult:
    filepath = Path(filepath)
    base_dir = filepath.parent
    # ... pass base_dir to _parse_entries which passes to _parse_response
```

Add `_bodyFile` field to `HARResponse`:

```python
@dataclass
class HARResponse:
    # ... existing fields ...
    body_file: str | None = None  # relative path to body file on disk
```

### Tests

```python
class TestHARParserBodyFile:
    def test_parse_response_with_body_file(self, tmp_path):
        """_bodyFile reference loads text content from disk."""

    def test_parse_response_with_binary_body_file(self, tmp_path):
        """_bodyFile for binary content stores path reference string."""

    def test_parse_response_with_missing_body_file(self, tmp_path):
        """Missing _bodyFile does not crash, body is None."""

    def test_parse_har_file_passes_base_dir(self, tmp_path):
        """parse_har_file passes its directory as base_dir for body resolution."""

    def test_parse_response_prefers_inline_text_over_body_file(self):
        """If both text and _bodyFile present, text takes precedence."""
```

### Commit

```
feat: support _bodyFile references in HAR parser for disk-streamed bodies
```

---

## Task 4: Wire capture into `observe go` and `_stop_observe`

**Files:**
- Modify: `src/graftpunk/cli/main.py` (`_run_observe_go`)
- Modify: `src/graftpunk/session.py` (`_stop_observe`)
- Modify: `src/graftpunk/observe/context.py` (`build_observe_context`)
- Test: `tests/unit/test_observe.py`, `tests/unit/test_cli.py`

### What to build

**`_run_observe_go` changes:**
1. Create `bodies_dir = storage.run_dir / "bodies"` and pass to `NodriverCaptureBackend`
2. Pass `max_body_size` from a new `--max-body-size` option (default 5MB)
3. Call `await backend.stop_capture_async()` before `backend.get_har_entries()` (to fetch bodies)
4. Write console logs via `storage.write_console_logs(backend.get_console_logs())`
5. Remove the existing TODO comment about console logs

**`build_observe_context` changes:**
- Pass `bodies_dir` (derived from storage.run_dir) to `create_capture_backend`

**`_stop_observe` changes (in session.py):**
- If capture backend has `stop_capture_async`, run it before `get_har_entries()`
- Handle the async case (may need `asyncio.run()` or check if loop is running)

### Tests

```python
class TestObserveGoCapture:
    @pytest.mark.asyncio
    async def test_observe_go_calls_stop_capture(self):
        """_run_observe_go calls stop_capture_async before get_har_entries."""

    @pytest.mark.asyncio
    async def test_observe_go_writes_console_logs(self):
        """_run_observe_go writes console logs to storage."""

    @pytest.mark.asyncio
    async def test_observe_go_creates_bodies_dir(self):
        """_run_observe_go passes bodies_dir to capture backend."""

    def test_observe_go_max_body_size_option(self):
        """--max-body-size flag is passed through to backend."""
```

### Commit

```
feat: wire full capture (bodies, console logs, disk streaming) into observe go
```

---

## Task 5: Token types and extraction logic

**Files:**
- Create: `src/graftpunk/tokens.py`
- Modify: `src/graftpunk/plugins/cli_plugin.py`
- Modify: `src/graftpunk/plugins/__init__.py`
- Test: `tests/unit/test_tokens.py` (new)
- Test: `tests/unit/test_cli_plugin.py`

### What to build

**New file `src/graftpunk/tokens.py`:**

```python
"""CSRF token and dynamic header extraction for authenticated sessions."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any

import requests

from graftpunk.logging import get_logger

LOG = get_logger(__name__)


@dataclass(frozen=True)
class Token:
    """Configuration for extracting a dynamic token from a web page or session."""

    name: str                          # Header name to inject (e.g. "CSRFToken")
    source: str                        # "page", "cookie", "response_header"
    pattern: str | None = None         # Regex with capture group (for "page" source)
    cookie_name: str | None = None     # Cookie name (for "cookie" source)
    response_header: str | None = None # Response header (for "response_header" source)
    page_url: str = "/"                # URL to fetch for extraction (for "page" source)
    cache_duration: float = 300        # Cache TTL in seconds

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("Token.name must be non-empty")
        if self.source not in ("page", "cookie", "response_header"):
            raise ValueError(
                f"Token.source must be 'page', 'cookie', or 'response_header', "
                f"got {self.source!r}"
            )
        if self.source == "page" and not self.pattern:
            raise ValueError("Token with source='page' requires a pattern")
        if self.source == "cookie" and not self.cookie_name:
            raise ValueError("Token with source='cookie' requires cookie_name")
        if self.source == "response_header" and not self.response_header:
            raise ValueError(
                "Token with source='response_header' requires response_header"
            )

    @classmethod
    def from_meta_tag(
        cls, name: str, header: str, page_url: str = "/", cache_duration: float = 300
    ) -> Token:
        """Create token config for HTML <meta name="..." content="..."> extraction."""
        return cls(
            name=header,
            source="page",
            pattern=rf'<meta\s+name=["\']?{re.escape(name)}["\']?\s+content=["\']([^"\']+)',
            page_url=page_url,
            cache_duration=cache_duration,
        )

    @classmethod
    def from_cookie(
        cls, cookie_name: str, header: str, cache_duration: float = 300
    ) -> Token:
        """Create token config for cookie-based CSRF (e.g. Django csrftoken)."""
        return cls(
            name=header,
            source="cookie",
            cookie_name=cookie_name,
            cache_duration=cache_duration,
        )

    @classmethod
    def from_js_variable(
        cls, pattern: str, header: str, page_url: str = "/", cache_duration: float = 300
    ) -> Token:
        """Create token config for JavaScript variable extraction."""
        return cls(
            name=header,
            source="page",
            pattern=pattern,
            page_url=page_url,
            cache_duration=cache_duration,
        )

    @classmethod
    def from_response_header(
        cls,
        response_header: str,
        request_header: str,
        page_url: str = "/",
        cache_duration: float = 300,
    ) -> Token:
        """Create token config for response header extraction."""
        return cls(
            name=request_header,
            source="response_header",
            response_header=response_header,
            page_url=page_url,
            cache_duration=cache_duration,
        )


@dataclass(frozen=True)
class TokenConfig:
    """Collection of token extraction rules for a plugin."""

    tokens: tuple[Token, ...]

    def __post_init__(self) -> None:
        if not self.tokens:
            raise ValueError("TokenConfig.tokens must be non-empty")


@dataclass(frozen=True)
class CachedToken:
    """An extracted token value with TTL."""

    name: str
    value: str
    extracted_at: float
    ttl: float

    @property
    def is_expired(self) -> bool:
        return (time.time() - self.extracted_at) > self.ttl


def extract_token(session: requests.Session, token: Token, base_url: str) -> str:
    """Extract a token value using the configured strategy.

    Args:
        session: Authenticated requests.Session with cookies.
        token: Token extraction configuration.
        base_url: Plugin's base URL for relative page_url resolution.

    Returns:
        Extracted token value.

    Raises:
        ValueError: If token cannot be extracted.
    """
    if token.source == "cookie":
        value = session.cookies.get(token.cookie_name)  # type: ignore[arg-type]
        if not value:
            raise ValueError(f"Cookie '{token.cookie_name}' not found in session")
        return value

    if token.source == "response_header":
        url = f"{base_url.rstrip('/')}{token.page_url}"
        resp = session.head(url, timeout=10, allow_redirects=True)
        value = resp.headers.get(token.response_header)  # type: ignore[arg-type]
        if not value:
            raise ValueError(
                f"Header '{token.response_header}' not found in response from {url}"
            )
        return value

    if token.source == "page":
        url = f"{base_url.rstrip('/')}{token.page_url}"
        resp = session.get(url, timeout=15)
        resp.raise_for_status()
        match = re.search(token.pattern, resp.text)  # type: ignore[arg-type]
        if not match:
            raise ValueError(
                f"Token pattern not found in {url}: {token.pattern}"
            )
        return match.group(1)

    raise ValueError(f"Unknown token source: {token.source}")


_CACHE_ATTR = "_gp_cached_tokens"


def prepare_session(
    session: requests.Session,
    token_config: TokenConfig,
    base_url: str,
) -> requests.Session:
    """Extract all tokens and inject as session headers.

    Uses in-memory cache on the session object. Expired tokens are re-extracted.

    Args:
        session: Authenticated requests.Session.
        token_config: Token extraction rules.
        base_url: Plugin's base URL.

    Returns:
        The same session with tokens injected as headers.
    """
    cache: dict[str, CachedToken] = getattr(session, _CACHE_ATTR, {})

    for token in token_config.tokens:
        cached = cache.get(token.name)
        if cached and not cached.is_expired:
            session.headers[token.name] = cached.value
            continue

        try:
            value = extract_token(session, token, base_url)
            cache[token.name] = CachedToken(
                name=token.name,
                value=value,
                extracted_at=time.time(),
                ttl=token.cache_duration,
            )
            session.headers[token.name] = value
            LOG.info("token_extracted", name=token.name, source=token.source)
        except ValueError:
            LOG.exception("token_extraction_failed", name=token.name)
            raise

    setattr(session, _CACHE_ATTR, cache)
    return session


def clear_cached_tokens(session: requests.Session) -> None:
    """Clear all cached tokens from a session (e.g. for retry after 403)."""
    if hasattr(session, _CACHE_ATTR):
        getattr(session, _CACHE_ATTR).clear()
```

**`cli_plugin.py` changes:**

Add to `SitePlugin`:
```python
token_config: TokenConfig | None = None
```

Add to `CLIPluginProtocol`:
```python
@property
def token_config(self) -> TokenConfig | None: ...
```

Add `token_config: TokenConfig | None = None` to `PluginConfig`.

**`plugins/__init__.py` changes:**

Add `Token`, `TokenConfig` to imports and `__all__`.

### Tests

```python
# tests/unit/test_tokens.py

class TestToken:
    def test_token_validation_empty_name(self):
    def test_token_validation_invalid_source(self):
    def test_token_page_requires_pattern(self):
    def test_token_cookie_requires_cookie_name(self):
    def test_token_response_header_requires_header(self):
    def test_from_meta_tag(self):
    def test_from_cookie(self):
    def test_from_js_variable(self):
    def test_from_response_header(self):

class TestTokenConfig:
    def test_empty_tokens_raises(self):
    def test_valid_config(self):

class TestCachedToken:
    def test_not_expired(self):
    def test_expired(self):

class TestExtractToken:
    def test_extract_from_cookie(self):
    def test_extract_from_cookie_missing(self):
    def test_extract_from_page(self, requests_mock):
    def test_extract_from_page_pattern_not_found(self, requests_mock):
    def test_extract_from_response_header(self, requests_mock):
    def test_extract_from_response_header_missing(self, requests_mock):

class TestPrepareSession:
    def test_extracts_and_injects_token(self, requests_mock):
    def test_uses_cached_token(self, requests_mock):
    def test_re_extracts_expired_token(self, requests_mock):
    def test_multiple_tokens(self, requests_mock):

class TestClearCachedTokens:
    def test_clears_cache(self):
    def test_no_cache_no_error(self):
```

### Commit

```
feat: add Token, TokenConfig types and extraction logic for CSRF/dynamic tokens
```

---

## Task 6: Auto-inject tokens in plugin commands (Tier 2)

**Files:**
- Modify: `src/graftpunk/cli/plugin_commands.py`
- Test: `tests/unit/test_plugin_commands.py`

### What to build

In `_create_plugin_command`'s `callback`, after loading the session and before building `CommandContext`, check for `token_config` and call `prepare_session()`:

```python
# After: session = plugin.get_session() if needs_session else requests.Session()
# Before: ctx = CommandContext(...)

# Auto-inject tokens if plugin declares token_config
token_config = getattr(plugin, "token_config", None)
if token_config is not None and needs_session:
    from graftpunk.tokens import prepare_session as _prepare_tokens
    base_url = getattr(plugin, "base_url", "")
    try:
        _prepare_tokens(session, token_config, base_url)
    except ValueError as exc:
        gp_console.error(f"Token extraction failed: {exc}")
        raise SystemExit(1) from exc
```

### Tests

```python
class TestTokenAutoInjection:
    def test_command_with_token_config_injects_tokens(self):
        """Plugin with token_config gets prepare_session called."""

    def test_command_without_token_config_skips_injection(self):
        """Plugin without token_config is unaffected."""

    def test_token_extraction_failure_shows_error(self):
        """Token extraction ValueError shows user-friendly error."""
```

### Commit

```
feat: auto-inject tokens in plugin commands via token_config (Tier 2)
```

---

## Task 7: YAML token_config support

**Files:**
- Modify: `src/graftpunk/plugins/yaml_loader.py`
- Modify: `src/graftpunk/plugins/yaml_plugin.py`
- Test: `tests/unit/test_yaml_loader.py`

### What to build

In `parse_yaml_plugin()`, parse a `tokens:` block and construct `TokenConfig`:

```yaml
# Example YAML syntax
tokens:
  - name: CSRFToken
    source: page
    pattern: "ACC\\.config\\.CSRFToken\\s*=\\s*'([^']+)'"
    page_url: /
    cache_duration: 300
  - name: X-CSRFToken
    source: cookie
    cookie_name: csrftoken
```

In `parse_yaml_plugin()`, after parsing login config:

```python
from graftpunk.tokens import Token, TokenConfig

# Parse token config
tokens_block = data.get("tokens")
token_config: TokenConfig | None = None
if tokens_block is not None:
    if not isinstance(tokens_block, list):
        raise PluginError(
            f"Plugin '{filepath}': 'tokens' must be a list of token definitions."
        )
    tokens = []
    for i, token_def in enumerate(tokens_block):
        if not isinstance(token_def, dict):
            raise PluginError(
                f"Plugin '{filepath}': token #{i + 1} must be a mapping."
            )
        if "name" not in token_def or "source" not in token_def:
            raise PluginError(
                f"Plugin '{filepath}': token #{i + 1} missing required "
                f"field(s) 'name' and/or 'source'."
            )
        tokens.append(Token(
            name=token_def["name"],
            source=token_def["source"],
            pattern=token_def.get("pattern"),
            cookie_name=token_def.get("cookie_name"),
            response_header=token_def.get("response_header"),
            page_url=token_def.get("page_url", "/"),
            cache_duration=token_def.get("cache_duration", 300),
        ))
    token_config = TokenConfig(tokens=tuple(tokens))
```

Pass `token_config` to `build_plugin_config()` (add `token_config` parameter to `build_plugin_config`).

Also update `yaml_plugin.py` — after `dataclasses.asdict(config)` deep-converts `TokenConfig` to a plain dict, restore it (same pattern as `LoginConfig` restoration).

### Tests

```python
class TestYAMLTokenConfig:
    def test_parse_tokens_from_yaml(self, tmp_path):
        """tokens: block in YAML produces TokenConfig on PluginConfig."""

    def test_parse_tokens_page_source(self, tmp_path):
        """Token with source=page and pattern is parsed correctly."""

    def test_parse_tokens_cookie_source(self, tmp_path):
        """Token with source=cookie and cookie_name is parsed correctly."""

    def test_parse_tokens_invalid_not_list(self, tmp_path):
        """tokens: as non-list raises PluginError."""

    def test_parse_tokens_missing_name(self, tmp_path):
        """Token without name field raises PluginError."""

    def test_no_tokens_block_is_none(self, tmp_path):
        """No tokens: block means token_config is None."""
```

### Commit

```
feat: add YAML token_config support for declarative token extraction
```

---

## Task 8: `gp http` command

**Files:**
- Create: `src/graftpunk/cli/http_commands.py`
- Modify: `src/graftpunk/cli/main.py`
- Test: `tests/unit/test_http_commands.py` (new)

### What to build

**New file `src/graftpunk/cli/http_commands.py`:**

```python
"""Ad-hoc HTTP requests with cached session cookies."""

from __future__ import annotations

import datetime
import json
import os
import sys
from typing import Annotated, Any

import requests
import typer
from rich.console import Console

from graftpunk import console as gp_console
from graftpunk.cache import load_session_for_api
from graftpunk.logging import get_logger
from graftpunk.observe.context import OBSERVE_BASE_DIR
from graftpunk.observe.storage import ObserveStorage
from graftpunk.session_context import resolve_session

LOG = get_logger(__name__)
console = Console(stderr=True)

http_app = typer.Typer(name="http", help="Make HTTP requests with cached session cookies")

DEFAULT_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "sec-ch-ua": '"Chromium";v="131", "Not_A Brand";v="24"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
}

MAX_TOKEN_RETRIES = 1  # Single retry on 403 with token refresh


def _resolve_json_body(json_arg: str) -> str:
    """Resolve JSON body from inline string, @filename, or @- (stdin)."""
    if json_arg.startswith("@"):
        source = json_arg[1:]
        if source == "-":
            return sys.stdin.read()
        from pathlib import Path
        path = Path(source)
        if not path.exists():
            raise typer.BadParameter(f"File not found: {source}")
        return path.read_text(encoding="utf-8")
    return json_arg


def _make_request(
    method: str,
    url: str,
    session_name: str,
    *,
    json_body: str | None = None,
    form_data: str | None = None,
    extra_headers: list[str] | None = None,
    browser_headers: bool = True,
    timeout: float = 30,
) -> requests.Response:
    """Make an HTTP request using a cached session."""
    session = load_session_for_api(session_name)

    if browser_headers:
        session.headers.update(DEFAULT_BROWSER_HEADERS)

    if json_body:
        session.headers["Accept"] = "application/json"
        session.headers["Content-Type"] = "application/json"

    for header in (extra_headers or []):
        name, _, value = header.partition(":")
        if not name:
            raise typer.BadParameter(f"Invalid header format: {header!r}. Use 'Name: value'.")
        session.headers[name.strip()] = value.strip()

    # Auto-inject tokens if the session maps to a plugin with token_config
    from graftpunk.cli.plugin_commands import _plugin_session_map
    plugin_name = None
    for pname, sname in _plugin_session_map.items():
        if sname == session_name or pname == session_name:
            plugin_name = pname
            break

    token_config = None
    base_url = ""
    if plugin_name:
        from graftpunk.cli.plugin_commands import _registered_plugins_for_teardown
        for plugin in _registered_plugins_for_teardown:
            if plugin.site_name == plugin_name:
                token_config = getattr(plugin, "token_config", None)
                base_url = getattr(plugin, "base_url", "")
                break

    if token_config:
        from graftpunk.tokens import prepare_session
        prepare_session(session, token_config, base_url)

    kwargs: dict[str, Any] = {"timeout": timeout}
    if json_body:
        resolved = _resolve_json_body(json_body)
        kwargs["data"] = resolved
    elif form_data:
        kwargs["data"] = form_data

    response = session.request(method, url, **kwargs)

    # Token refresh on 403: clear cached tokens, re-extract, retry once
    if response.status_code == 403 and token_config:
        from graftpunk.tokens import clear_cached_tokens, prepare_session as _prep
        clear_cached_tokens(session)
        _prep(session, token_config, base_url)
        response = session.request(method, url, **kwargs)

    return response


def _save_observe_data(
    session_name: str,
    method: str,
    url: str,
    response: requests.Response,
    request_headers: dict[str, str],
    request_body: str | None,
) -> ObserveStorage | None:
    """Save request/response as a HAR entry to observability storage."""
    run_id = datetime.datetime.now().strftime("%Y%m%d-%H%M%S") + f"-{os.getpid()}"
    try:
        storage = ObserveStorage(OBSERVE_BASE_DIR, session_name, run_id)
    except ValueError:
        return None

    entry = {
        "startedDateTime": datetime.datetime.now(datetime.UTC).isoformat(),
        "time": int(response.elapsed.total_seconds() * 1000),
        "request": {
            "method": method.upper(),
            "url": url,
            "headers": [{"name": k, "value": v} for k, v in request_headers.items()],
            "cookies": [],
            "queryString": [],
        },
        "response": {
            "status": response.status_code,
            "statusText": response.reason or "",
            "headers": [
                {"name": k, "value": v} for k, v in response.headers.items()
            ],
            "cookies": [],
            "content": {
                "mimeType": response.headers.get("Content-Type", ""),
                "size": len(response.content),
                "text": response.text if len(response.content) < 5 * 1024 * 1024 else None,
            },
        },
    }
    if request_body:
        entry["request"]["postData"] = {
            "mimeType": request_headers.get("Content-Type", ""),
            "text": request_body,
        }

    storage.write_har([entry])
    return storage


def _print_response(
    response: requests.Response,
    *,
    body_only: bool = False,
    verbose: bool = False,
) -> None:
    """Print HTTP response to stdout."""
    if body_only:
        sys.stdout.write(response.text)
        if not response.text.endswith("\n"):
            sys.stdout.write("\n")
        return

    if verbose:
        # Request info
        request = response.request
        sys.stdout.write(f"> {request.method} {request.url}\n")
        for name, value in (request.headers or {}).items():
            sys.stdout.write(f"> {name}: {value}\n")
        sys.stdout.write(">\n")

    # Status line
    sys.stdout.write(f"HTTP {response.status_code} {response.reason}\n")

    if verbose:
        for name, value in response.headers.items():
            sys.stdout.write(f"< {name}: {value}\n")
        sys.stdout.write("\n")
    else:
        # Show content-type and content-length in default mode
        ct = response.headers.get("Content-Type")
        cl = response.headers.get("Content-Length")
        if ct:
            sys.stdout.write(f"Content-Type: {ct}\n")
        if cl:
            sys.stdout.write(f"Content-Length: {cl}\n")
        sys.stdout.write("\n")

    sys.stdout.write(response.text)
    if response.text and not response.text.endswith("\n"):
        sys.stdout.write("\n")
```

For each HTTP method, create a command. Use a shared helper to avoid repetition:

```python
def _http_command(method: str):
    """Create an http subcommand for the given method."""

    @http_app.command(method.lower())
    def handler(
        url: Annotated[str, typer.Argument(help="Full URL to request")],
        session: Annotated[str | None, typer.Option("--session", "-s", help="Session name")] = None,
        json_body: Annotated[str | None, typer.Option("--json", "-j", help="JSON body (string or @file)")] = None,
        data: Annotated[str | None, typer.Option("--data", "-d", help="Form-encoded body")] = None,
        header: Annotated[list[str] | None, typer.Option("--header", "-H", help="Extra header (Name: value)")] = None,
        body_only: Annotated[bool, typer.Option("--body-only", help="Output only response body")] = False,
        verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Show request/response headers")] = False,
        timeout: Annotated[float, typer.Option(help="Request timeout in seconds")] = 30,
        no_browser_headers: Annotated[bool, typer.Option("--no-browser-headers", help="Don't add browser-like headers")] = False,
        no_observe: Annotated[bool, typer.Option("--no-observe", help="Don't save to observability storage")] = False,
    ) -> None:
        resolved = resolve_session(session)
        if not resolved:
            gp_console.error("No session specified. Use --session or `gp session use`.")
            raise typer.Exit(1)

        try:
            response = _make_request(
                method,
                url,
                resolved,
                json_body=json_body,
                form_data=data,
                extra_headers=header,
                browser_headers=not no_browser_headers,
                timeout=timeout,
            )
        except Exception as exc:
            gp_console.error(f"Request failed: {exc}")
            raise typer.Exit(1) from exc

        # Save to observe storage (on by default)
        if not no_observe:
            request_body = None
            if json_body:
                request_body = _resolve_json_body(json_body)
            elif data:
                request_body = data
            storage = _save_observe_data(
                resolved,
                method,
                url,
                response,
                dict(response.request.headers),
                request_body,
            )
            if storage and not body_only:
                console.print(f"[dim]Saved to {storage.run_dir}[/dim]")

        _print_response(response, body_only=body_only, verbose=verbose)

    handler.__name__ = f"http_{method.lower()}"
    handler.__doc__ = f"Make an HTTP {method.upper()} request with session cookies."
    return handler


# Register all HTTP method commands
for _method in ("get", "post", "put", "patch", "delete", "head", "options"):
    _http_command(_method)
```

**`main.py` changes:**

```python
from graftpunk.cli.http_commands import http_app
app.add_typer(http_app)
```

### Tests

```python
# tests/unit/test_http_commands.py

class TestResolveJsonBody:
    def test_inline_json(self):
    def test_from_file(self, tmp_path):
    def test_from_stdin(self, monkeypatch):
    def test_missing_file(self):

class TestMakeRequest:
    def test_get_with_browser_headers(self, requests_mock):
    def test_post_with_json_body(self, requests_mock):
    def test_no_browser_headers(self, requests_mock):
    def test_extra_headers(self, requests_mock):
    def test_token_refresh_on_403(self, requests_mock):

class TestSaveObserveData:
    def test_saves_har_entry(self, tmp_path):
    def test_includes_request_body(self, tmp_path):

class TestPrintResponse:
    def test_body_only(self, capsys):
    def test_verbose(self, capsys):
    def test_default_output(self, capsys):

class TestHTTPCommands:
    def test_get_command(self, runner):
    def test_post_command(self, runner):
    def test_no_session_error(self, runner):
    def test_no_observe_flag(self, runner):
```

### Commit

```
feat: add gp http command for ad-hoc authenticated HTTP requests
```

---

## Task 9: Token refresh on 403 (in `gp http` and plugin commands)

**Files:**
- Modify: `src/graftpunk/cli/plugin_commands.py`
- Test: `tests/unit/test_plugin_commands.py`

### What to build

The `gp http` 403 retry is already built into `_make_request()` (Task 8). For plugin commands, add retry logic in `_execute_with_limits()`:

In `_execute_with_limits`, after catching `requests.RequestException`:

```python
except requests.exceptions.HTTPError as exc:
    # Token refresh on 403: clear cached tokens and retry once
    if (
        exc.response is not None
        and exc.response.status_code == 403
        and hasattr(ctx, "_token_refreshed") is False
    ):
        token_config = getattr(plugin, "token_config", None) if plugin else None
        if token_config:
            from graftpunk.tokens import clear_cached_tokens, prepare_session as _prep
            clear_cached_tokens(ctx.session)
            base_url = getattr(plugin, "base_url", "")
            _prep(ctx.session, token_config, base_url)
            ctx._token_refreshed = True  # prevent infinite retry
            # Don't count this as a retry attempt
            continue
    last_exc = exc
    # ... existing retry logic
```

Actually, the simpler approach: wrap the token injection + command execution in `_create_plugin_command`'s callback:

```python
# After prepare_session() call, wrap execution with 403 retry
try:
    result = _execute_with_limits(cmd_spec.handler, ctx, cmd_spec, **kwargs)
except requests.exceptions.HTTPError as exc:
    if (
        exc.response is not None
        and exc.response.status_code == 403
        and token_config is not None
    ):
        from graftpunk.tokens import clear_cached_tokens, prepare_session as _prep
        clear_cached_tokens(session)
        _prep(session, token_config, base_url)
        result = _execute_with_limits(cmd_spec.handler, ctx, cmd_spec, **kwargs)
    else:
        raise
```

### Tests

```python
class TestTokenRefreshOn403:
    def test_403_retries_with_fresh_token(self):
        """HTTPError 403 clears tokens, re-extracts, and retries once."""

    def test_403_without_token_config_propagates(self):
        """HTTPError 403 without token_config raises normally."""

    def test_second_403_propagates(self):
        """If retry also returns 403, error propagates."""
```

### Commit

```
feat: auto-retry with fresh tokens on 403 in plugin commands
```

---

## Task 10: Update examples and templates

**Files:**
- Modify: `examples/plugins/hackernews.py`
- Modify: `examples/plugins/quotes.py`
- Modify: `examples/templates/yaml_template.yaml`

### What to build

Add `token_config` example usage to the example plugins. Add `tokens:` example to the YAML template.

For `hackernews.py` — HN doesn't use CSRF tokens, but add a comment:

```python
# token_config: No CSRF tokens needed for Hacker News.
# For sites that require dynamic tokens, add:
#
#     from graftpunk.tokens import Token, TokenConfig
#
#     token_config = TokenConfig(tokens=(
#         Token.from_meta_tag(name="csrf-token", header="X-CSRF-Token"),
#     ))
```

For `yaml_template.yaml`, add:

```yaml
# Optional: Token extraction for CSRF or dynamic headers
# tokens:
#   - name: X-CSRF-Token
#     source: page
#     pattern: '<meta name="csrf-token" content="([^"]+)"'
#     page_url: /
#     cache_duration: 300
#   - name: X-CSRFToken
#     source: cookie
#     cookie_name: csrftoken
```

### Commit

```
docs: add token_config examples to plugins and YAML template
```

---

## Task 11: Final verification

**Steps:**

```bash
# Run full test suite
uv run pytest tests/ -v

# Lint
uvx ruff check .
uvx ruff format --check .

# Type check
uvx ty check src/

# CLI smoke tests
gp http get https://httpbin.org/get --no-observe --no-browser-headers
gp http post https://httpbin.org/post --json '{"test": true}' --no-observe --no-browser-headers
gp observe go --help
gp http --help
```

### Commit (if needed)

```
fix: address verification findings
```

---

## Deferred items

**None.** All items from the original issue list are implemented. The only potential future enhancements are:

- Request/response timing data in HAR entries (would require tracking `Network.loadingFinished` events and computing elapsed time)
- `gp http` response body colorization (JSON syntax highlighting in terminal)
- Token extraction from response body (not just page body) — currently page source only

These are unplanned and should be tracked as separate issues if wanted.
