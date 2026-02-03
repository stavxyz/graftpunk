"""Tests for encryption module."""

from unittest.mock import MagicMock, patch

import pytest
from cryptography.fernet import Fernet

from graftpunk.encryption import (
    _get_key_from_supabase_vault,
    _load_encryption_key,
    decrypt_data,
    encrypt_data,
    get_encryption_key,
    reset_encryption_key_cache,
)
from graftpunk.exceptions import EncryptionError


class TestEncryption:
    """Tests for encryption functions."""

    def setup_method(self) -> None:
        """Reset encryption key cache before each test."""
        reset_encryption_key_cache()

    def test_encrypt_decrypt_roundtrip(self, tmp_path, monkeypatch):
        """Test that encrypt/decrypt is a valid roundtrip."""
        # Set up temporary config directory
        monkeypatch.setenv("GRAFTPUNK_CONFIG_DIR", str(tmp_path))
        monkeypatch.setenv("GRAFTPUNK_STORAGE_BACKEND", "local")
        reset_encryption_key_cache()

        # Reset settings to pick up new config
        from graftpunk.config import reset_settings

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
        monkeypatch.setenv("GRAFTPUNK_CONFIG_DIR", str(tmp_path))
        monkeypatch.setenv("GRAFTPUNK_STORAGE_BACKEND", "local")
        reset_encryption_key_cache()

        from graftpunk.config import reset_settings

        reset_settings()

        key1 = get_encryption_key()
        key2 = get_encryption_key()

        assert key1 is key2  # Same object (cached)

    def test_encryption_key_is_valid_fernet_key(self, tmp_path, monkeypatch):
        """Test that generated key is a valid Fernet key."""
        monkeypatch.setenv("GRAFTPUNK_CONFIG_DIR", str(tmp_path))
        monkeypatch.setenv("GRAFTPUNK_STORAGE_BACKEND", "local")
        reset_encryption_key_cache()

        from graftpunk.config import reset_settings

        reset_settings()

        key = get_encryption_key()

        # Should not raise
        fernet = Fernet(key)
        assert fernet is not None

    def test_decrypt_with_wrong_key_raises_error(self, tmp_path, monkeypatch):
        """Test that decrypting with wrong key raises EncryptionError."""
        monkeypatch.setenv("GRAFTPUNK_CONFIG_DIR", str(tmp_path))
        monkeypatch.setenv("GRAFTPUNK_STORAGE_BACKEND", "local")
        reset_encryption_key_cache()

        from graftpunk.config import reset_settings

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

        monkeypatch.setenv("GRAFTPUNK_CONFIG_DIR", str(tmp_path))
        monkeypatch.setenv("GRAFTPUNK_STORAGE_BACKEND", "local")
        reset_encryption_key_cache()

        from graftpunk.config import reset_settings

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
        monkeypatch.setenv("GRAFTPUNK_CONFIG_DIR", str(tmp_path))
        monkeypatch.setenv("GRAFTPUNK_STORAGE_BACKEND", "local")
        reset_encryption_key_cache()

        from graftpunk.config import reset_settings

        reset_settings()

        # Create a key file
        existing_key = Fernet.generate_key()
        key_file = tmp_path / ".session_key"
        key_file.write_bytes(existing_key)

        # Get key - should use existing
        key = get_encryption_key()
        assert key == existing_key


class TestLoadEncryptionKeySupabasePath:
    """Tests for the supabase branch in _load_encryption_key()."""

    def setup_method(self) -> None:
        """Reset encryption key cache before each test."""
        reset_encryption_key_cache()

    @patch("graftpunk.encryption._get_key_from_supabase_vault")
    @patch("graftpunk.encryption.get_settings")
    def test_load_key_delegates_to_supabase_vault(self, mock_settings, mock_vault):
        """Test that _load_encryption_key calls supabase vault when backend is supabase."""
        mock_settings.return_value.storage_backend = "supabase"
        expected_key = Fernet.generate_key()
        mock_vault.return_value = expected_key

        result = _load_encryption_key()

        mock_vault.assert_called_once()
        assert result == expected_key

    @patch("graftpunk.encryption._get_key_from_supabase_vault")
    @patch("graftpunk.encryption.get_settings")
    def test_load_key_supabase_case_insensitive(self, mock_settings, mock_vault):
        """Test that storage_backend comparison is case-insensitive."""
        mock_settings.return_value.storage_backend = "Supabase"
        expected_key = Fernet.generate_key()
        mock_vault.return_value = expected_key

        result = _load_encryption_key()

        mock_vault.assert_called_once()
        assert result == expected_key


