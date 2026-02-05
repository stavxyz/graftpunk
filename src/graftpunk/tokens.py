"""CSRF token and dynamic header extraction for authenticated sessions."""

from __future__ import annotations

import asyncio
import re
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Literal, cast

import requests

from graftpunk.logging import get_logger

LOG = get_logger(__name__)

# Polling constants for browser-based token extraction.
# Tokens may not appear immediately (e.g. anti-bot challenges, lazy-rendered pages).
_TOKEN_POLL_ATTEMPTS = 6
_TOKEN_POLL_INTERVAL = 0.5  # seconds between attempts (total max: 3s)


class _BrowserExtractionNeeded(Exception):  # noqa: N818 — internal control flow signal, not an error
    """Raised when a token requires browser-based extraction.

    This is an internal signal used by ``extract_token()`` to tell
    ``prepare_session()`` that this token should be batched into the
    browser extraction pass. Not a user-facing error.
    """


async def nodriver_start(*, headless: bool = True) -> Any:
    """Start a nodriver browser instance.

    Thin wrapper to isolate the nodriver import for testability.
    """
    import nodriver

    return await nodriver.start(headless=headless)


def _deregister_nodriver_browser(browser: Any) -> None:
    """Remove browser from nodriver's global registry to prevent atexit noise."""
    try:
        from nodriver.core.util import get_registered_instances

        get_registered_instances().discard(browser)
    except Exception:  # noqa: BLE001 — best-effort cleanup
        LOG.debug("deregister_nodriver_browser_failed", exc_info=True)


async def _poll_for_tokens(
    tab: Any,
    tokens: list[Token],
    url: str,
    log_prefix: str,
) -> dict[str, str]:
    """Poll tab content for token patterns with retry logic.

    Args:
        tab: Nodriver tab to extract content from.
        tokens: List of tokens to extract from this page.
        url: Full URL (for logging).
        log_prefix: Log event prefix ("browser_token" or "login_token").

    Returns:
        Mapping of successfully extracted token names to values.
    """
    results: dict[str, str] = {}
    unmatched = list(tokens)

    for _attempt in range(_TOKEN_POLL_ATTEMPTS):
        content = await tab.get_content()
        still_unmatched = []

        for token in unmatched:
            match = re.search(token.pattern, content)  # type: ignore[arg-type]
            if match:
                results[token.name] = match.group(1)
                LOG.info(f"{log_prefix}_extracted", name=token.name, url=url)
            else:
                still_unmatched.append(token)

        unmatched = still_unmatched
        if not unmatched:
            break
        await tab.sleep(_TOKEN_POLL_INTERVAL)

    for token in unmatched:
        LOG.warning(f"{log_prefix}_pattern_not_found", name=token.name, url=url)

    return results


async def _extract_tokens_browser(
    session: requests.Session,
    tokens: list[Token],
    base_url: str,
) -> dict[str, str]:
    """Extract multiple tokens using a single headless browser session.

    Starts one headless nodriver browser, injects session cookies, and navigates
    to each unique page_url to extract tokens via regex. Tokens sharing a
    page_url are extracted from a single navigation.

    Args:
        session: Authenticated requests.Session with cookies to inject.
        tokens: List of Token configs to extract (typically source="page";
            other sources may not produce meaningful results with regex).
        base_url: Plugin's base URL for resolving relative page_url paths.

    Returns:
        Mapping of token name to extracted value. Missing keys indicate
        extraction failure for that specific token.
    """
    from graftpunk.session import inject_cookies_to_nodriver

    by_url: dict[str, list[Token]] = defaultdict(list)
    for token in tokens:
        by_url[token.page_url].append(token)

    browser = await nodriver_start(headless=True)
    try:
        tab = browser.main_tab
        injected, _skipped = await inject_cookies_to_nodriver(tab, session.cookies)
        LOG.debug("token_extraction_cookies_injected", count=injected)

        results: dict[str, str] = {}

        for page_url, token_group in by_url.items():
            url = f"{base_url.rstrip('/')}{page_url}"
            try:
                tab = await browser.get(url)
                extracted = await _poll_for_tokens(tab, token_group, url, "browser_token")
                results.update(extracted)
            except Exception as exc:  # noqa: BLE001 — per-URL isolation; nodriver raises varied exception types
                LOG.warning(
                    "browser_token_navigation_failed",
                    url=url,
                    error=str(exc),
                    exc_type=type(exc).__name__,
                    token_count=len(token_group),
                )
                continue

        return results
    finally:
        browser.stop()
        _deregister_nodriver_browser(browser)


