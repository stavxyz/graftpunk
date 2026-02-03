"""Tests for configuration management."""

import pytest

from graftpunk.config import get_settings, reset_settings


class TestGetStorageConfig:
    """Tests for GraftpunkSettings.get_storage_config."""

    def test_local_storage_config(self):
        """Local backend returns sessions directory."""
        settings = get_settings()
        config = settings.get_storage_config("local")
        assert config == {"base_dir": settings.sessions_dir}

    def test_local_storage_config_default(self):
        """Default storage backend is local."""
        settings = get_settings()
        config = settings.get_storage_config()
        assert config == {"base_dir": settings.sessions_dir}

    def test_supabase_storage_config(self, monkeypatch):
        """Supabase backend returns correct config when env vars are set."""
        monkeypatch.setenv("GRAFTPUNK_SUPABASE_URL", "https://example.supabase.co")
        monkeypatch.setenv("GRAFTPUNK_SUPABASE_SERVICE_KEY", "secret-key-123")
        reset_settings()
        settings = get_settings()

        config = settings.get_storage_config("supabase")
        assert config["url"] == "https://example.supabase.co"
        assert config["service_key"] == "secret-key-123"
        assert config["bucket_name"] == "sessions"
        assert config["retry_max_attempts"] == 5
        assert config["retry_base_delay"] == 1.0

    def test_supabase_missing_url(self):
        """Supabase backend raises ValueError when URL is missing."""
        settings = get_settings()
        with pytest.raises(ValueError, match="GRAFTPUNK_SUPABASE_URL"):
            settings.get_storage_config("supabase")

    def test_supabase_missing_service_key(self, monkeypatch):
        """Supabase backend raises ValueError when service key is missing."""
        monkeypatch.setenv("GRAFTPUNK_SUPABASE_URL", "https://example.supabase.co")
        reset_settings()
        settings = get_settings()

        with pytest.raises(ValueError, match="GRAFTPUNK_SUPABASE_SERVICE_KEY"):
            settings.get_storage_config("supabase")

    def test_s3_storage_config(self, monkeypatch):
        """S3 backend returns correct config when env vars are set."""
        monkeypatch.setenv("GRAFTPUNK_S3_BUCKET", "my-bucket")
        monkeypatch.setenv("GRAFTPUNK_S3_REGION", "us-east-1")
        monkeypatch.setenv("GRAFTPUNK_S3_ENDPOINT_URL", "https://s3.example.com")
        reset_settings()
        settings = get_settings()

        config = settings.get_storage_config("s3")
        assert config["bucket"] == "my-bucket"
        assert config["region"] == "us-east-1"
        assert config["endpoint_url"] == "https://s3.example.com"
        assert config["retry_max_attempts"] == 5
        assert config["retry_base_delay"] == 1.0

    def test_s3_missing_bucket(self):
        """S3 backend raises ValueError when bucket is missing."""
        settings = get_settings()
        with pytest.raises(ValueError, match="GRAFTPUNK_S3_BUCKET"):
            settings.get_storage_config("s3")

    def test_s3_optional_fields_default_to_none(self, monkeypatch):
        """S3 backend allows region and endpoint_url to be None."""
        monkeypatch.setenv("GRAFTPUNK_S3_BUCKET", "my-bucket")
        reset_settings()
        settings = get_settings()

        config = settings.get_storage_config("s3")
        assert config["bucket"] == "my-bucket"
        assert config["region"] is None
        assert config["endpoint_url"] is None

    def test_unknown_backend_raises(self):
        """Unknown storage backend raises ValueError."""
        settings = get_settings()
        with pytest.raises(ValueError, match="Unsupported storage backend: foobar"):
            settings.get_storage_config("foobar")


class TestSessionsDir:
    """Tests for GraftpunkSettings.sessions_dir property."""

    def test_sessions_dir_is_subdir_of_config_dir(self):
        """sessions_dir is config_dir / 'sessions'."""
        settings = get_settings()
        assert settings.sessions_dir == settings.config_dir / "sessions"

    def test_sessions_dir_exists(self):
        """sessions_dir is created on init."""
        settings = get_settings()
        assert settings.sessions_dir.exists()
        assert settings.sessions_dir.is_dir()
