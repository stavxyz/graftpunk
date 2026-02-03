"""Stealth browser session using undetected-chromedriver.

Combines multiple anti-detection techniques:
1. undetected-chromedriver - Patches ChromeDriver binary to avoid detection
2. selenium-stealth - Injects JavaScript to hide automation indicators
3. Persistent profile - Builds trust score over time with reCAPTCHA
"""

import platform as platform_mod
from pathlib import Path

import undetected_chromedriver as uc
from selenium_stealth import stealth

from graftpunk.chrome import get_chrome_version
from graftpunk.config import get_settings
from graftpunk.exceptions import ChromeDriverError
from graftpunk.logging import get_logger

LOG = get_logger(__name__)


def get_profile_dir() -> Path:
    """Get persistent Chrome profile directory.

    Returns:
        Path to Chrome profile directory (~/.config/graftpunk/chrome_profile).
    """
    return get_settings().config_dir / "chrome_profile"


def _get_platform_fingerprint() -> tuple[str, str, str]:
    """Get consistent platform/vendor/renderer fingerprint for current OS.

    Returns:
        Tuple of (platform, vendor, renderer) that match the current operating system.
        Using mismatched values (e.g., Win32 platform with macOS GPU) is a major
        bot detection signal.
    """
    system = platform_mod.system()
    if system == "Darwin":
        # macOS - Use Apple GPU
        return ("MacIntel", "Apple Inc.", "Apple GPU")
    elif system == "Windows":
        # Windows - Use common Intel GPU with DirectX
        return ("Win32", "Google Inc.", "ANGLE (Intel, Intel(R) UHD Graphics Direct3D11)")
    else:
        # Linux - Use Mesa
        return ("Linux x86_64", "Google Inc.", "Mesa Intel(R) UHD Graphics")


def create_stealth_driver(
    headless: bool = False,
    profile_dir: Path | None = None,
) -> uc.Chrome:
    """Create a stealth Chrome driver with anti-detection measures.

    Combines three techniques:
    1. undetected-chromedriver - Patched ChromeDriver binary
    2. selenium-stealth - JavaScript property injection
    3. Persistent profile - Session/cookie reuse for trust building

    Args:
        headless: Run in headless mode (less stealthy, use with caution).
        profile_dir: Custom profile directory (default: ~/.config/graftpunk/chrome_profile).

    Returns:
        Configured undetected Chrome driver with stealth settings applied.
    """
    if profile_dir is None:
        profile_dir = get_profile_dir()

    profile_dir.mkdir(parents=True, exist_ok=True)
    LOG.info("creating_stealth_driver", profile_dir=str(profile_dir), headless=headless)

    # Configure undetected-chromedriver options
    options = uc.ChromeOptions()

    # Anti-detection options
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--no-first-run")
    options.add_argument("--no-service-autorun")
    options.add_argument("--password-store=basic")

    # Realistic window size (most common desktop resolution)
    options.add_argument("--window-size=1920,1080")

    if headless:
        options.add_argument("--headless=new")

    # Detect Chrome version for matching ChromeDriver
    try:
        chrome_version = int(get_chrome_version(major=True))
        LOG.info("detected_chrome_version_for_driver", version=chrome_version)
    except (ChromeDriverError, ValueError) as exc:
        LOG.warning("chrome_version_detection_failed", error=str(exc))
        chrome_version = None

    # Create undetected driver
    # Pass user_data_dir directly (recommended by undetected-chromedriver)
    # Pass version_main to ensure matching ChromeDriver is used
    driver = uc.Chrome(
        options=options,
        user_data_dir=str(profile_dir),
        version_main=chrome_version,
    )

    # Resize the initial window (decoy tab) to match login window size
    # undetected-chromedriver opens a blank window that goes to google.com
    # We make it the same size as our actual login window instead of full screen
    driver.set_window_size(1920, 1080)
    driver.set_window_position(0, 0)

    # Note: We do NOT inject CDP code to hide navigator.webdriver
    # On Chrome 143+, navigator.webdriver is set at a level that JavaScript cannot override
    LOG.info("skipping_cdp_injection_chrome_143_limitation")

    # Get platform-specific fingerprint
    plat, webgl_vendor, renderer = _get_platform_fingerprint()
    LOG.info(
        "using_platform_fingerprint",
        platform=plat,
        vendor=webgl_vendor,
        renderer=renderer,
    )

    # Apply selenium-stealth for additional JavaScript-level anti-detection
    stealth(
        driver,
        languages=["en-US", "en"],
        vendor=webgl_vendor,
        platform=plat,
        webgl_vendor=webgl_vendor,
        renderer=renderer,
        fix_hairline=True,
    )

    LOG.info("stealth_driver_created", navigator_webdriver_limitation=True)
    return driver
