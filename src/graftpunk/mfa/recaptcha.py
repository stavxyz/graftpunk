"""reCAPTCHA detection and handling utilities.

This module provides utilities for detecting and handling reCAPTCHA
challenges on web pages. It supports both reCAPTCHA v2 and v3.
"""

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from selenium.webdriver.remote.webdriver import WebDriver

from graftpunk.logging import get_logger

LOG = get_logger(__name__)

# Common reCAPTCHA selectors
RECAPTCHA_SELECTORS = [
    # reCAPTCHA v2 iframe
    'iframe[src*="recaptcha"]',
    'iframe[title*="reCAPTCHA"]',
    # reCAPTCHA v2 div container
    ".g-recaptcha",
    "div[data-sitekey]",
    # reCAPTCHA v3 badge (usually hidden)
    ".grecaptcha-badge",
    # Invisible reCAPTCHA
    'div[class*="grecaptcha"]',
]

# Selectors indicating reCAPTCHA has been solved
RECAPTCHA_SOLVED_SELECTORS = [
    # v2 checkbox checked
    ".recaptcha-checkbox-checked",
    'span.recaptcha-checkbox-checkmark[style*="opacity: 1"]',
    # Response token present (both v2 and v3)
    'textarea[name="g-recaptcha-response"]:not([value=""])',
]


@dataclass
class ReCaptchaConfig:
    """Configuration for reCAPTCHA detection.

    Attributes:
        timeout: Maximum seconds to wait for reCAPTCHA solution.
        poll_interval: Seconds between solution checks.
        detect_invisible: Whether to detect invisible/v3 reCAPTCHA.
    """

    timeout: int = 120
    poll_interval: float = 1.0
    detect_invisible: bool = True


def detect_recaptcha(driver: "WebDriver") -> bool:
    """Detect if reCAPTCHA is present on the current page.

    Args:
        driver: Selenium WebDriver instance.

    Returns:
        True if reCAPTCHA is detected, False otherwise.

    Example:
        >>> if detect_recaptcha(driver):
        ...     print("reCAPTCHA detected!")
        ...     wait_for_recaptcha_solution(driver)
    """
    from selenium.common.exceptions import NoSuchElementException
    from selenium.webdriver.common.by import By

    for selector in RECAPTCHA_SELECTORS:
        try:
            elements = driver.find_elements(By.CSS_SELECTOR, selector)
            if elements:
                LOG.info("recaptcha_detected", selector=selector, count=len(elements))
                return True
        except NoSuchElementException:
            continue

    LOG.debug("recaptcha_not_detected")
    return False


def get_recaptcha_type(driver: "WebDriver") -> str | None:
    """Determine the type of reCAPTCHA on the page.

    Args:
        driver: Selenium WebDriver instance.

    Returns:
        "v2" for visible checkbox reCAPTCHA,
        "v2_invisible" for invisible v2,
        "v3" for reCAPTCHA v3,
        None if no reCAPTCHA detected.
    """
    from selenium.common.exceptions import NoSuchElementException
    from selenium.webdriver.common.by import By

    try:
        # Check for v2 visible checkbox
        v2_checkbox = driver.find_elements(By.CSS_SELECTOR, ".g-recaptcha")
        for elem in v2_checkbox:
            size = elem.get_attribute("data-size")
            if size == "invisible":
                LOG.info("recaptcha_type_detected", type="v2_invisible")
                return "v2_invisible"
            LOG.info("recaptcha_type_detected", type="v2")
            return "v2"

        # Check for v3 badge
        v3_badge = driver.find_elements(By.CSS_SELECTOR, ".grecaptcha-badge")
        if v3_badge:
            LOG.info("recaptcha_type_detected", type="v3")
            return "v3"

        # Check for recaptcha iframe (could be v2 or v2_invisible)
        iframes = driver.find_elements(By.CSS_SELECTOR, 'iframe[src*="recaptcha"]')
        if iframes:
            LOG.info("recaptcha_type_detected", type="v2")
            return "v2"

    except NoSuchElementException:
        pass

    LOG.debug("recaptcha_type_not_determined")
    return None


def is_recaptcha_solved(driver: "WebDriver") -> bool:
    """Check if reCAPTCHA has been solved.

    Args:
        driver: Selenium WebDriver instance.

    Returns:
        True if reCAPTCHA appears to be solved, False otherwise.
    """
    from selenium.common.exceptions import NoSuchElementException
    from selenium.webdriver.common.by import By

    for selector in RECAPTCHA_SOLVED_SELECTORS:
        try:
            elements = driver.find_elements(By.CSS_SELECTOR, selector)
            if elements:
                LOG.info("recaptcha_solved", selector=selector)
                return True
        except NoSuchElementException:
            continue

    # Also check for response token via JavaScript
    try:
        response = driver.execute_script(
            "return document.querySelector('textarea[name=\"g-recaptcha-response\"]')?.value"
        )
        if response:
            LOG.info("recaptcha_solved_via_token")
            return True
    except Exception as exc:  # noqa: S110
        LOG.debug("recaptcha_token_check_failed", error=str(exc))

    return False


def wait_for_recaptcha_solution(
    driver: "WebDriver",
    config: ReCaptchaConfig | None = None,
) -> bool:
    """Wait for reCAPTCHA to be solved (manually or by service).

    This function polls the page waiting for the reCAPTCHA challenge
    to be solved. It's useful when using manual solving or third-party
    CAPTCHA solving services.

    Args:
        driver: Selenium WebDriver instance.
        config: Optional configuration for timeout and polling.

    Returns:
        True if reCAPTCHA was solved within timeout, False otherwise.

    Example:
        >>> if detect_recaptcha(driver):
        ...     print("Please solve the CAPTCHA...")
        ...     if wait_for_recaptcha_solution(driver, ReCaptchaConfig(timeout=60)):
        ...         print("CAPTCHA solved!")
        ...     else:
        ...         print("CAPTCHA timeout")
    """
    if config is None:
        config = ReCaptchaConfig()

    start_time = time.time()
    LOG.info("waiting_for_recaptcha_solution", timeout=config.timeout)

    while time.time() - start_time < config.timeout:
        if is_recaptcha_solved(driver):
            elapsed = time.time() - start_time
            LOG.info("recaptcha_solution_found", elapsed_seconds=elapsed)
            return True

        time.sleep(config.poll_interval)

    LOG.warning("recaptcha_solution_timeout", timeout=config.timeout)
    return False


def get_recaptcha_response(driver: "WebDriver") -> str | None:
    """Get the reCAPTCHA response token from the page.

    Args:
        driver: Selenium WebDriver instance.

    Returns:
        The reCAPTCHA response token, or None if not available.
    """
    try:
        response = driver.execute_script(
            "return document.querySelector('textarea[name=\"g-recaptcha-response\"]')?.value"
        )
        if response:
            LOG.debug("recaptcha_response_retrieved", length=len(response))
            return response
    except Exception as exc:
        LOG.warning("recaptcha_response_retrieval_failed", error=str(exc))

    return None
