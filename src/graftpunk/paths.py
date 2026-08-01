"""Settings-free path resolution shared by config and workstation_env.

This module is the single owner of "where is the config dir". It must stay
import-light and side-effect-free: no get_settings(), no directory creation
(GraftpunkSettings.__init__ creates directories; this module must not).

Note: pydantic-settings additionally honors a cwd-relative .env file — a
GRAFTPUNK_CONFIG_DIR set only there relocates settings.config_dir but does
NOT relocate the workstation env file. Documented as unsupported.
"""

import os
from pathlib import Path


def config_dir() -> Path:
    """Resolve the graftpunk config directory from the real environment.

    GRAFTPUNK_CONFIG_DIR from os.environ, else ~/.config/graftpunk.
    """
    override = os.environ.get("GRAFTPUNK_CONFIG_DIR")
    if override:
        return Path(override)
    return Path.home() / ".config" / "graftpunk"


def workstation_env_path() -> Path:
    """Path of the workstation env file: <config_dir>/env."""
    return config_dir() / "env"
