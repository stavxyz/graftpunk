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

    def test_decrypt_data_explicit_none_key_uses_configured_key(self, tmp_path, monkeypatch):
        """decrypt_data(..., key=None) is equivalent to the default configured-key path."""
        monkeypatch.setenv("GRAFTPUNK_CONFIG_DIR", str(tmp_path))
        monkeypatch.setenv("GRAFTPUNK_STORAGE_BACKEND", "local")
        reset_encryption_key_cache()

        from graftpunk.config import reset_settings

        reset_settings()

        original_data = b"Hello via configured key"
        encrypted = encrypt_data(original_data)

        assert decrypt_data(encrypted, key=None) == original_data

    def test_decrypt_data_with_explicit_key_roundtrip(self, tmp_path, monkeypatch):
        """decrypt_data(..., key=<key>) decrypts with the caller-supplied key,
        bypassing the configured key sources entirely (e.g. a Worker holding
        the Fernet key as a secret, with no graftpunk key file present)."""
        monkeypatch.setenv("GRAFTPUNK_CONFIG_DIR", str(tmp_path))
        monkeypatch.setenv("GRAFTPUNK_STORAGE_BACKEND", "local")
        reset_encryption_key_cache()

        from graftpunk.config import reset_settings

        reset_settings()

        explicit_key = Fernet.generate_key()
        original_data = b"Hello via explicit key"
        encrypted = Fernet(explicit_key).encrypt(original_data)

        assert decrypt_data(encrypted, key=explicit_key) == original_data

    def test_decrypt_data_with_wrong_explicit_key_raises_error(self, tmp_path, monkeypatch):
        """A wrong explicit key raises EncryptionError, same mapping as the
        configured-key path."""
        monkeypatch.setenv("GRAFTPUNK_CONFIG_DIR", str(tmp_path))
        monkeypatch.setenv("GRAFTPUNK_STORAGE_BACKEND", "local")
        reset_encryption_key_cache()

        from graftpunk.config import reset_settings

        reset_settings()

        right_key = Fernet.generate_key()
        wrong_key = Fernet.generate_key()
        encrypted = Fernet(right_key).encrypt(b"Secret message")

        with pytest.raises(EncryptionError):
            decrypt_data(encrypted, key=wrong_key)


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


class TestCallerSuppliedKey:
    """encrypt_data and cache_session take the key a keyless host holds (#196)."""

    def setup_method(self) -> None:
        reset_encryption_key_cache()

    def teardown_method(self) -> None:
        reset_encryption_key_cache()

    @staticmethod
    def _configure(tmp_path, monkeypatch, backend: str):  # noqa: ANN205
        from graftpunk.cache import _reset_session_storage_backend
        from graftpunk.config import reset_settings

        monkeypatch.setenv("GRAFTPUNK_CONFIG_DIR", str(tmp_path))
        monkeypatch.setenv("GRAFTPUNK_STORAGE_BACKEND", backend)
        reset_settings()
        _reset_session_storage_backend()
        return tmp_path / ".session_key"

    @pytest.mark.parametrize("backend", ["local", "s3"])
    def test_a_caller_key_round_trips_and_no_key_file_is_made(self, tmp_path, monkeypatch, backend):
        key_file = self._configure(tmp_path, monkeypatch, backend)
        key = Fernet.generate_key()

        encrypted = encrypt_data(b"cookie jar", key=key)

        assert Fernet(key).decrypt(encrypted) == b"cookie jar"
        assert decrypt_data(encrypted, key=key) == b"cookie jar"
        assert not key_file.exists()

    def test_a_malformed_caller_key_is_an_encryption_error(self, tmp_path, monkeypatch):
        self._configure(tmp_path, monkeypatch, "local")

        with pytest.raises(EncryptionError, match="Invalid encryption key"):
            encrypt_data(b"cookie jar", key=b"not-a-fernet-key")

    def test_no_key_file_and_no_key_raises_for_a_remote_backend(self, tmp_path, monkeypatch):
        key_file = self._configure(tmp_path, monkeypatch, "s3")

        with pytest.raises(EncryptionError) as raised:
            encrypt_data(b"cookie jar")

        message = str(raised.value)
        assert str(key_file) in message
        assert "GRAFTPUNK_STORAGE_BACKEND=s3" in message
        assert "key=" in message
        assert not key_file.exists()

    def test_a_remote_backend_read_without_a_key_file_raises_the_same_error(
        self, tmp_path, monkeypatch
    ):
        """Before #196 a read generated a fresh key, then reported a changed key."""
        key_file = self._configure(tmp_path, monkeypatch, "s3")
        encrypted = Fernet(Fernet.generate_key()).encrypt(b"cookie jar")

        with pytest.raises(EncryptionError, match="No session encryption key"):
            decrypt_data(encrypted)
        assert not key_file.exists()

    def test_a_remote_backend_uses_an_existing_key_file(self, tmp_path, monkeypatch):
        key_file = self._configure(tmp_path, monkeypatch, "s3")
        key = Fernet.generate_key()
        key_file.write_bytes(key)

        assert Fernet(key).decrypt(encrypt_data(b"cookie jar")) == b"cookie jar"

    def test_the_local_backend_still_generates_its_key_beside_its_sessions(
        self, tmp_path, monkeypatch
    ):
        key_file = self._configure(tmp_path, monkeypatch, "local")

        assert decrypt_data(encrypt_data(b"cookie jar")) == b"cookie jar"
        assert key_file.exists()

    def test_cache_session_encrypts_with_the_caller_key(self, tmp_path, monkeypatch):
        import requests

        from graftpunk.cache import (
            _get_session_storage_backend,
            cache_session,
            load_session_for_api_from_bytes,
        )

        key_file = self._configure(tmp_path, monkeypatch, "local")
        key = Fernet.generate_key()
        session = requests.Session()
        session.cookies.set("sid", "abc", domain="myshop.example")

        cache_session(session, "myshop", key=key)

        blob, _metadata = _get_session_storage_backend().load_session("myshop")
        api = load_session_for_api_from_bytes(blob, key=key)
        assert api.cookies.get("sid", domain="myshop.example") == "abc"
        assert not key_file.exists()


