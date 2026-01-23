"""TOTP (Time-based One-Time Password) utilities.

This module provides TOTP generation and verification using pyotp.
TOTP is the most common form of MFA used by authenticator apps like
Google Authenticator, Authy, and 1Password.
"""

import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pyotp

from bsc.logging import get_logger

LOG = get_logger(__name__)


def _get_totp(secret: str) -> "pyotp.TOTP":
    """Get a TOTP object from the secret.

    Args:
        secret: Base32-encoded TOTP secret.

    Returns:
        pyotp.TOTP instance.

    Raises:
        ValueError: If secret is invalid.
    """
    try:
        import pyotp
    except ImportError as exc:
        raise ImportError(
            "pyotp package is required for TOTP. Install with: pip install pyotp"
        ) from exc

    try:
        return pyotp.TOTP(secret)
    except Exception as exc:
        raise ValueError(f"Invalid TOTP secret: {exc}") from exc


def generate_totp(secret: str) -> str:
    """Generate a TOTP code from a secret.

    Args:
        secret: Base32-encoded TOTP secret (from authenticator setup).

    Returns:
        6-digit TOTP code as a string.

    Raises:
        ValueError: If secret is invalid.

    Example:
        >>> secret = "JBSWY3DPEHPK3PXP"  # Example secret
        >>> code = generate_totp(secret)
        >>> print(code)  # e.g., "123456"
    """
    totp = _get_totp(secret)
    code = totp.now()
    LOG.debug("totp_generated", code_length=len(code))
    return code


def verify_totp(secret: str, code: str, valid_window: int = 1) -> bool:
    """Verify a TOTP code against a secret.

    Args:
        secret: Base32-encoded TOTP secret.
        code: 6-digit TOTP code to verify.
        valid_window: Number of 30-second windows to check before/after.
            Default of 1 allows for clock skew.

    Returns:
        True if the code is valid, False otherwise.

    Example:
        >>> secret = "JBSWY3DPEHPK3PXP"
        >>> code = generate_totp(secret)
        >>> verify_totp(secret, code)
        True
    """
    totp = _get_totp(secret)
    is_valid = totp.verify(code, valid_window=valid_window)
    LOG.debug("totp_verified", is_valid=is_valid)
    return is_valid


def get_totp_remaining_seconds() -> int:
    """Get seconds remaining until the current TOTP period expires.

    TOTP codes change every 30 seconds. This returns how many seconds
    remain in the current period, useful for waiting until a fresh code.

    Returns:
        Seconds remaining (0-29).

    Example:
        >>> remaining = get_totp_remaining_seconds()
        >>> if remaining < 5:
        ...     time.sleep(remaining + 1)  # Wait for fresh code
        >>> code = generate_totp(secret)
    """
    return 30 - int(time.time() % 30)


def wait_for_fresh_totp(secret: str, min_validity_seconds: int = 5) -> str:
    """Wait for a fresh TOTP code with sufficient validity time.

    If the current TOTP period is about to expire, waits for the next
    period before generating a code. This ensures the code won't expire
    before it can be used.

    Args:
        secret: Base32-encoded TOTP secret.
        min_validity_seconds: Minimum seconds the code should be valid.
            If current period has less time remaining, waits for next period.

    Returns:
        6-digit TOTP code with at least min_validity_seconds remaining.

    Example:
        >>> secret = "JBSWY3DPEHPK3PXP"
        >>> code = wait_for_fresh_totp(secret, min_validity_seconds=10)
        >>> # Code is guaranteed to be valid for at least 10 more seconds
    """
    remaining = get_totp_remaining_seconds()

    if remaining < min_validity_seconds:
        wait_time = remaining + 1  # Wait for next period
        LOG.info(
            "waiting_for_fresh_totp",
            remaining=remaining,
            wait_time=wait_time,
        )
        time.sleep(wait_time)

    code = generate_totp(secret)
    LOG.info(
        "generated_fresh_totp",
        remaining_seconds=get_totp_remaining_seconds(),
    )
    return code
