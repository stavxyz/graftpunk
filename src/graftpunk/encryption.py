"""Session encryption using Fernet symmetric encryption.

This module provides encryption/decryption for session data using
Fernet (AES-128-CBC with HMAC authentication). Keys are sourced based
on the storage backend configuration:

- GRAFTPUNK_STORAGE_BACKEND=local: Local file (~/.config/graftpunk/.session_key),
  generated on first use
- GRAFTPUNK_STORAGE_BACKEND=supabase: Supabase Vault
  (secret name from GRAFTPUNK_SESSION_KEY_VAULT_NAME)
- any other backend (s3): the same local file, which must already exist

A caller that holds the key itself (a Worker holding it as a secret) passes it
as ``key=`` to :func:`encrypt_data` or :func:`decrypt_data` and no key source
is consulted.

Thread Safety:
    The encryption key is cached globally for performance (avoids repeated
    round-trips to Vault or filesystem). The cache is NOT thread-safe.
    This is acceptable for the current single-threaded CLI usage pattern.
    If using graftpunk in a multi-threaded application, external synchronization
    is required when calling get_encryption_key() or reset_encryption_key_cache().
"""

import binascii
import os
from typing import cast

from cryptography.fernet import Fernet

from graftpunk._distribution import install_command
from graftpunk.config import get_settings
from graftpunk.exceptions import EncryptionError
from graftpunk.logging import get_logger

LOG = get_logger(__name__)

# Cache the encryption key in memory to avoid repeated round-trips
_encryption_key_cache: bytes | None = None


def get_encryption_key() -> bytes:
    """Get encryption key for session data.

    Key source is determined by GRAFTPUNK_STORAGE_BACKEND:
    - "local" (default): Local file (~/.config/graftpunk/.session_key),
      generated on first use
    - "supabase": Supabase Vault (using GRAFTPUNK_SESSION_KEY_VAULT_NAME or default)
    - any other backend: the same local file, which must already exist

    The key is cached in memory after first retrieval to avoid
    repeated round-trips to Vault or filesystem.

    Returns:
        Fernet-compatible 32-byte key (base64 encoded).

    Raises:
        EncryptionError: If the key cannot be retrieved or generated, or the
            key file is missing for a backend other than ``local``.
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
    """Get the encryption key from the local key file, creating it only for local storage.

    The local backend keeps its sessions in the same config directory as the
    key file, so a key generated here lives exactly as long as the sessions it
    encrypts. Any other backend stores sessions somewhere that outlives this
    host's config directory: a key generated for it on an ephemeral host would
    encrypt sessions nothing can ever decrypt (#196), so a missing file is an
    error there.

    Returns:
        Fernet-compatible key bytes.

    Raises:
        EncryptionError: The key file is missing and the storage backend is not
            ``local``.
    """
    settings = get_settings()
    key_file = settings.config_dir / ".session_key"

    if key_file.exists():
        LOG.debug("using_encryption_key_from_file")
        return key_file.read_bytes()

    backend = settings.storage_backend.lower()
    if backend != "local":
        raise EncryptionError(
            f"No session encryption key at {key_file}, and GRAFTPUNK_STORAGE_BACKEND={backend} "
            "stores sessions where a key generated on this host could not follow them. "
            "Copy the key the other hosts use to that file (mode 0600), create a new one with "
            "python -c 'from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())', "
            "or pass the key as key= to encrypt_data, decrypt_data or cache_session."
        )

    # Generate new key and save it with secure permissions from creation
    key_file.parent.mkdir(parents=True, exist_ok=True)
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
            f"Install with: {install_command('supabase')}"
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

        # The RPC returns the decrypted secret value.  Supabase-py types
        # result.data as Any, but ty resolves it to object | list[object] |
        # None depending on the code path, so we cast through dict[str, ...]
        # to satisfy the type checker.
        secret_value = result.data
        if isinstance(secret_value, list) and len(secret_value) > 0:
            first = secret_value[0]
            if isinstance(first, dict):
                row = cast("dict[str, str]", first)
                secret_value = row.get("decrypted_secret", "")
            else:
                secret_value = ""
        elif isinstance(secret_value, dict):
            row_d = cast("dict[str, str]", secret_value)
            secret_value = row_d.get("decrypted_secret", "")

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


def _fernet(key: bytes | None) -> Fernet:
    """A Fernet for *key*, or for the configured key when *key* is None.

    Fernet() itself raises ValueError on a malformed key (wrong length, not
    url-safe base64, stray trailing newline). That is the single likeliest
    failure for a caller passing ``key=`` from a secret store, so it is mapped
    to EncryptionError rather than escaping as a bare ValueError that the
    documented contracts below do not mention.
    """
    fernet_key = key if key is not None else get_encryption_key()
    try:
        return Fernet(fernet_key)
    except (ValueError, TypeError) as exc:
        raise EncryptionError(
            "Invalid encryption key: a Fernet key must be 32 url-safe base64-encoded bytes."
        ) from exc


def encrypt_data(data: bytes, *, key: bytes | None = None) -> bytes:
    """Encrypt data using Fernet symmetric encryption.

    Args:
        data: Raw bytes to encrypt.
        key: Optional raw Fernet key. When given, encrypt with it directly,
            bypassing the configured key sources, the counterpart of
            :func:`decrypt_data`'s ``key``. When None (default), use the
            configured key.

    Returns:
        Encrypted bytes.

    Raises:
        EncryptionError: If the key is malformed, or no key was given and the
            configured source has none (see :func:`get_encryption_key`).
    """
    return _fernet(key).encrypt(data)


def decrypt_data(data: bytes, *, key: bytes | None = None) -> bytes:
    """Decrypt data using Fernet symmetric encryption.

    Args:
        data: Encrypted bytes.
        key: Optional raw Fernet key. When given, decrypt with it directly,
            bypassing the configured key sources (for environments without
            graftpunk's key file/vault, e.g. a Worker holding the key as a
            secret). When None (default), use the configured key.

    Returns:
        Decrypted raw bytes.

    Raises:
        EncryptionError: If the key is malformed, or decryption fails (wrong key
            or corrupted data).
    """
    from cryptography.fernet import InvalidToken

    fernet = _fernet(key)
    try:
        return fernet.decrypt(data)
    except InvalidToken as exc:
        raise EncryptionError(
            "Decryption failed. The session file may be corrupted "
            "or the encryption key has changed."
        ) from exc
