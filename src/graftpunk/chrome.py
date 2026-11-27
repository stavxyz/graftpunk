"""Chrome version detection utilities."""

import re
import subprocess
from collections.abc import Callable
from pathlib import Path

from graftpunk.exceptions import BrowserError, ChromeDriverError
from graftpunk.logging import get_logger

LOG = get_logger(__name__)


def get_chrome_version(major: bool = True) -> str:
    """Detect installed Chrome version.

    Args:
        major: If True, return only major version. Otherwise, return full version.

    Returns:
        Chrome version string.

    Raises:
        ChromeDriverError: If Chrome is not installed or version cannot be determined.
    """
    # Try common Chrome binary locations
    chrome_paths = [
        # macOS
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        # Linux
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/chromium-browser",
        "/usr/bin/chromium",
        # Windows
        "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
        "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
    ]

    for chrome_path in chrome_paths:
        if Path(chrome_path).exists():
            try:
                result = subprocess.run(  # noqa: S603
                    [chrome_path, "--version"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                version_output = result.stdout.strip()
                # Parse version from output like "Google Chrome 120.0.6099.71"
                match = re.search(r"(\d+(?:\.\d+)*)", version_output)
                if match:
                    full_version = match.group(1)
                    LOG.debug("detected_chrome_version", version=full_version, path=chrome_path)
                    if major:
                        return full_version.split(".")[0]
                    return full_version
            except (subprocess.TimeoutExpired, OSError) as exc:
                LOG.warning("chrome_version_check_failed", path=chrome_path, error=str(exc))
                continue

    # Try 'which' command for Chrome (path lookup is intentional)
    try:
        result = subprocess.run(
            ["which", "google-chrome", "chromium-browser", "chromium"],  # noqa: S603, S607
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            chrome_binary = result.stdout.strip().split("\n")[0]
            if chrome_binary:
                result = subprocess.run(  # noqa: S603
                    [chrome_binary, "--version"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                match = re.search(r"(\d+(?:\.\d+)*)", result.stdout)
                if match:
                    full_version = match.group(1)
                    if major:
                        return full_version.split(".")[0]
                    return full_version
    except (subprocess.TimeoutExpired, OSError):
        pass

    raise ChromeDriverError("Chrome not found. Please install Google Chrome or Chromium.")


def chromedriver_version(read: Callable[..., str] | None = None) -> str | None:
    """The ChromeDriver version to ask webdriver-manager for: the installed
    Chrome's full version, or None when it cannot be read.

    webdriver-manager 4.0.2 and 4.1.2 pick the last known-good ChromeDriver
    whose version string contains the first three parts of the version given
    (``get_url_for_version_and_platform``), so a bare major matches any build
    holding those digits: "158" matched "160.0.8158.0", a ChromeDriver Chrome
    158 refuses. The full version matches the same build only. The version is
    read once: the major comes from the same ``--version`` output, so it cannot
    be read when the full one cannot. *read* is the version reader,
    :func:`get_chrome_version` by default.
    """
    read = read or get_chrome_version
    try:
        return read(major=False)
    except ChromeDriverError as exc:
        LOG.debug("chrome_version_for_driver_unread", error=str(exc))
        return None


def no_chromedriver_error(version: str | None, exc: BaseException) -> BrowserError | None:
    """The :class:`BrowserError` for webdriver-manager's failure *exc* when it has
    no ChromeDriver for Chrome *version*, else None. webdriver-manager 4.0.2
    raises a bare ``Exception`` reading "No such driver version", and 4.1.2 a
    ``ValueError`` reading "Could not find a compatible chromedriver version";
    both are matched by their text. Asking again with the major alone is not the
    way out: that picks a ChromeDriver for another build."""
    text = str(exc)
    if "No such driver version" not in text and "Could not find a compatible" not in text:
        return None
    installed = f"the installed Chrome {version}" if version else "the installed Chrome"
    return BrowserError(
        f"webdriver-manager has no ChromeDriver for {installed} ({exc}). Update Chrome to "
        "a current release, which a ChromeDriver is published for, or use the nodriver "
        'backend (backend="nodriver"), which needs no ChromeDriver.'
    )