class TestGetKeyFromSupabaseVault:
    """Tests for _get_key_from_supabase_vault() function."""

    def setup_method(self) -> None:
        """Reset encryption key cache before each test."""
        reset_encryption_key_cache()

    def test_import_error_raises_encryption_error(self):
        """Test that missing supabase package raises EncryptionError."""
        with (
            patch.dict("sys.modules", {"supabase": None}),
            pytest.raises(EncryptionError, match="supabase package is required"),
        ):
            _get_key_from_supabase_vault()

    @patch("graftpunk.encryption.get_settings")
    def test_missing_supabase_url_raises_error(self, mock_get_settings):
        """Test that missing supabase_url raises EncryptionError."""
        mock_settings = MagicMock()
        mock_settings.supabase_url = None
        mock_settings.supabase_service_key = MagicMock()
        mock_get_settings.return_value = mock_settings

        # Need supabase importable
        mock_supabase = MagicMock()
        with (
            patch.dict("sys.modules", {"supabase": mock_supabase}),
            pytest.raises(EncryptionError, match="GRAFTPUNK_SUPABASE_URL"),
        ):
            _get_key_from_supabase_vault()

    @patch("graftpunk.encryption.get_settings")
    def test_missing_supabase_service_key_raises_error(self, mock_get_settings):
        """Test that missing supabase_service_key raises EncryptionError."""
        mock_settings = MagicMock()
        mock_settings.supabase_url = "https://project.supabase.co"
        mock_settings.supabase_service_key = None
        mock_get_settings.return_value = mock_settings

        mock_supabase = MagicMock()
        with (
            patch.dict("sys.modules", {"supabase": mock_supabase}),
            pytest.raises(EncryptionError, match="GRAFTPUNK_SUPABASE_SERVICE_KEY"),
        ):
            _get_key_from_supabase_vault()

    @patch("graftpunk.encryption.get_settings")
    def test_successful_vault_fetch_list_response(self, mock_get_settings):
        """Test successful key fetch with list response from vault."""
        valid_key = Fernet.generate_key()

        mock_settings = MagicMock()
        mock_settings.supabase_url = "https://project.supabase.co"
        mock_settings.supabase_service_key = MagicMock()
        mock_settings.supabase_service_key.get_secret_value.return_value = "service-key"
        mock_settings.session_key_vault_name = "session-encryption-key"
        mock_get_settings.return_value = mock_settings

        mock_execute = MagicMock()
        mock_execute.data = [{"decrypted_secret": valid_key.decode()}]

        mock_client = MagicMock()
        mock_client.rpc.return_value.execute.return_value = mock_execute

        mock_create_client = MagicMock(return_value=mock_client)

        with patch.dict("sys.modules", {"supabase": MagicMock(create_client=mock_create_client)}):
            result = _get_key_from_supabase_vault()

        assert result == valid_key

    @patch("graftpunk.encryption.get_settings")
    def test_successful_vault_fetch_dict_response(self, mock_get_settings):
        """Test successful key fetch with dict response from vault."""
        valid_key = Fernet.generate_key()

        mock_settings = MagicMock()
        mock_settings.supabase_url = "https://project.supabase.co"
        mock_settings.supabase_service_key = MagicMock()
        mock_settings.supabase_service_key.get_secret_value.return_value = "service-key"
        mock_settings.session_key_vault_name = "session-encryption-key"
        mock_get_settings.return_value = mock_settings

        mock_execute = MagicMock()
        mock_execute.data = {"decrypted_secret": valid_key.decode()}

        mock_client = MagicMock()
        mock_client.rpc.return_value.execute.return_value = mock_execute

        mock_create_client = MagicMock(return_value=mock_client)

        with patch.dict("sys.modules", {"supabase": MagicMock(create_client=mock_create_client)}):
            result = _get_key_from_supabase_vault()

        assert result == valid_key

    @patch("graftpunk.encryption.get_settings")
    def test_empty_vault_result_raises_error(self, mock_get_settings):
        """Test that empty vault result raises EncryptionError."""
        mock_settings = MagicMock()
        mock_settings.supabase_url = "https://project.supabase.co"
        mock_settings.supabase_service_key = MagicMock()
        mock_settings.supabase_service_key.get_secret_value.return_value = "service-key"
        mock_settings.session_key_vault_name = "session-encryption-key"
        mock_get_settings.return_value = mock_settings

        mock_execute = MagicMock()
        mock_execute.data = []

        mock_client = MagicMock()
        mock_client.rpc.return_value.execute.return_value = mock_execute

        mock_create_client = MagicMock(return_value=mock_client)

        with (
            patch.dict("sys.modules", {"supabase": MagicMock(create_client=mock_create_client)}),
            pytest.raises(EncryptionError, match="not found in Supabase Vault"),
        ):
            _get_key_from_supabase_vault()

    @patch("graftpunk.encryption.get_settings")
    def test_empty_secret_value_raises_error(self, mock_get_settings):
        """Test that empty secret value in response raises EncryptionError."""
        mock_settings = MagicMock()
        mock_settings.supabase_url = "https://project.supabase.co"
        mock_settings.supabase_service_key = MagicMock()
        mock_settings.supabase_service_key.get_secret_value.return_value = "service-key"
        mock_settings.session_key_vault_name = "session-encryption-key"
        mock_get_settings.return_value = mock_settings

        mock_execute = MagicMock()
        mock_execute.data = [{"decrypted_secret": ""}]

        mock_client = MagicMock()
        mock_client.rpc.return_value.execute.return_value = mock_execute

        mock_create_client = MagicMock(return_value=mock_client)

        with (
            patch.dict("sys.modules", {"supabase": MagicMock(create_client=mock_create_client)}),
            pytest.raises(EncryptionError, match="Empty encryption key"),
        ):
            _get_key_from_supabase_vault()

    @patch("graftpunk.encryption.get_settings")
    def test_invalid_fernet_key_raises_error(self, mock_get_settings):
        """Test that invalid Fernet key from vault raises EncryptionError."""
        mock_settings = MagicMock()
        mock_settings.supabase_url = "https://project.supabase.co"
        mock_settings.supabase_service_key = MagicMock()
        mock_settings.supabase_service_key.get_secret_value.return_value = "service-key"
        mock_settings.session_key_vault_name = "session-encryption-key"
        mock_get_settings.return_value = mock_settings

        mock_execute = MagicMock()
        mock_execute.data = [{"decrypted_secret": "not-a-valid-fernet-key"}]

        mock_client = MagicMock()
        mock_client.rpc.return_value.execute.return_value = mock_execute

        mock_create_client = MagicMock(return_value=mock_client)

        with (
            patch.dict("sys.modules", {"supabase": MagicMock(create_client=mock_create_client)}),
            pytest.raises(EncryptionError, match="Invalid encryption key in Vault"),
        ):
            _get_key_from_supabase_vault()

    @patch("graftpunk.encryption.get_settings")
    def test_unexpected_secret_type_raises_error(self, mock_get_settings):
        """Test that unexpected type for secret value raises EncryptionError."""
        mock_settings = MagicMock()
        mock_settings.supabase_url = "https://project.supabase.co"
        mock_settings.supabase_service_key = MagicMock()
        mock_settings.supabase_service_key.get_secret_value.return_value = "service-key"
        mock_settings.session_key_vault_name = "session-encryption-key"
        mock_get_settings.return_value = mock_settings

        # Return an integer instead of str/bytes/dict/list
        mock_execute = MagicMock()
        mock_execute.data = 12345

        mock_client = MagicMock()
        mock_client.rpc.return_value.execute.return_value = mock_execute

        mock_create_client = MagicMock(return_value=mock_client)

        with (
            patch.dict("sys.modules", {"supabase": MagicMock(create_client=mock_create_client)}),
            pytest.raises(EncryptionError, match="Unexpected type"),
        ):
            _get_key_from_supabase_vault()

    @patch("graftpunk.encryption.get_settings")
    def test_general_exception_raises_encryption_error(self, mock_get_settings):
        """Test that general exceptions are wrapped in EncryptionError."""
        mock_settings = MagicMock()
        mock_settings.supabase_url = "https://project.supabase.co"
        mock_settings.supabase_service_key = MagicMock()
        mock_settings.supabase_service_key.get_secret_value.return_value = "service-key"
        mock_settings.session_key_vault_name = "session-encryption-key"
        mock_get_settings.return_value = mock_settings

        mock_create_client = MagicMock(side_effect=RuntimeError("connection failed"))

        with (
            patch.dict("sys.modules", {"supabase": MagicMock(create_client=mock_create_client)}),
            pytest.raises(EncryptionError, match="Failed to fetch encryption key"),
        ):
            _get_key_from_supabase_vault()

    @patch("graftpunk.encryption.get_settings")
    def test_successful_vault_fetch_bytes_response(self, mock_get_settings):
        """Test successful key fetch when secret value is bytes."""
        valid_key = Fernet.generate_key()

        mock_settings = MagicMock()
        mock_settings.supabase_url = "https://project.supabase.co"
        mock_settings.supabase_service_key = MagicMock()
        mock_settings.supabase_service_key.get_secret_value.return_value = "service-key"
        mock_settings.session_key_vault_name = "session-encryption-key"
        mock_get_settings.return_value = mock_settings

        mock_execute = MagicMock()
        # Return bytes directly (not in a list/dict wrapper)
        mock_execute.data = valid_key

        mock_client = MagicMock()
        mock_client.rpc.return_value.execute.return_value = mock_execute

        mock_create_client = MagicMock(return_value=mock_client)

        with patch.dict("sys.modules", {"supabase": MagicMock(create_client=mock_create_client)}):
            result = _get_key_from_supabase_vault()

        assert result == valid_key

    @patch("graftpunk.encryption.get_settings")
    def test_url_normalization(self, mock_get_settings):
        """Test that supabase URL is normalized with trailing slash."""
        valid_key = Fernet.generate_key()

        mock_settings = MagicMock()
        mock_settings.supabase_url = "https://project.supabase.co/"
        mock_settings.supabase_service_key = MagicMock()
        mock_settings.supabase_service_key.get_secret_value.return_value = "service-key"
        mock_settings.session_key_vault_name = "session-encryption-key"
        mock_get_settings.return_value = mock_settings

        mock_execute = MagicMock()
        mock_execute.data = [{"decrypted_secret": valid_key.decode()}]

        mock_client = MagicMock()
        mock_client.rpc.return_value.execute.return_value = mock_execute

        mock_create_client = MagicMock(return_value=mock_client)

        with patch.dict("sys.modules", {"supabase": MagicMock(create_client=mock_create_client)}):
            _get_key_from_supabase_vault()

        # Verify URL was normalized (trailing slash added/kept)
        mock_create_client.assert_called_once_with(
            "https://project.supabase.co/",
            "service-key",
        )

    @patch("graftpunk.encryption.get_settings")
    def test_none_data_raises_error(self, mock_get_settings):
        """Test that None result.data raises EncryptionError."""
        mock_settings = MagicMock()
        mock_settings.supabase_url = "https://project.supabase.co"
        mock_settings.supabase_service_key = MagicMock()
        mock_settings.supabase_service_key.get_secret_value.return_value = "service-key"
        mock_settings.session_key_vault_name = "session-encryption-key"
        mock_get_settings.return_value = mock_settings

        mock_execute = MagicMock()
        mock_execute.data = None

        mock_client = MagicMock()
        mock_client.rpc.return_value.execute.return_value = mock_execute

        mock_create_client = MagicMock(return_value=mock_client)

        with (
            patch.dict("sys.modules", {"supabase": MagicMock(create_client=mock_create_client)}),
            pytest.raises(EncryptionError, match="not found in Supabase Vault"),
        ):
            _get_key_from_supabase_vault()
