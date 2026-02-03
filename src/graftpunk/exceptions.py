"""Custom exceptions for graftpunk package."""


class GraftpunkError(Exception):
    """Base exception class for all graftpunk errors."""


class BrowserError(GraftpunkError):
    """Raised when browser automation or interaction fails."""


class ChromeDriverError(BrowserError):
    """Raised when ChromeDriver initialization or version mismatch occurs."""


class SessionExpiredError(GraftpunkError):
    """Raised when a saved session has expired or become invalid."""


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
