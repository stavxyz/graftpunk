"""Configuration management with pydantic-settings."""

from pathlib import Path
from typing import Any

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class GraftpunkSettings(BaseSettings):
    """graftpunk application settings loaded from environment variables.

    All settings use the GRAFTPUNK_ prefix for environment variables.
    """

    # Storage configuration
    storage_backend: str = Field(
        default="local",
        description="Storage backend: local, supabase, s3",
    )
    config_dir: Path = Field(
        default=Path.home() / ".config" / "graftpunk",
        description="Configuration directory for graftpunk data",
    )
    session_ttl_hours: int = Field(
        default=720,  # 30 days
        description="Session TTL in hours",
    )

    # Logging configuration
    log_level: str = Field(default="INFO", description="Logging level")

    # Supabase configuration (for supabase storage backend)
    supabase_url: str | None = Field(
        default=None,
        description="Supabase project URL",
    )
    supabase_service_key: SecretStr | None = Field(
        default=None,
        description="Supabase service role key",
    )
    session_storage_bucket: str = Field(
        default="sessions",
        description="Supabase Storage bucket name for sessions",
    )
    session_key_vault_name: str = Field(
        default="session-encryption-key",
        description="Supabase Vault secret name for encryption key",
    )

    # S3 configuration (for s3 storage backend)
    s3_bucket: str | None = Field(
        default=None,
        description="S3 bucket name for sessions",
    )
    s3_region: str | None = Field(
        default=None,
        description="S3 region",
    )
    s3_endpoint_url: str | None = Field(
        default=None,
        description="S3 endpoint URL (for S3-compatible storage)",
    )

    # Retry configuration
    retry_max_attempts: int = Field(
        default=5,
        description="Maximum retry attempts for storage operations",
    )
    retry_base_delay: float = Field(
        default=1.0,
        description="Base delay in seconds for exponential backoff",
    )

    model_config = SettingsConfigDict(
        env_prefix="GRAFTPUNK_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    def __init__(self, **kwargs: Any) -> None:
        """Initialize settings and create config directory."""
        super().__init__(**kwargs)
        # Create config directory if it doesn't exist
        self.config_dir.mkdir(parents=True, exist_ok=True)
        # Create sessions subdirectory
        sessions_dir = self.config_dir / "sessions"
        sessions_dir.mkdir(parents=True, exist_ok=True)

    @property
    def sessions_dir(self) -> Path:
        """Get the sessions storage directory."""
        return self.config_dir / "sessions"

    def get_storage_config(self, backend_type: str | None = None) -> dict[str, Any]:
        """Get storage backend configuration.

        Args:
            backend_type: Optional storage backend type override. If not provided,
                uses self.storage_backend.

        Returns:
            Configuration dictionary for storage backend.

        Raises:
            ValueError: If storage backend configuration is invalid.
        """
        storage_type = backend_type or self.storage_backend

        if storage_type == "local":
            return {"base_dir": self.sessions_dir}

        if storage_type == "supabase":
            if not self.supabase_url:
                raise ValueError(
                    "GRAFTPUNK_SUPABASE_URL environment variable is required "
                    "when using supabase storage backend"
                )
            if not self.supabase_service_key:
                raise ValueError(
                    "GRAFTPUNK_SUPABASE_SERVICE_KEY environment variable is required "
                    "when using supabase storage backend"
                )

            return {
                "url": self.supabase_url,
                "service_key": self.supabase_service_key.get_secret_value(),
                "bucket_name": self.session_storage_bucket,
                "retry_max_attempts": self.retry_max_attempts,
                "retry_base_delay": self.retry_base_delay,
            }

        if storage_type == "s3":
            if not self.s3_bucket:
                raise ValueError(
                    "GRAFTPUNK_S3_BUCKET environment variable is required "
                    "when using s3 storage backend"
                )

            return {
                "bucket": self.s3_bucket,
                "region": self.s3_region,
                "endpoint_url": self.s3_endpoint_url,
                "retry_max_attempts": self.retry_max_attempts,
                "retry_base_delay": self.retry_base_delay,
            }

        raise ValueError(
            f"Unsupported storage backend: {storage_type}. Supported: local, supabase, s3"
        )


# Global settings instance
_settings: GraftpunkSettings | None = None


def get_settings() -> GraftpunkSettings:
    """Get or create the global settings instance."""
    global _settings
    if _settings is None:
        _settings = GraftpunkSettings()
    return _settings


def reset_settings() -> None:
    """Reset the global settings instance (useful for testing)."""
    global _settings
    _settings = None
