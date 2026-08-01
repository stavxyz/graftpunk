"""Configuration management with pydantic-settings."""

from pathlib import Path
from typing import Any, Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from graftpunk import paths


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
        # Lambda (not the bare function ref) so the resolver stays
        # late-bound: tests can monkeypatch paths.config_dir and the
        # default picks it up.
        default_factory=lambda: paths.config_dir(),
        description="Configuration directory for graftpunk data",
    )
    session_ttl_hours: int = Field(
        default=720,  # 30 days
        description="Session TTL in hours",
    )

    # Logging configuration
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = Field(
        default="WARNING",
        description="Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)",
    )
    log_format: Literal["console", "json"] = Field(
        default="console",
        description="Log output format (console for human-readable, json for structured)",
    )

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

    browser_executable_path: str | None = Field(
        default=None,
        description=(
            "Path to a Chrome/Chromium binary for the nodriver backend. When set, "
            "browser sessions use it instead of the system Chrome auto-detection — "
            "e.g. point at a Chrome-for-Testing binary on machines/CI without a "
            "system Chrome install."
        ),
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


# Leaf fields consumed ONLY via external attribute access — safe for the
# lazy overlay. Reads that happen INSIDE model methods/properties
# (get_storage_config()'s supabase_*/s3_*, sessions_dir's config_dir) bind
# to the raw model and can never see the overlay, so command values for
# those fields are unsupported (warned + ignored; supply via real env).
PROXY_SAFE_FIELDS = frozenset({"browser_executable_path"})


class LazySettings:
    """Bounded lazy overlay over GraftpunkSettings.

    Resolves command-valued workstation-env entries for PROXY_SAFE_FIELDS on
    first attribute access. Source-aware precedence gate: the overlay is
    skipped only when the REAL environment provides the field (a merely
    .env-provided value still yields to the workstation command value), so
    the documented order — real env > workstation file > cwd .env > default —
    holds for both value kinds and is decided here alone.
    """

    def __init__(self, model: GraftpunkSettings, lazy_fields: dict[str, str]):
        # lazy_fields: field name -> env var name (derived from model config,
        # pydantic stays the single authority for the mapping)
        object.__setattr__(self, "_model", model)
        object.__setattr__(self, "_lazy_fields", lazy_fields)
        object.__setattr__(self, "_resolved", {})

    def __getattr__(self, name: str):
        lazy_fields = object.__getattribute__(self, "_lazy_fields")
        if name in lazy_fields:
            resolved = object.__getattribute__(self, "_resolved")
            if name not in resolved:
                from pydantic import TypeAdapter

                from graftpunk import workstation_env

                raw = workstation_env.load().lookup(lazy_fields[name])
                model = object.__getattribute__(self, "_model")
                if raw is None:
                    resolved[name] = getattr(model, name)
                else:
                    adapter = TypeAdapter(GraftpunkSettings.model_fields[name].annotation)
                    resolved[name] = adapter.validate_python(raw)
            return resolved[name]
        return getattr(object.__getattribute__(self, "_model"), name)


def _field_env_name(field_name: str) -> str:
    """Derive the env var name from the model's own settings config."""
    prefix = GraftpunkSettings.model_config.get("env_prefix", "")
    return f"{prefix}{field_name}".upper()


def _build_lazy_field_map() -> dict[str, str]:
    """Map allowlisted field -> env var for command entries the real env
    doesn't override. Warns about command entries targeting non-allowlisted
    GRAFTPUNK_* fields (static-only; see PROXY_SAFE_FIELDS)."""
    import os

    from graftpunk import workstation_env
    from graftpunk.logging import get_logger

    log = get_logger(__name__)
    lazy: dict[str, str] = {}
    command_names = workstation_env.load().command_entry_names()
    matched_env_names: set[str] = set()
    for field_name in GraftpunkSettings.model_fields:
        env_name = _field_env_name(field_name)
        if env_name not in command_names:
            continue
        matched_env_names.add(env_name)
        if field_name not in PROXY_SAFE_FIELDS:
            log.warning(
                "workstation_env_command_unsupported_for_field",
                envvar=env_name,
                hint="command values are unsupported for this setting; "
                "treat as static-only or supply via the real environment",
            )
            continue
        if os.environ.get(env_name):
            continue  # real env wins; empty string counts as a miss
        lazy[field_name] = env_name

    # Typo diagnostic: a command entry that LOOKS like a settings field
    # (GRAFTPUNK_ prefix) but matches NO model field at all would otherwise
    # be silently ignored forever — most likely a misspelled setting name.
    prefix = GraftpunkSettings.model_config.get("env_prefix", "")
    for env_name in command_names:
        if env_name.startswith(prefix) and env_name not in matched_env_names:
            log.warning(
                "workstation_env_command_unknown_setting",
                envvar=env_name,
                hint="no GraftpunkSettings field maps to this name; check for a typo",
            )
    return lazy


# Global settings instance
# NOTE: this singleton (including a LazySettings proxy's per-field _resolved
# cache) is intentionally NOT invalidated by workstation-env writes
# (set_entry()/unset_entry()) -- only workstation_env's own load()/lookup()
# guarantee write-then-lookup coherence. Call reset_settings() explicitly to
# see a workstation-env write reflected in settings (tests already do this).
_settings: "GraftpunkSettings | LazySettings | None" = None


def get_settings() -> "GraftpunkSettings | LazySettings":
    """Get or create the global settings instance.

    First call runs workstation_env.ensure_bootstrap() BEFORE constructing
    the model — the ordering invariant (file statics precede the singleton)
    is owned here, structurally, for every entry path: the CLI and
    in-process library consumers alike. When the workstation file holds
    command-valued entries for PROXY_SAFE_FIELDS, the singleton is wrapped
    in a LazySettings proxy; otherwise the raw model is returned unchanged.
    """
    global _settings
    if _settings is None:
        from graftpunk import workstation_env

        workstation_env.ensure_bootstrap()
        model = GraftpunkSettings()
        lazy_fields = _build_lazy_field_map()
        _settings = LazySettings(model, lazy_fields) if lazy_fields else model
    return _settings


def reset_settings() -> None:
    """Reset the global settings instance.

    Clears the cached global settings instance, forcing the next call to
    get_settings() to create a new instance. Useful for testing or when
    environment variables have changed and need to be reloaded.
    """
    global _settings
    _settings = None
