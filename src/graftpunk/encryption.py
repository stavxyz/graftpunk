"""Session encryption using Fernet symmetric encryption.

This module provides encryption/decryption for session data using
Fernet (AES-128-CBC with HMAC authentication). Keys are sourced based
on the storage backend configuration:

- GRAFTPUNK_STORAGE_BACKEND=local: Local file (~/.config/graftpunk/.session_key)
- GRAFTPUNK_STORAGE_BACKEND=supabase: Supabase Vault
  (secret name from GRAFTPUNK_SESSION_KEY_VAULT_NAME)

Thread Safety:
    The encryption key is cached globally for performance (avoids repeated
    round-trips to Vault or filesystem). The cache is NOT thread-safe.
    This is acceptable for the current single-threaded CLI usage pattern.
    If using graftpunk in a multi-threaded application, external synchronization
    is required when calling get_encryption_key() or reset_encryption_key_cache().
"""

import binascii
import os

from cryptography.fernet import Fernet

from graftpunk.config import get_settings
from graftpunk.exceptions import EncryptionError
from graftpunk.logging import get_logger

LOG = get_logger(__name__)

# Cache the encryption key in memory to avoid repeated round-trips
_encryption_key_cache: bytes | None = None


def get_encryption_key() -> bytes:
    """Get encryption key for session data.

    Key source is determined by GRAFTPUNK_STORAGE_BACKEND:
    - "local" (default): Local file (~/.config/graftpunk/.session_key)
    - "supabase": Supabase Vault (using GRAFTPUNK_SESSION_KEY_VAULT_NAME or default)

    The key is cached in memory after first retrieval to avoid
    repeated round-trips to Vault or filesystem.

    Returns:
        Fernet-compatible 32-byte key (base64 encoded).

    Raises:
        EncryptionError: If key cannot be retrieved or generated.
    """
    global _encryption_key_cache

    if _encryption_key_cache is not None:
        return _encryption_key_cache

    _encryption_key_cache = _load_encryption_key()
    return _encryption_key_cache


def _load_encryption_key() -> bytes:
    """Load encryption key based on storage backend configuration.

    Returns:
        Fernet-compatible key bytes.
    """
    settings = get_settings()
    storage_backend = settings.storage_backend.lower()

    if storage_backend == "supabase":
        return _get_key_from_supabase_vault()

    return _get_key_from_file()


def _get_key_from_file() -> bytes:
    """Get or create encryption key from local file.

    Returns:
        Fernet-compatible key bytes.
    """
    settings = get_settings()
    key_file = settings.config_dir / ".session_key"

    if key_file.exists():
        LOG.debug("using_encryption_key_from_file")
        return key_file.read_bytes()

    # Generate new key and save it with secure permissions from creation
    key = Fernet.generate_key()
    fd = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(key)
    LOG.info("generated_new_session_encryption_key")
    return key


def _get_key_from_supabase_vault() -> bytes:
    """Fetch encryption key from Supabase Vault.

    Uses the vault.decrypted_secrets view to read the secret.
    The secret name is configured via GRAFTPUNK_SESSION_KEY_VAULT_NAME env var,
    defaulting to "session-encryption-key".

    Returns:
        Fernet-compatible key bytes.

    Raises:
        EncryptionError: If Vault fetch fails or key is invalid.
    """
    try:
        # Import here to avoid circular imports and allow local-only usage
        from supabase import create_client
    except ImportError as exc:
        raise EncryptionError(
            "supabase package is required for Supabase Vault. "
            "Install with: pip install graftpunk[supabase]"
        ) from exc

    settings = get_settings()
    vault_name = settings.session_key_vault_name

    if not settings.supabase_url or not settings.supabase_service_key:
        raise EncryptionError(
            "GRAFTPUNK_SUPABASE_URL and GRAFTPUNK_SUPABASE_SERVICE_KEY required "
            "when GRAFTPUNK_STORAGE_BACKEND=supabase"
        )

    LOG.info("fetching_encryption_key_from_vault", vault_name=vault_name)

    try:
        # Normalize URL
        normalized_url = settings.supabase_url.rstrip("/") + "/"
        client = create_client(
            normalized_url,
            settings.supabase_service_key.get_secret_value(),
        )

        # Query the decrypted_secrets view in vault schema
        # This requires the service role key (not anon key)
        result = client.rpc(
            "get_vault_secret",
            {"secret_name": vault_name},
        ).execute()

        if not result.data:
            raise EncryptionError(
                f"Encryption key '{vault_name}' not found in Supabase Vault. "
                "Please create it with: "
                "SELECT vault.create_secret('base64-key', 'session-encryption-key');"
            )

        # The RPC returns the decrypted secret value
        secret_value = result.data
        if isinstance(secret_value, list) and len(secret_value) > 0:
            secret_value = secret_value[0].get("decrypted_secret", "")
        elif isinstance(secret_value, dict):
            secret_value = secret_value.get("decrypted_secret", "")

        if not secret_value:
            raise EncryptionError(f"Empty encryption key returned from Vault for '{vault_name}'")

        # Validate it's a valid Fernet key (base64-encoded 32 bytes)
        key: bytes
        if isinstance(secret_value, str):
            key = secret_value.encode()
        elif isinstance(secret_value, bytes):
            key = secret_value
        else:
            raise EncryptionError(
                f"Unexpected type for encryption key from Vault: {type(secret_value)}"
            )

        try:
            Fernet(key)  # Validate key format
        except (ValueError, TypeError, binascii.Error) as e:
            raise EncryptionError(
                f"Invalid encryption key in Vault '{vault_name}': {e}. "
                "Key must be a valid Fernet key (base64-encoded 32 bytes). "
                "Generate with: python -c "
                "'from cryptography.fernet import Fernet; "
                "print(Fernet.generate_key().decode())'"
            ) from e

        LOG.info("using_encryption_key_from_vault", vault_name=vault_name)
        return key

    except EncryptionError:
        raise
    except Exception as e:
        raise EncryptionError(f"Failed to fetch encryption key from Supabase Vault: {e}") from e


def reset_encryption_key_cache() -> None:
    """Reset the encryption key cache.

    Useful for testing or when rotating keys.
    """
    global _encryption_key_cache
    _encryption_key_cache = None


def encrypt_data(data: bytes) -> bytes:
    """Encrypt data using Fernet symmetric encryption.

    Args:
        data: Raw bytes to encrypt.

    Returns:
        Encrypted bytes.
    """
    key = get_encryption_key()
    fernet = Fernet(key)
    return fernet.encrypt(data)


def decrypt_data(data: bytes) -> bytes:
    """Decrypt data using Fernet symmetric encryption.

    Args:
        data: Encrypted bytes.

    Returns:
        Decrypted raw bytes.

    Raises:
        EncryptionError: If decryption fails (wrong key or corrupted data).
    """
    from cryptography.fernet import InvalidToken

    key = get_encryption_key()
    fernet = Fernet(key)
    try:
        return fernet.decrypt(data)
    except InvalidToken as exc:
        raise EncryptionError(
            "Decryption failed. The session file may be corrupted "
            "or the encryption key has changed."
        ) from exc