async def extract_tokens_from_tab(
    tab: Any,
    tokens: list[Token],
    base_url: str,
) -> dict[str, str]:
    """Extract tokens using an already-open browser tab.

    Used during login to piggyback on the existing browser session
    rather than launching a separate browser for token extraction.

    Args:
        tab: An open nodriver tab with authenticated session.
        tokens: List of Token configs to extract (page-source tokens only).
        base_url: Plugin's base URL for resolving relative page_url paths.

    Returns:
        Mapping of token name to extracted value.
    """
    by_url: dict[str, list[Token]] = defaultdict(list)
    for token in tokens:
        if token.source == "page" and token.pattern:
            by_url[token.page_url].append(token)

    results: dict[str, str] = {}
    for page_url, token_group in by_url.items():
        url = f"{base_url.rstrip('/')}{page_url}"
        try:
            tab = await tab.browser.get(url)
            extracted = await _poll_for_tokens(tab, token_group, url, "login_token")
            results.update(extracted)
        except Exception as exc:  # noqa: BLE001 — per-URL isolation; best-effort during login
            LOG.warning("login_token_extraction_failed", url=url, error=str(exc))
            continue

    return results


@dataclass(frozen=True)
class Token:
    """Configuration for extracting a dynamic token from a web page or session."""

    name: str  # Header name to inject (e.g. "CSRFToken")
    source: Literal["page", "cookie", "response_header"]
    pattern: str | None = None  # Regex with capture group (for "page" source)
    cookie_name: str | None = None  # Cookie name (for "cookie" source)
    response_header: str | None = None  # Response header (for "response_header" source)
    page_url: str = "/"  # URL to fetch for extraction (for "page" source)
    cache_duration: float = 300  # Cache TTL in seconds
    extraction: Literal["http", "browser", "auto"] = "auto"
    # Extraction strategy (only applies to page and response_header sources):
    #   source="cookie"           -> extraction is ignored (always direct lookup)
    #   source="page"             -> "http": requests only, "browser": nodriver only,
    #                                "auto": try HTTP then fall back to browser
    #   source="response_header"  -> same as "page"

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("Token.name must be non-empty")
        if self.source not in ("page", "cookie", "response_header"):
            raise ValueError(
                f"Token.source must be 'page', 'cookie', or 'response_header', got {self.source!r}"
            )
        if self.source == "page" and not self.pattern:
            raise ValueError("Token with source='page' requires a pattern")
        if self.source == "cookie" and not self.cookie_name:
            raise ValueError("Token with source='cookie' requires cookie_name")
        if self.source == "response_header" and not self.response_header:
            raise ValueError("Token with source='response_header' requires response_header")
        if self.extraction not in ("http", "browser", "auto"):
            raise ValueError(
                f"Token.extraction must be 'http', 'browser', or 'auto', got {self.extraction!r}"
            )
        if self.cache_duration <= 0:
            raise ValueError("Token.cache_duration must be positive")

    @classmethod
    def from_meta_tag(
        cls,
        name: str,
        header: str,
        page_url: str = "/",
        cache_duration: float = 300,
        extraction: Literal["http", "browser", "auto"] = "auto",
    ) -> Token:
        """Create token config for HTML <meta name="..." content="..."> extraction."""
        return cls(
            name=header,
            source="page",
            pattern=rf'<meta\s+name=["\']?{re.escape(name)}["\']?\s+content=["\']([^"\']+)',
            page_url=page_url,
            cache_duration=cache_duration,
            extraction=extraction,
        )

    @classmethod
    def from_cookie(cls, cookie_name: str, header: str, cache_duration: float = 300) -> Token:
        """Create token config for cookie-based CSRF (e.g. Django csrftoken)."""
        return cls(
            name=header,
            source="cookie",
            cookie_name=cookie_name,
            cache_duration=cache_duration,
        )

    @classmethod
    def from_js_variable(
        cls,
        pattern: str,
        header: str,
        page_url: str = "/",
        cache_duration: float = 300,
        extraction: Literal["http", "browser", "auto"] = "auto",
    ) -> Token:
        """Create token config for JavaScript variable extraction."""
        return cls(
            name=header,
            source="page",
            pattern=pattern,
            page_url=page_url,
            cache_duration=cache_duration,
            extraction=extraction,
        )

    @classmethod
    def from_response_header(
        cls,
        response_header: str,
        request_header: str,
        page_url: str = "/",
        cache_duration: float = 300,
        extraction: Literal["http", "browser", "auto"] = "auto",
    ) -> Token:
        """Create token config for response header extraction."""
        return cls(
            name=request_header,
            source="response_header",
            response_header=response_header,
            page_url=page_url,
            cache_duration=cache_duration,
            extraction=extraction,
        )


