"""gp plugin new through the Typer runner (plugin tooling spec, 2026-09-11)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from graftpunk.cli.scaffold_commands import plugin_app

runner = CliRunner()


def _build_app() -> typer.Typer:
    app = typer.Typer()
    app.add_typer(plugin_app)
    return app


def _entry(
    method: str, url: str, *, content_type: str = "application/json", body: str = "{}"
) -> dict:
    return {
        "startedDateTime": "2026-09-10T10:00:00.000Z",
        "time": 1,
        "request": {"method": method, "url": url, "headers": [], "cookies": [], "queryString": []},
        "response": {
            "status": 200,
            "statusText": "OK",
            "headers": [{"name": "Content-Type", "value": content_type}],
            "cookies": [],
            "content": {"mimeType": content_type, "text": body, "size": len(body)},
        },
    }


class TestPluginNewHappyPath:
    def test_new_project_in_target_dir(self, tmp_path: Path) -> None:
        result = runner.invoke(
            _build_app(),
            [
                "plugin",
                "new",
                "myshop",
                "--url",
                "https://myshop.example.com",
                "--dir",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 0, result.output
        assert (tmp_path / "src" / "graftpunk_myshop" / "plugin.py").exists()

    def test_reserved_name_refused_through_the_real_app(self) -> None:
        """Uses the real graftpunk.cli.main.app, whose plugin_app and existing
        groups (observe, session, http, config, keepalive) are all registered
        by the time register_plugin_commands runs, so 'observe' is reserved."""
        from graftpunk.cli.main import app as real_app

        result = runner.invoke(real_app, ["plugin", "new", "observe"])
        assert result.exit_code == 1
        assert "reserved" in result.output.lower()

    def test_plugin_group_itself_is_reserved_through_the_real_app(self) -> None:
        """plugin_app is attached before register_plugin_commands runs, so
        'plugin' is in the reserved set by the time it derives it."""
        from graftpunk.cli.main import app as real_app

        result = runner.invoke(real_app, ["plugin", "new", "plugin"])
        assert result.exit_code == 1
        assert "reserved" in result.output.lower()

    def test_conflict_refusal(self, tmp_path: Path) -> None:
        (tmp_path / "README.md").write_text("already here")
        result = runner.invoke(
            _build_app(),
            [
                "plugin",
                "new",
                "myshop",
                "--url",
                "https://myshop.example.com",
                "--dir",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 1
        assert "README.md" in result.output

    def test_bad_backend_refused(self, tmp_path: Path) -> None:
        result = runner.invoke(
            _build_app(),
            ["plugin", "new", "myshop", "--dir", str(tmp_path), "--backend", "carrier-pigeon"],
        )
        assert result.exit_code == 1

    def test_rejects_invalid_name(self, tmp_path: Path) -> None:
        result = runner.invoke(
            _build_app(),
            ["plugin", "new", "2fa-site", "--dir", str(tmp_path)],
        )
        assert result.exit_code == 1
        assert "letter" in result.output.lower()
        assert not (tmp_path / "pyproject.toml").exists()
        assert not (tmp_path / "src").exists()


class TestPluginNewFromRun:
    def test_from_run_emits_login_config_token_config_and_stubs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        observe_base = tmp_path / "observe"
        monkeypatch.setattr("graftpunk.cli.observe_commands.OBSERVE_BASE_DIR", observe_base)
        run_dir = observe_base / "myshop" / "run-1"
        run_dir.mkdir(parents=True)
        entries = [
            _entry(
                "GET",
                "https://api.myshop.example.com/login",
                content_type="text/html",
                body=(
                    '<meta name="csrf-token" content="abc">'
                    '<form action="/login" method="post">'
                    '<input type="email" name="email" id="email-field">'
                    '<input type="password" name="password" id="pw">'
                    '<button type="submit" id="go">Go</button>'
                    "</form>"
                ),
            ),
            _entry(
                "POST",
                "https://api.myshop.example.com/login",
                content_type="application/json",
                body="{}",
            ),
            _entry("GET", "https://api.myshop.example.com/orders/1", body='{"id": 1}'),
        ]
        har = {"log": {"version": "1.2", "entries": entries}}
        (run_dir / "network.har").write_text(json.dumps(har))

        target = tmp_path / "out"
        result = runner.invoke(
            _build_app(),
            ["plugin", "new", "myshop", "--from-run", "myshop", "run-1", "--dir", str(target)],
        )
        assert result.exit_code == 0, result.output
        plugin_code = (target / "src" / "graftpunk_myshop" / "plugin.py").read_text()
        assert "login_config = LoginConfig(" in plugin_code
        assert '"password": "#pw"' in plugin_code
        assert "@command(" in plugin_code
        assert 'base_url = "https://api.myshop.example.com"' in plugin_code


class TestGeneratedProjectPassesItsOwnGate:
    def test_new_project_is_ruff_clean(self, tmp_path: Path) -> None:
        result = runner.invoke(
            _build_app(),
            [
                "plugin",
                "new",
                "myshop",
                "--url",
                "https://myshop.example.com",
                "--dir",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 0, result.output
        check = subprocess.run(  # noqa: S603 - argv is fixed, not untrusted input
            [sys.executable, "-m", "ruff", "check", str(tmp_path)], capture_output=True, text=True
        )
        assert check.returncode == 0, check.stdout + check.stderr
        fmt = subprocess.run(  # noqa: S603 - argv is fixed, not untrusted input
            [sys.executable, "-m", "ruff", "format", "--check", str(tmp_path)],
            capture_output=True,
            text=True,
        )
        assert fmt.returncode == 0, fmt.stdout + fmt.stderr

    def test_generated_tests_pass_against_a_generated_fixture_and_sidecar(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        observe_base = tmp_path / "observe"
        monkeypatch.setattr("graftpunk.cli.observe_commands.OBSERVE_BASE_DIR", observe_base)
        run_dir = observe_base / "myshop" / "run-1"
        run_dir.mkdir(parents=True)
        entries = [_entry("GET", "https://api.myshop.example.com/orders/1", body='{"id": 1}')]
        har = {"log": {"version": "1.2", "entries": entries}}
        (run_dir / "network.har").write_text(json.dumps(har))

        target = tmp_path / "out"
        result = runner.invoke(
            _build_app(),
            ["plugin", "new", "myshop", "--from-run", "myshop", "run-1", "--dir", str(target)],
        )
        assert result.exit_code == 0, result.output

        # The developer's hand-derivation step: copy the capture's shape into
        # a fixture. "1" is what the generated test's own path-param
        # placeholder produces, so the fixture name below is what the
        # request under test actually looks up.
        fixtures_dir = target / "tests" / "fixtures"
        (fixtures_dir / "get_orders_{order_id}.json").write_text('{"id": 1}')

        env = {**os.environ, "PYTHONPATH": str(target / "src")}
        pytest_result = subprocess.run(  # noqa: S603 - argv is fixed, not untrusted input
            [sys.executable, "-m", "pytest", "tests", "-q"],
            cwd=target,
            capture_output=True,
            text=True,
            env=env,
        )
        assert pytest_result.returncode == 0, pytest_result.stdout + pytest_result.stderr


class TestSuiteModeLeavesRestOfPyprojectByteIdentical:
    def test_only_the_two_known_edits_change(self, tmp_path: Path) -> None:
        pyproject = tmp_path / "pyproject.toml"
        original = (
            '[project]\nname = "mysuite"\n\n'
            '[project.entry-points."graftpunk.plugins"]\n'
            'existing = "mysuite.existing:ExistingPlugin"\n\n'
            "[tool.hatch.build.targets.wheel]\n"
            'packages = ["src/mysuite"]\n\n'
            "[tool.ruff]\n"
            "line-length = 88\n"
        )
        pyproject.write_text(original)
        result = runner.invoke(
            _build_app(),
            [
                "plugin",
                "new",
                "widgets",
                "--url",
                "https://myshop.example.com",
                "--dir",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 0, result.output
        after = pyproject.read_text()
        assert "[tool.ruff]" in after
        assert "line-length = 88" in after
        assert 'existing = "mysuite.existing:ExistingPlugin"' in after