class TestMissingKeyIsNotAnExpiredSession:
    """On a non-local backend a missing key file is its own error, on every path (#196 review)."""

    def setup_method(self) -> None:
        reset_encryption_key_cache()

    def teardown_method(self) -> None:
        reset_encryption_key_cache()

    @staticmethod
    def _s3_settings_over_local_files(tmp_path, monkeypatch):  # noqa: ANN205
        """Settings say s3 (so the key rule is s3's); the slots live in a local store."""
        from graftpunk import cache
        from graftpunk.config import reset_settings
        from graftpunk.storage.local import LocalSessionStorage

        monkeypatch.setenv("GRAFTPUNK_CONFIG_DIR", str(tmp_path))
        monkeypatch.setenv("GRAFTPUNK_STORAGE_BACKEND", "local")
        reset_settings()
        cache._reset_session_storage_backend()
        store = LocalSessionStorage(base_dir=tmp_path / "sessions")
        monkeypatch.setattr(cache, "_get_session_storage_backend", lambda **_kw: store)
        return store

    def _cache_then_lose_the_key(self, tmp_path, monkeypatch):  # noqa: ANN205
        import requests

        from graftpunk.cache import cache_session
        from graftpunk.config import reset_settings

        self._s3_settings_over_local_files(tmp_path, monkeypatch)
        session = requests.Session()
        session.cookies.set("sid", "abc", domain="myshop.example")
        cache_session(session, "myshop")
        (tmp_path / ".session_key").unlink()
        reset_encryption_key_cache()
        monkeypatch.setenv("GRAFTPUNK_STORAGE_BACKEND", "s3")
        reset_settings()

    @pytest.mark.parametrize("loader", ["load_session", "load_session_for_api"])
    def test_a_load_names_the_missing_key_and_never_says_clear(self, tmp_path, monkeypatch, loader):
        from graftpunk import cache
        from graftpunk.exceptions import MissingEncryptionKeyError, SessionExpiredError

        self._cache_then_lose_the_key(tmp_path, monkeypatch)

        with pytest.raises(MissingEncryptionKeyError) as raised:
            getattr(cache, loader)("myshop")

        assert not isinstance(raised.value, SessionExpiredError)
        message = str(raised.value)
        assert str(tmp_path / ".session_key") in message
        assert "key=" in message
        assert "clear" not in message
        assert not (tmp_path / ".session_key").exists()

    def test_a_wrong_key_is_still_reported_as_an_unreadable_session(self, tmp_path, monkeypatch):
        from graftpunk.cache import load_session
        from graftpunk.exceptions import SessionExpiredError

        self._cache_then_lose_the_key(tmp_path, monkeypatch)
        (tmp_path / ".session_key").write_bytes(Fernet.generate_key())

        with pytest.raises(SessionExpiredError, match="cannot be decrypted"):
            load_session("myshop")

    def test_a_save_without_a_key_writes_nothing(self, tmp_path, monkeypatch):
        import requests

        from graftpunk import cache
        from graftpunk.config import reset_settings
        from graftpunk.exceptions import MissingEncryptionKeyError

        saves: list[str] = []

        class RecordingBackend:
            def save_session(self, name, data, metadata):  # noqa: ANN001, ANN201
                saves.append(name)
                return name

        monkeypatch.setenv("GRAFTPUNK_CONFIG_DIR", str(tmp_path))
        monkeypatch.setenv("GRAFTPUNK_STORAGE_BACKEND", "s3")
        reset_settings()
        monkeypatch.setattr(cache, "_get_session_storage_backend", lambda **_kw: RecordingBackend())

        with pytest.raises(MissingEncryptionKeyError):
            cache.cache_session(requests.Session(), "myshop")

        assert saves == []
        assert not (tmp_path / ".session_key").exists()


