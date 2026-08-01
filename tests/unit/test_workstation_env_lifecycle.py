"""Lifecycle-faithful tests (spec § Testing 1, 2, 11).

These drive the REAL entry path in a SPAWNED interpreter — true process
isolation, so production import order is exercised without mutating the
test process's sys.modules (no stale module identities, no re-run
import-time plugin registration poisoning the in-process suite). Laziness
is observed via sentinel files the command values would create if executed
— no cross-process spying needed.
"""

import os
import subprocess
import sys

import pytest


def _run_child(tmp_path, script: str) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["GRAFTPUNK_CONFIG_DIR"] = str(tmp_path)
    env.pop("GRAFTPUNK_BROWSER_EXECUTABLE_PATH", None)
    return subprocess.run(  # noqa: S603
        [sys.executable, "-c", script], capture_output=True, text=True, env=env
    )


def _write_env_file(tmp_path, content: str) -> None:
    # Real workstation env files are always written via workstation_env's
    # _atomic_write, which chmods 0600 on every write. Match that here —
    # otherwise the default umask leaves the file group/other-readable,
    # workstation_env.load() logs a workstation_env_permissive_mode
    # warning, and (since this fires during ensure_bootstrap(), before
    # cli/main.py's configure_logging() call rewires structlog to
    # stderr) that warning lands on stdout via structlog's default
    # PrintLoggerFactory — polluting the exact stdout match below with
    # noise unrelated to the ordering/laziness invariant under test.
    path = tmp_path / "env"
    path.write_text(content)
    path.chmod(0o600)


def test_real_order_static_visibility(tmp_path):
    # Spec Testing #1: importing the real CLI module runs bootstrap BEFORE
    # import-time plugin discovery constructs the settings singleton. This
    # test fails if injection ever moves after settings construction.
    _write_env_file(tmp_path, "GRAFTPUNK_BROWSER_EXECUTABLE_PATH=/from/file/chrome\n")
    result = _run_child(
        tmp_path,
        "import graftpunk.cli.main\n"
        "from graftpunk.config import get_settings\n"
        "print(get_settings().browser_executable_path)\n",
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "/from/file/chrome"


def test_laziness_regression_help_runs_zero_commands(tmp_path):
    # Spec Testing #2: with command entries present (credentials AND an
    # allowlisted settings field), gp --help must execute ZERO of them.
    # Each command value would create a sentinel file if it ever ran.
    _write_env_file(
        tmp_path,
        f"SHOPKEEP_PASSWORD=$(touch {tmp_path}/CRED_EVALUATED; echo secret)\n"
        f"GRAFTPUNK_BROWSER_EXECUTABLE_PATH=$(touch {tmp_path}/SETTING_EVALUATED; echo /lazy)\n",
    )
    result = _run_child(
        tmp_path,
        "from typer.testing import CliRunner\n"
        "from graftpunk.cli.main import app\n"
        "r = CliRunner().invoke(app, ['--help'])\n"
        "raise SystemExit(r.exit_code)\n",
    )
    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "CRED_EVALUATED").exists()
    assert not (tmp_path / "SETTING_EVALUATED").exists()


def test_bootstrap_warning_goes_to_stderr_not_stdout(tmp_path):
    # Reviewer finding: ensure_bootstrap() used to run BEFORE
    # configure_logging(), so any warning it emits (e.g. permissive file
    # mode) printed via structlog's built-in default PrintLogger (stdout)
    # instead of the CLI's configured stderr renderer -- corrupting piped
    # `gp` output for CSV/JSON consumers. A deliberately 0644 env file
    # triggers workstation_env_permissive_mode at import time, inside
    # cli.main's module-scope ensure_bootstrap() call. Calls the real Typer
    # app object directly (not CliRunner, which redirects sys.stdout/stderr
    # to an in-memory buffer that never reaches this test's subprocess-level
    # capture) so stdout/stderr are exactly what a real `gp` invocation
    # would produce.
    path = tmp_path / "env"
    path.write_text("K=v\n")
    path.chmod(0o644)
    result = _run_child(
        tmp_path,
        "import sys\n"
        "from graftpunk.cli.main import app\n"
        "try:\n"
        "    app(['config', 'path'])\n"
        "except SystemExit as e:\n"
        "    sys.exit(e.code or 0)\n",
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(path)  # stdout carries ONLY the path
    assert "workstation_env_permissive_mode" in result.stderr


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses permission bits")
def test_unreadable_env_file_does_not_crash_gp(tmp_path):
    # Reviewer finding (A1): load()'s unguarded stat()/read_text() used to
    # let an unreadable file crash EVERY `gp` invocation (via
    # ensure_bootstrap() at cli/main.py module scope) with a raw
    # PermissionError traceback and no in-tool recovery -- including `gp
    # config path`, which needs no file access at all. Same rationale as
    # test_bootstrap_warning_goes_to_stderr_not_stdout above for spawning a
    # real subprocess rather than using CliRunner.
    path = tmp_path / "env"
    path.write_text("K=v\n")
    path.chmod(0o000)
    try:
        result = _run_child(
            tmp_path,
            "import sys\n"
            "from graftpunk.cli.main import app\n"
            "try:\n"
            "    app(['config', 'path'])\n"
            "except SystemExit as e:\n"
            "    sys.exit(e.code or 0)\n",
        )
    finally:
        path.chmod(0o600)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(path)  # stdout carries ONLY the path
    assert "workstation_env_unreadable" in result.stderr


def test_library_reach_without_cli_import(tmp_path):
    # Spec Testing #11: a consumer that never imports graftpunk.cli (e.g.
    # grocerbot's in-process GraftpunkClient) sees file statics identically —
    # ensure_bootstrap() lives inside get_settings(), not in the CLI.
    # (Match exactly graftpunk.cli / graftpunk.cli.* — a bare startswith
    # would false-positive on graftpunk.client, which the package __init__
    # imports.)
    _write_env_file(tmp_path, "GRAFTPUNK_BROWSER_EXECUTABLE_PATH=/from/file/chrome\n")
    result = _run_child(
        tmp_path,
        "import sys\n"
        "from graftpunk.config import get_settings\n"
        "assert not any(m == 'graftpunk.cli' or m.startswith('graftpunk.cli.')\n"
        "               for m in sys.modules), 'CLI modules leaked into library path'\n"
        "print(get_settings().browser_executable_path)\n",
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "/from/file/chrome"
