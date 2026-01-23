"""Custom exceptions for graftpunk package."""


class GraftpunkError(Exception):
    """Base exception for graftpunk package."""


class BrowserError(GraftpunkError):
    """Browser automation failed."""


class ChromeDriverError(BrowserError):
    """ChromeDriver initialization or version mismatch error."""


class SessionExpiredError(GraftpunkError):
    """Saved session has expired or is invalid."""


class SessionNotFoundError(GraftpunkError):
    """No cached session found."""


class EncryptionError(GraftpunkError):
    """Encryption or decryption failed."""


class StorageError(GraftpunkError):
    """Storage backend operation failed."""


class PluginError(GraftpunkError):
    """Plugin loading or execution failed."""


class KeepaliveError(GraftpunkError):
    """Keepalive operation failed."""


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
