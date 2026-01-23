"""MFA (Multi-Factor Authentication) helpers.

This package provides utilities for handling common MFA flows:
- TOTP: Time-based One-Time Password generation and verification
- reCAPTCHA: Detection and handling of reCAPTCHA challenges
- Magic Links: Detection and extraction of magic link tokens
"""

from graftpunk.mfa.magiclink import (
    MagicLinkConfig,
    detect_magic_link,
    extract_magic_link_token,
    wait_for_magic_link,
)
from graftpunk.mfa.recaptcha import (
    ReCaptchaConfig,
    detect_recaptcha,
    get_recaptcha_type,
    wait_for_recaptcha_solution,
)
from graftpunk.mfa.totp import (
    generate_totp,
    get_totp_remaining_seconds,
    verify_totp,
)

__all__ = [
    # TOTP
    "generate_totp",
    "verify_totp",
    "get_totp_remaining_seconds",
    # reCAPTCHA
    "ReCaptchaConfig",
    "detect_recaptcha",
    "get_recaptcha_type",
    "wait_for_recaptcha_solution",
    # Magic Links
    "MagicLinkConfig",
    "detect_magic_link",
    "extract_magic_link_token",
    "wait_for_magic_link",
]