class TestTheKeyFollowsTheConfigDir:
    """The cached key belongs to the key file it was read from (#282 review)."""

    def teardown_method(self) -> None:
        reset_encryption_key_cache()

    def test_a_late_config_dir_change_reads_that_directorys_key(self, tmp_path, monkeypatch):
        from graftpunk.config import reset_settings

        first, later = tmp_path / "first", tmp_path / "later"
        for directory in (first, later):
            directory.mkdir()
            (directory / ".session_key").write_bytes(Fernet.generate_key())
        monkeypatch.setenv("GRAFTPUNK_STORAGE_BACKEND", "local")
        monkeypatch.setenv("GRAFTPUNK_CONFIG_DIR", str(first))
        reset_settings()
        reset_encryption_key_cache()
        assert get_encryption_key() == (first / ".session_key").read_bytes()

        monkeypatch.setenv("GRAFTPUNK_CONFIG_DIR", str(later))

        assert get_encryption_key() == (later / ".session_key").read_bytes()
        assert get_encryption_key() is get_encryption_key()


class TestCallerKeyOnEveryWritePath:
    """Every write path, and load_session, takes the caller's key= (#196 review)."""

    def setup_method(self) -> None:
        reset_encryption_key_cache()

    def teardown_method(self) -> None:
        reset_encryption_key_cache()

    @staticmethod
    def _local(tmp_path, monkeypatch):  # noqa: ANN205
        from graftpunk import cache
        from graftpunk.config import reset_settings

        monkeypatch.setenv("GRAFTPUNK_CONFIG_DIR", str(tmp_path))
        monkeypatch.setenv("GRAFTPUNK_STORAGE_BACKEND", "local")
        reset_settings()
        cache._reset_session_storage_backend()
        return tmp_path / ".session_key"

    @staticmethod
    def _blob(name: str) -> bytes:
        from graftpunk.cache import _get_session_storage_backend

        return _get_session_storage_backend().load_session(name)[0]

    @staticmethod
    def _session(value: str):  # noqa: ANN205
        import requests

        session = requests.Session()
        session.cookies.set("sid", value, domain="myshop.example")
        return session

    def test_load_session_with_a_caller_key(self, tmp_path, monkeypatch):
        from graftpunk.cache import cache_session, load_session

        key_file = self._local(tmp_path, monkeypatch)
        key = Fernet.generate_key()
        cache_session(self._session("abc"), "myshop", key=key)

        loaded = load_session("myshop", key=key)

        assert loaded.cookies.get("sid", domain="myshop.example") == "abc"
        assert not key_file.exists()

    def test_copy_session_reads_and_writes_with_the_caller_key(self, tmp_path, monkeypatch):
        from graftpunk.cache import cache_session, copy_session, load_session_for_api_from_bytes

        key_file = self._local(tmp_path, monkeypatch)
        key = Fernet.generate_key()
        cache_session(self._session("abc"), "myshop", key=key)

        copy_session("myshop", "myshop@alice", account_identifier="alice@myshop.example", key=key)

        api = load_session_for_api_from_bytes(self._blob("myshop@alice"), key=key)
        assert api.cookies.get("sid", domain="myshop.example") == "abc"
        assert not key_file.exists()

    def test_update_session_cookies_writes_back_with_the_caller_key(self, tmp_path, monkeypatch):
        from graftpunk.cache import (
            cache_session,
            load_session_for_api_from_bytes,
            update_session_cookies,
        )

        key_file = self._local(tmp_path, monkeypatch)
        key = Fernet.generate_key()
        cache_session(self._session("old"), "myshop", key=key)
        api = load_session_for_api_from_bytes(self._blob("myshop"), key=key)
        api.cookies.set("sid", "new", domain="myshop.example")

        update_session_cookies(api, "myshop", key=key)

        stored = load_session_for_api_from_bytes(self._blob("myshop"), key=key)
        assert stored.cookies.get("sid", domain="myshop.example") == "new"
        assert not key_file.exists()

    def test_cache_login_session_encrypts_with_the_caller_key(self, tmp_path, monkeypatch):
        from graftpunk.cache import load_session_for_api_from_bytes
        from graftpunk.plugins.cli_plugin import SitePlugin, cache_login_session

        class MyShop(SitePlugin):
            site_name = "myshop"
            base_url = "https://myshop.example"
            session_name = "myshop"

        key_file = self._local(tmp_path, monkeypatch)
        key = Fernet.generate_key()

        used = cache_login_session(MyShop(), self._session("abc"), name="myshop@alice", key=key)

        api = load_session_for_api_from_bytes(self._blob(used), key=key)
        assert api.cookies.get("sid", domain="myshop.example") == "abc"
        assert not key_file.exists()
