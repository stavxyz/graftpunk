"""Tests for encryption module."""

import pytest
from cryptography.fernet import Fernet

from bsc.encryption import (
    decrypt_data,
    encrypt_data,
    get_encryption_key,
    reset_encryption_key_cache,
)
from bsc.exceptions import EncryptionError


class TestEncryption:
    """Tests for encryption functions."""

    def setup_method(self) -> None:
        """Reset encryption key cache before each test."""
        reset_encryption_key_cache()

    def test_encrypt_decrypt_roundtrip(self, tmp_path, monkeypatch):
        """Test that encrypt/decrypt is a valid roundtrip."""
        # Set up temporary config directory
        monkeypatch.setenv("BSC_CONFIG_DIR", str(tmp_path))
        monkeypatch.setenv("BSC_STORAGE_BACKEND", "local")
        reset_encryption_key_cache()

        # Reset settings to pick up new config
        from bsc.config import reset_settings

        reset_settings()

        original_data = b"Hello, World! This is test data."

        # Encrypt
        encrypted = encrypt_data(original_data)
        assert encrypted != original_data
        assert len(encrypted) > len(original_data)

        # Decrypt
        decrypted = decrypt_data(encrypted)
        assert decrypted == original_data

    def test_encryption_key_is_cached(self, tmp_path, monkeypatch):
        """Test that encryption key is cached."""
        monkeypatch.setenv("BSC_CONFIG_DIR", str(tmp_path))
        monkeypatch.setenv("BSC_STORAGE_BACKEND", "local")
        reset_encryption_key_cache()

        from bsc.config import reset_settings

        reset_settings()

        key1 = get_encryption_key()
        key2 = get_encryption_key()

        assert key1 is key2  # Same object (cached)

    def test_encryption_key_is_valid_fernet_key(self, tmp_path, monkeypatch):
        """Test that generated key is a valid Fernet key."""
        monkeypatch.setenv("BSC_CONFIG_DIR", str(tmp_path))
        monkeypatch.setenv("BSC_STORAGE_BACKEND", "local")
        reset_encryption_key_cache()

        from bsc.config import reset_settings

        reset_settings()

        key = get_encryption_key()

        # Should not raise
        fernet = Fernet(key)
        assert fernet is not None

    def test_decrypt_with_wrong_key_raises_error(self, tmp_path, monkeypatch):
        """Test that decrypting with wrong key raises EncryptionError."""
        monkeypatch.setenv("BSC_CONFIG_DIR", str(tmp_path))
        monkeypatch.setenv("BSC_STORAGE_BACKEND", "local")
        reset_encryption_key_cache()

        from bsc.config import reset_settings

        reset_settings()

        original_data = b"Secret message"
        encrypted = encrypt_data(original_data)

        # Change the key
        key_file = tmp_path / ".session_key"
        key_file.write_bytes(Fernet.generate_key())
        reset_encryption_key_cache()

        # Should raise EncryptionError
        with pytest.raises(EncryptionError):
            decrypt_data(encrypted)


class TestEncryptionKeyFile:
    """Tests for encryption key file management."""

    def setup_method(self) -> None:
        """Reset encryption key cache before each test."""
        reset_encryption_key_cache()

    def test_key_file_created_with_secure_permissions(self, tmp_path, monkeypatch):
        """Test that key file is created with 0o600 permissions."""
        import os

        monkeypatch.setenv("BSC_CONFIG_DIR", str(tmp_path))
        monkeypatch.setenv("BSC_STORAGE_BACKEND", "local")
        reset_encryption_key_cache()

        from bsc.config import reset_settings

        reset_settings()

        # Trigger key generation
        get_encryption_key()

        key_file = tmp_path / ".session_key"
        assert key_file.exists()

        # Check permissions (Unix only)
        if hasattr(os, "stat"):
            stat_result = os.stat(key_file)
            mode = stat_result.st_mode & 0o777
            assert mode == 0o600, f"Expected 0o600, got {oct(mode)}"

    def test_existing_key_file_is_reused(self, tmp_path, monkeypatch):
        """Test that existing key file is reused."""
        monkeypatch.setenv("BSC_CONFIG_DIR", str(tmp_path))
        monkeypatch.setenv("BSC_STORAGE_BACKEND", "local")
        reset_encryption_key_cache()

        from bsc.config import reset_settings

        reset_settings()

        # Create a key file
        existing_key = Fernet.generate_key()
        key_file = tmp_path / ".session_key"
        key_file.write_bytes(existing_key)

        # Get key - should use existing
        key = get_encryption_key()
        assert key == existing_key