@dataclass(frozen=True)
class TokenConfig:
    """Collection of token extraction rules for a plugin."""

    tokens: tuple[Token, ...]

    def __post_init__(self) -> None:
        if not self.tokens:
            raise ValueError("TokenConfig.tokens must be non-empty")
        names = [t.name for t in self.tokens]
        if len(names) != len(set(names)):
            dupes = [n for n in names if names.count(n) > 1]
            raise ValueError(f"TokenConfig has duplicate token names: {set(dupes)}")


@dataclass(frozen=True)
class CachedToken:
    """An extracted token value with TTL."""

    name: str
    value: str
    extracted_at: float
    ttl: float

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("CachedToken.name must be non-empty")
        if self.ttl <= 0:
            raise ValueError("CachedToken.ttl must be positive")

    @property
    def is_expired(self) -> bool:
        """Check if the cached token has exceeded its TTL."""
        return (time.time() - self.extracted_at) > self.ttl


def _run_browser_extraction(
    session: requests.Session,
    tokens: list[Token],
    base_url: str,
) -> dict[str, str]:
    """Run browser extraction, handling sync/async context detection.

    Args:
        session: Authenticated session with cookies.
        tokens: Tokens needing browser extraction.
        base_url: Plugin's base URL.

    Returns:
        Mapping of token name to extracted value.
    """
    coro = _extract_tokens_browser(session, tokens, base_url)

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # Sync context — suppress asyncio cleanup noise
        from graftpunk.logging import suppress_asyncio_noise

        with suppress_asyncio_noise():
            return asyncio.run(coro)
    else:
        # Async context — run in a thread to avoid nested asyncio.run()
        import concurrent.futures

        LOG.debug("browser_extraction_using_thread_pool", reason="running_event_loop_detected")
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(asyncio.run, coro)
            return cast("dict[str, str]", future.result())


def extract_token(session: requests.Session, token: Token, base_url: str) -> str:
    """Extract a token value using the configured strategy.

    Args:
        session: Authenticated requests.Session with cookies.
        token: Token extraction configuration.
        base_url: Plugin's base URL for relative page_url resolution.

    Returns:
        Extracted token value.

    Raises:
        ValueError: If token cannot be extracted via HTTP.
        _BrowserExtractionNeeded: If token needs browser extraction
            (extraction="browser", or extraction="auto" after HTTP failure).
        requests.RequestException: If HTTP extraction fails and
            extraction="http" (no browser fallback).
    """
    if token.source == "cookie":
        value = session.cookies.get(token.cookie_name)
        if not value:
            raise ValueError(f"Cookie '{token.cookie_name}' not found in session")
        return value

    # Browser-only: skip HTTP entirely
    if token.extraction == "browser":
        raise _BrowserExtractionNeeded(token.name)

    if token.source == "response_header":
        url = f"{base_url.rstrip('/')}{token.page_url}"
        try:
            resp = session.head(url, timeout=10, allow_redirects=True)
        except requests.RequestException:
            if token.extraction == "auto":
                raise _BrowserExtractionNeeded(token.name) from None
            raise
        value = resp.headers.get(token.response_header)
        if not value:
            raise ValueError(f"Header '{token.response_header}' not found in response from {url}")
        return value

    if token.source == "page":
        url = f"{base_url.rstrip('/')}{token.page_url}"
        try:
            resp = session.get(url, timeout=15)
            resp.raise_for_status()
        except requests.RequestException:
            if token.extraction == "auto":
                raise _BrowserExtractionNeeded(token.name) from None
            raise
        match = re.search(token.pattern, resp.text)  # type: ignore[arg-type]
        if not match:
            if token.extraction == "auto":
                raise _BrowserExtractionNeeded(token.name)
            raise ValueError(f"Token pattern not found in {url}: {token.pattern}")
        return match.group(1)

    raise ValueError(f"Unknown token source: {token.source}")


