"""Settings-free path resolution shared by config and workstation_env.

This module is the single owner of "where is the config dir". It must stay
import-light and side-effect-free: no get_settings(), no directory creation
(GraftpunkSettings.__init__ creates directories; this module must not).

GraftpunkSettings.config_dir is a property that calls config_dir() on every
read, so settings, the session store, plugin discovery and the workstation env
file can never see different directories (#282). A GRAFTPUNK_CONFIG_DIR set
only in a cwd .env is not read here or there; GraftpunkSettings warns about it.
"""

import os
from pathlib import Path

CONFIG_DIR_ENV = "GRAFTPUNK_CONFIG_DIR"


def config_dir() -> Path:
    """Resolve the graftpunk config directory from the real environment.

    GRAFTPUNK_CONFIG_DIR from os.environ, else ~/.config/graftpunk. An empty
    value counts as unset. A ``~`` prefix is expanded and a relative value is
    made absolute against the current directory, so the answer does not
    change meaning between the callers that use it.
    """
    override = os.environ.get(CONFIG_DIR_ENV)
    if override:
        return Path(os.path.abspath(os.path.expanduser(override)))
    return Path.home() / ".config" / "graftpunk"


def workstation_env_path() -> Path:
    """Path of the workstation env file: <config_dir>/env."""
    return config_dir() / "env"
