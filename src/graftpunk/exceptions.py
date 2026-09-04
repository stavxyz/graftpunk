"""Custom exceptions for graftpunk package."""

from __future__ import annotations

from collections.abc import Sequence


class GraftpunkError(Exception):
    """Base exception class for all graftpunk errors."""


class BrowserError(GraftpunkError):
    """Raised when browser automation or interaction fails."""


class ChromeDriverError(BrowserError):
    """Raised when ChromeDriver initialization or version mismatch occurs."""


class SessionExpiredError(GraftpunkError):
    """A *cached* session could not be loaded: expired TTL, or it failed to decrypt/deserialize.

    Raised by the cache and storage layers. For a session that loads fine but is
    no longer authenticated against the site, see :class:`SessionInvalidatedError`.
    """


class SessionNotFoundError(GraftpunkError):
    """Raised when no cached session can be found for the requested key."""


class EncryptionError(GraftpunkError):
    """Raised when encryption or decryption operations fail."""


class StorageError(GraftpunkError):
    """Raised when a storage backend operation fails."""


class PluginError(GraftpunkError):
    """Raised when plugin loading or execution fails."""


class CommandError(PluginError):
    """Expected command failure with a user-facing message.

    Plugin authors raise this for anticipated errors (validation failures,
    business rule violations). The framework displays user_message cleanly
    without traceback.

    Example:
        raise CommandError("Amount must be positive")
    """

    def __init__(self, user_message: str) -> None:
        self.user_message = user_message
        super().__init__(user_message)


class KeepaliveError(GraftpunkError):
    """Raised when a keepalive operation fails."""


class MFARequiredError(GraftpunkError):
    """Multi-factor authentication is required to proceed.

    This exception should be raised by site plugins when login fails
    due to an MFA challenge that cannot be automatically resolved.

    Attributes:
        mfa_type: Type of MFA required (e.g., 'totp', 'sms', 'email', 'push').
        message: Human-readable message describing the MFA requirement.
    """

    def __init__(self, message: str = "MFA is required", mfa_type: str | None = None) -> None:
        """Initialize MFARequiredError.

        Args:
            message: Human-readable error message.
            mfa_type: Type of MFA required (optional).
        """
        super().__init__(message)
        self.mfa_type = mfa_type
        self.message = message


class TokenExtractionError(GraftpunkError, ValueError):
    """A token could not be extracted from the page, response or cookie jar.

    Also a ``ValueError``, permanently: earlier releases raised plain
    ``ValueError`` here, and dropping the base would be a fresh breaking change
    with nothing to gain. Catch the subtypes to tell a stale session from
    configuration drift:

    - :class:`SessionInvalidatedError` — re-login and retry.
    - :class:`TokenPatternMismatchError` — the site changed; fix the Token config.

    Browser-mode extraction failures raise this base class: by the time the
    batch comes back empty the cause (a redirect to the login page, or a
    pattern that no longer matches the rendered page) is not distinguishable
    without inspecting the page, so neither subtype would be honest.
    """


class SessionInvalidatedError(TokenExtractionError):
    """A cookie the token is read from is absent from the session.

    Raised for ``source="cookie"`` tokens. That is the one extraction failure
    graftpunk can attribute to the session itself; it is recoverable by a
    fresh login, not by retrying with the same session. (A mistyped
    ``cookie_name`` produces the same signal — check the Token config if a
    re-login does not help.) Distinct from :class:`SessionExpiredError`, which
    is about the *cached* session failing to load at all.
    """


class TokenPatternMismatchError(TokenExtractionError):
    """The configured page pattern or response header is not in what the site returned.

    Raised for ``source="page"`` and ``source="response_header"`` tokens in
    HTTP mode. The most likely cause is that the site changed and the Token
    configuration needs an update; a missing header can occasionally mean the
    server no longer serves the authenticated response, so if updating the
    config does not help, try a fresh login.
    """


class AmbiguousSessionError(GraftpunkError):
    """Several cached sessions match a plugin and none was selected.

    Pick one with ``--session``, the ``GRAFTPUNK_SESSION`` env var, or
    ``gp session use``.
    """

    def __init__(self, base_name: str, candidates: Sequence[str]) -> None:
        self.base_name = base_name
        self.candidates = list(candidates)
        names = ", ".join(candidates)
        super().__init__(f"Several sessions cached for '{base_name}': {names}. Pick one.")