_CACHE_ATTR = "_gp_cached_tokens"
_CSRF_TOKENS_ATTR = "_gp_csrf_tokens"


def _inject_csrf_token(session: requests.Session, name: str, value: str) -> None:
    """Store a CSRF token for method-scoped injection in prepare_request().

    Tokens are stored on a separate session attribute rather than
    session.headers so they are only sent on mutation methods
    (POST/PUT/PATCH/DELETE), matching browser CSRF behavior.

    Args:
        session: The requests session to store the token on.
        name: Header name (e.g. "CSRFToken").
        value: Token value.
    """
    csrf_tokens: dict[str, str] = getattr(session, _CSRF_TOKENS_ATTR, {})
    csrf_tokens[name] = value
    setattr(session, _CSRF_TOKENS_ATTR, csrf_tokens)


def prepare_session(
    session: requests.Session,
    token_config: TokenConfig,
    base_url: str,
) -> requests.Session:
    """Extract all tokens and store for method-scoped injection.

    Uses a two-phase flow:
    1. Try cookie/HTTP extraction for each token; collect tokens that raise
       _BrowserExtractionNeeded into a deferred batch
    2. Extract all deferred tokens in a single headless browser session

    Tokens are stored in a separate attribute (not session.headers) so that
    GraftpunkSession.prepare_request() can inject them only on mutation
    methods (POST/PUT/PATCH/DELETE).

    Args:
        session: Authenticated requests.Session.
        token_config: Token extraction rules.
        base_url: Plugin's base URL.

    Returns:
        The same session with tokens ready for method-scoped injection.
    """
    cache: dict[str, CachedToken] = getattr(session, _CACHE_ATTR, {})
    browser_needed: list[Token] = []

    # Phase 1: Try non-browser extraction, collect browser-needed tokens
    for token in token_config.tokens:
        cached = cache.get(token.name)
        if cached:
            # EAFP: inject even if expired — if the server rejects with 403,
            # the retry path in plugin_commands clears cache and re-extracts
            _inject_csrf_token(session, token.name, cached.value)
            if cached.is_expired:
                LOG.debug("token_injecting_expired", name=token.name)
            continue

        try:
            value = extract_token(session, token, base_url)
            cache[token.name] = CachedToken(
                name=token.name,
                value=value,
                extracted_at=time.time(),
                ttl=token.cache_duration,
            )
            _inject_csrf_token(session, token.name, value)
            LOG.info("token_extracted", name=token.name, source=token.source)
        except _BrowserExtractionNeeded:
            LOG.info("token_needs_browser", name=token.name, source=token.source)
            browser_needed.append(token)
        except ValueError:
            LOG.exception("token_extraction_failed", name=token.name)
            raise

    # Phase 2: Batch browser extraction
    if browser_needed:
        LOG.info("browser_extraction_batch", count=len(browser_needed))
        results = _run_browser_extraction(session, browser_needed, base_url)

        for token in browser_needed:
            value = results.get(token.name)
            if value is None:
                raise ValueError(f"Browser extraction failed for token '{token.name}'")
            cache[token.name] = CachedToken(
                name=token.name,
                value=value,
                extracted_at=time.time(),
                ttl=token.cache_duration,
            )
            _inject_csrf_token(session, token.name, value)

    setattr(session, _CACHE_ATTR, cache)
    return session


def clear_cached_tokens(session: requests.Session) -> None:
    """Clear all cached tokens from a session (e.g. for retry after 403)."""
    if hasattr(session, _CACHE_ATTR):
        getattr(session, _CACHE_ATTR).clear()
