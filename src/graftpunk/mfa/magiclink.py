"""Magic link detection and extraction utilities.

Magic links are passwordless authentication URLs sent via email.
This module provides utilities for detecting and extracting tokens
from magic link URLs.
"""

import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlparse

if TYPE_CHECKING:
    from selenium.webdriver.remote.webdriver import WebDriver

from graftpunk.logging import get_logger

LOG = get_logger(__name__)

# Common magic link URL patterns
DEFAULT_MAGIC_LINK_PATTERNS = [
    # Common token parameter names
    r"[?&]token=([a-zA-Z0-9_-]+)",
    r"[?&]magic=([a-zA-Z0-9_-]+)",
    r"[?&]auth=([a-zA-Z0-9_-]+)",
    r"[?&]code=([a-zA-Z0-9_-]+)",
    r"[?&]otp=([a-zA-Z0-9_-]+)",
    # Path-based tokens
    r"/magic/([a-zA-Z0-9_-]+)",
    r"/auth/([a-zA-Z0-9_-]+)",
    r"/verify/([a-zA-Z0-9_-]+)",
    r"/login/([a-zA-Z0-9_-]+)",
]

# Common page indicators that magic link was sent
MAGIC_LINK_SENT_PATTERNS = [
    r"check your email",
    r"email.*sent",
    r"magic link.*sent",
    r"click.*link.*email",
    r"we.*sent.*email",
    r"verification.*email",
]


@dataclass
class MagicLinkConfig:
    """Configuration for magic link detection.

    Attributes:
        patterns: Regex patterns to extract tokens from URLs.
        timeout: Maximum seconds to wait for magic link.
        poll_interval: Seconds between checks.
        expected_domains: Optional list of expected magic link domains.
    """

    patterns: list[str] = field(default_factory=lambda: DEFAULT_MAGIC_LINK_PATTERNS.copy())
    timeout: int = 300  # 5 minutes
    poll_interval: float = 2.0
    expected_domains: list[str] = field(default_factory=list)


def detect_magic_link(
    url: str,
    config: MagicLinkConfig | None = None,
) -> bool:
    """Detect if a URL appears to be a magic link.

    Args:
        url: URL to check.
        config: Optional configuration with patterns and domains.

    Returns:
        True if the URL appears to be a magic link.

    Example:
        >>> url = "https://app.example.com/auth?token=abc123"
        >>> detect_magic_link(url)
        True
    """
    if config is None:
        config = MagicLinkConfig()

    # Check domain if expected_domains specified
    if config.expected_domains:
        parsed = urlparse(url)
        if not any(domain in parsed.netloc for domain in config.expected_domains):
            return False

    # Check patterns
    for pattern in config.patterns:
        if re.search(pattern, url, re.IGNORECASE):
            LOG.info("magic_link_detected", url=url[:50], pattern=pattern)
            return True

    return False


def extract_magic_link_token(
    url: str,
    config: MagicLinkConfig | None = None,
) -> str | None:
    """Extract the authentication token from a magic link URL.

    Args:
        url: Magic link URL.
        config: Optional configuration with patterns.

    Returns:
        Extracted token, or None if not found.

    Example:
        >>> url = "https://app.example.com/auth?token=abc123xyz"
        >>> extract_magic_link_token(url)
        'abc123xyz'
    """
    if config is None:
        config = MagicLinkConfig()

    # Try regex patterns first
    for pattern in config.patterns:
        match = re.search(pattern, url, re.IGNORECASE)
        if match:
            token = match.group(1)
            LOG.info("magic_link_token_extracted", token_length=len(token))
            return token

    # Fallback: try common query parameter names
    parsed = urlparse(url)
    params = parse_qs(parsed.query)

    for param_name in ["token", "magic", "auth", "code", "otp", "key"]:
        if param_name in params:
            token = params[param_name][0]
            LOG.info(
                "magic_link_token_extracted",
                param=param_name,
                token_length=len(token),
            )
            return token

    LOG.debug("magic_link_token_not_found", url=url[:50])
    return None


def detect_magic_link_sent_page(driver: "WebDriver") -> bool:
    """Detect if the current page indicates a magic link was sent.

    Args:
        driver: Selenium WebDriver instance.

    Returns:
        True if page appears to indicate magic link was sent.
    """
    try:
        page_source = driver.page_source.lower()
        page_text = driver.find_element("tag name", "body").text.lower()
    except Exception:
        return False

    for pattern in MAGIC_LINK_SENT_PATTERNS:
        if re.search(pattern, page_text, re.IGNORECASE):
            LOG.info("magic_link_sent_page_detected", pattern=pattern)
            return True
        if re.search(pattern, page_source, re.IGNORECASE):
            LOG.info("magic_link_sent_page_detected", pattern=pattern, source="html")
            return True

    return False


def wait_for_magic_link(
    check_function: Callable[[], str | None],
    config: MagicLinkConfig | None = None,
) -> str | None:
    """Wait for a magic link to arrive by polling a check function.

    This is a generic polling function that repeatedly calls the provided
    check function until it returns a magic link URL or timeout occurs.

    Args:
        check_function: Callable that returns a magic link URL or None.
            Could check email inbox, browser console, etc.
        config: Optional configuration for timeout and polling.

    Returns:
        Magic link URL if found, None if timeout.

    Example:
        >>> def check_email_for_magic_link():
        ...     # Custom logic to check email inbox
        ...     return None  # or return URL when found
        ...
        >>> url = wait_for_magic_link(check_email_for_magic_link)
        >>> if url:
        ...     token = extract_magic_link_token(url)
    """
    if config is None:
        config = MagicLinkConfig()

    start_time = time.time()
    LOG.info("waiting_for_magic_link", timeout=config.timeout)

    while time.time() - start_time < config.timeout:
        try:
            result = check_function()
            if result and detect_magic_link(result, config):
                elapsed = time.time() - start_time
                LOG.info("magic_link_found", elapsed_seconds=elapsed)
                return result
        except Exception as exc:
            LOG.warning("magic_link_check_error", error=str(exc))

        time.sleep(config.poll_interval)

    LOG.warning("magic_link_timeout", timeout=config.timeout)
    return None


def wait_for_magic_link_navigation(
    driver: "WebDriver",
    config: MagicLinkConfig | None = None,
) -> str | None:
    """Wait for browser to navigate to a magic link URL.

    Monitors the browser's current URL for magic link patterns.
    Useful when the user clicks a magic link in another tab/window.

    Args:
        driver: Selenium WebDriver instance.
        config: Optional configuration for timeout and patterns.

    Returns:
        Magic link URL if detected, None if timeout.
    """
    if config is None:
        config = MagicLinkConfig()

    def check_current_url() -> str | None:
        current = driver.current_url
        if detect_magic_link(current, config):
            return current
        return None

    return wait_for_magic_link(check_current_url, config)
