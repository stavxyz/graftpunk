"""Chrome version detection utilities."""

import re
import subprocess
from pathlib import Path

from graftpunk.exceptions import ChromeDriverError
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
