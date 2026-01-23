"""Custom exceptions for BSC package."""


class BSCError(Exception):
    """Base exception for BSC package."""


class BrowserError(BSCError):
    """Browser automation failed."""


class ChromeDriverError(BrowserError):
    """ChromeDriver initialization or version mismatch error."""


class SessionExpiredError(BSCError):
    """Saved session has expired or is invalid."""


class SessionNotFoundError(BSCError):
    """No cached session found."""


class EncryptionError(BSCError):
    """Encryption or decryption failed."""


class StorageError(BSCError):
    """Storage backend operation failed."""


class PluginError(BSCError):
    """Plugin loading or execution failed."""


class KeepaliveError(BSCError):
    """Keepalive operation failed."""


class MFARequiredError(BSCError):
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
