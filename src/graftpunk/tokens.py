"""CSRF token and dynamic header extraction for authenticated sessions."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Literal

import requests

from graftpunk.logging import get_logger

LOG = get_logger(__name__)


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
    extraction: Literal["http", "browser", "auto"] = "auto"  # Extraction strategy

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
        value = session.cookies.get(token.cookie_name)
        if not value:
            raise ValueError(f"Cookie '{token.cookie_name}' not found in session")
        return value

    if token.source == "response_header":
        url = f"{base_url.rstrip('/')}{token.page_url}"
        resp = session.head(url, timeout=10, allow_redirects=True)
        value = resp.headers.get(token.response_header)
        if not value:
            raise ValueError(f"Header '{token.response_header}' not found in response from {url}")
        return value

    if token.source == "page":
        url = f"{base_url.rstrip('/')}{token.page_url}"
        resp = session.get(url, timeout=15)
        resp.raise_for_status()
        match = re.search(token.pattern, resp.text)  # type: ignore[arg-type]
        if not match:
            raise ValueError(f"Token pattern not found in {url}: {token.pattern}")
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
