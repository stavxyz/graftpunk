"""gp plugin new through the Typer runner (plugin tooling spec, 2026-09-11)."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest
import structlog
import typer
from structlog.testing import capture_logs
from typer.testing import CliRunner

from graftpunk.cli.scaffold_commands import plugin_app
from graftpunk.logging import configure_logging

runner = CliRunner()


def _plain(text: str) -> str:
    """*text* without ANSI escapes. Rich colours paths and usage errors when a
    terminal or FORCE_COLOR is detected, and the codes land inside the words
    these tests look for; CI and local runs differ on that, the words do not."""
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


_LOG_LINE_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} \[")


def _without_log_lines(text: str) -> str:
    """*text* without structlog's console-rendered lines.

    Whether a LOG.debug call actually renders into captured output depends
    on ambient structlog configuration left behind by whichever test in this
    worker process ran last (the autouse `_reset_structlog` fixture resets
    structlog to its unfiltered default after every test, and nothing
    re-applies the CLI's own WARNING-level config until the next `gp`
    process starts for real), not on anything the command being tested
    chooses to print. Comparing two invocations' own console text should not
    depend on that.
    """
    return "\n".join(line for line in text.splitlines() if not _LOG_LINE_RE.match(line))


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

    def test_reserved_name_refused_through_the_real_app(self, tmp_path: Path) -> None:
        """Uses the real graftpunk.cli.main.app, whose plugin_app and existing
        groups (observe, session, http, config, keepalive) are all registered
        by the time register_plugin_commands runs, so 'observe' is reserved."""
        from graftpunk.cli.main import app as real_app

        before = set(tmp_path.iterdir())
        result = runner.invoke(real_app, ["plugin", "new", "observe", "--dir", str(tmp_path)])
        assert result.exit_code == 1
        assert "reserved" in result.output.lower()
        assert set(tmp_path.iterdir()) == before

    def test_plugin_group_itself_is_reserved_through_the_real_app(self, tmp_path: Path) -> None:
        """register() attaches plugin_app before register_plugin_commands runs,
        so 'plugin' is in the snapshotted reserved set."""
        from graftpunk.cli.main import app as real_app

        before = set(tmp_path.iterdir())
        result = runner.invoke(real_app, ["plugin", "new", "plugin", "--dir", str(tmp_path)])
        assert result.exit_code == 1
        assert "reserved" in result.output.lower()
        assert set(tmp_path.iterdir()) == before

    def test_conflict_refusal(self, tmp_path: Path) -> None:
        (tmp_path / "README.md").write_text("already here")
        before = set(tmp_path.iterdir())
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
        assert set(tmp_path.iterdir()) == before

    def test_bad_backend_refused(self, tmp_path: Path) -> None:
        before = set(tmp_path.iterdir())
        result = runner.invoke(
            _build_app(),
            ["plugin", "new", "myshop", "--dir", str(tmp_path), "--backend", "carrier-pigeon"],
        )
        assert result.exit_code == 1
        assert "nodriver" in result.output
        assert "selenium" in result.output
        assert set(tmp_path.iterdir()) == before

    def test_rejects_invalid_name(self, tmp_path: Path) -> None:
        before = set(tmp_path.iterdir())
        result = runner.invoke(
            _build_app(),
            ["plugin", "new", "2fa-site", "--dir", str(tmp_path)],
        )
        assert result.exit_code == 1
        assert "letter" in result.output.lower()
        assert set(tmp_path.iterdir()) == before

    def test_run_without_from_run_refused(self, tmp_path: Path) -> None:
        before = set(tmp_path.iterdir())
        result = runner.invoke(
            _build_app(),
            ["plugin", "new", "myshop", "--run", "run-1", "--dir", str(tmp_path)],
        )
        assert result.exit_code == 1
        assert "--from-run" in result.output
        assert set(tmp_path.iterdir()) == before


def _write_run(observe_base: Path, session: str, run_id: str, *, url: str, body: str) -> None:
    run_dir = observe_base / session / run_id
    run_dir.mkdir(parents=True)
    entries = [_entry("GET", url, body=body)]
    har = {"log": {"version": "1.2", "entries": entries}}
    (run_dir / "network.har").write_text(json.dumps(har))


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
            [
                "plugin",
                "new",
                "myshop",
                "--from-run",
                "myshop",
                "--run",
                "run-1",
                "--dir",
                str(target),
            ],
        )
        assert result.exit_code == 0, result.output
        plugin_code = (target / "src" / "graftpunk_myshop" / "plugin.py").read_text()
        assert "login_config = LoginConfig(" in plugin_code
        assert '"password": "#pw"' in plugin_code
        assert "@command(" in plugin_code
        assert 'base_url = "https://api.myshop.example.com"' in plugin_code

    def test_from_run_alone_resolves_the_newest_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        observe_base = tmp_path / "observe"
        monkeypatch.setattr("graftpunk.cli.observe_commands.OBSERVE_BASE_DIR", observe_base)
        _write_run(
            observe_base,
            "myshop",
            "run-1",
            url="https://api.myshop.example.com/older",
            body='{"id": 1}',
        )
        _write_run(
            observe_base,
            "myshop",
            "run-2",
            url="https://api.myshop.example.com/newer",
            body='{"id": 2}',
        )

        target = tmp_path / "out"
        result = runner.invoke(
            _build_app(),
            ["plugin", "new", "myshop", "--from-run", "myshop", "--dir", str(target)],
        )
        assert result.exit_code == 0, result.output
        plugin_code = (target / "src" / "graftpunk_myshop" / "plugin.py").read_text()
        # resolve_run with no run_id picks the newest run (highest sorted
        # directory name): run-2, not run-1.
        assert "myshop/run-2" in plugin_code
        assert "myshop/run-1" not in plugin_code

    def test_from_run_with_explicit_run_resolves_that_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        observe_base = tmp_path / "observe"
        monkeypatch.setattr("graftpunk.cli.observe_commands.OBSERVE_BASE_DIR", observe_base)
        _write_run(
            observe_base,
            "myshop",
            "run-1",
            url="https://api.myshop.example.com/older",
            body='{"id": 1}',
        )
        _write_run(
            observe_base,
            "myshop",
            "run-2",
            url="https://api.myshop.example.com/newer",
            body='{"id": 2}',
        )

        target = tmp_path / "out"
        result = runner.invoke(
            _build_app(),
            [
                "plugin",
                "new",
                "myshop",
                "--from-run",
                "myshop",
                "--run",
                "run-1",
                "--dir",
                str(target),
            ],
        )
        assert result.exit_code == 0, result.output
        plugin_code = (target / "src" / "graftpunk_myshop" / "plugin.py").read_text()
        assert "myshop/run-1" in plugin_code
        assert "myshop/run-2" not in plugin_code


class TestPathListingsDoNotWrapMidWord:
    """Console.print's default wrapping breaks a path at 80 columns, so a
    listing meant to be copied could not be."""

    @staticmethod
    def _deep_target(tmp_path: Path) -> Path:
        return tmp_path / "a-fairly-long-directory-name" / "and-another-one-here" / "myshop"

    def test_the_written_file_listing_keeps_whole_paths(self, tmp_path: Path) -> None:
        target = self._deep_target(tmp_path)
        result = runner.invoke(
            _build_app(),
            [
                "plugin",
                "new",
                "myshop",
                "--url",
                "https://myshop.example.com",
                "--dir",
                str(target),
            ],
        )
        assert result.exit_code == 0, result.output
        expected = str(target / "src" / "graftpunk_myshop" / "plugin.py")
        assert len(expected) > 80
        assert expected in _plain(result.output)

    def test_the_conflict_listing_keeps_whole_paths(self, tmp_path: Path) -> None:
        target = self._deep_target(tmp_path)
        target.mkdir(parents=True)
        (target / "README.md").write_text("already here")
        result = runner.invoke(
            _build_app(),
            [
                "plugin",
                "new",
                "myshop",
                "--url",
                "https://myshop.example.com",
                "--dir",
                str(target),
            ],
        )
        assert result.exit_code == 1
        expected = str(target / "README.md")
        assert len(expected) > 80
        assert expected in _plain(result.output)


class TestNextStepsNamesTheFixtures:
    def test_the_fixture_path_for_a_templated_endpoint_is_named(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A --from-run project's suite fails on its first run until the
        fixtures exist, and nothing in the output said so."""
        observe_base = tmp_path / "observe"
        monkeypatch.setattr("graftpunk.cli.observe_commands.OBSERVE_BASE_DIR", observe_base)
        _write_run(
            observe_base,
            "myshop",
            "run-1",
            url="https://api.myshop.example.com/orders/1",
            body='{"id": 1}',
        )
        target = tmp_path / "out"

        result = runner.invoke(
            _build_app(),
            ["plugin", "new", "myshop", "--from-run", "myshop", "--dir", str(target)],
        )

        assert result.exit_code == 0, result.output
        plain_output = _plain(result.output)
        assert "Next:" in plain_output
        assert "tests/fixtures/get_orders_{order_id}.json" in plain_output
        assert "gp observe fixtures --help" in plain_output

    def test_a_project_with_no_endpoint_stubs_says_nothing(self, tmp_path: Path) -> None:
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
        assert "Next:" not in result.output


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
            [
                "plugin",
                "new",
                "myshop",
                "--from-run",
                "myshop",
                "--run",
                "run-1",
                "--dir",
                str(target),
            ],
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
        # A scaffolded project's first run is clean: no warnings summary at all.
        # The generated conftest used to both import graftpunk.testing.plugin and
        # list it in pytest_plugins, which pytest reports as a
        # PytestAssertRewriteWarning (final fix wave, 2026-09-12).
        assert "warnings summary" not in pytest_result.stdout.lower(), pytest_result.stdout

    def test_a_project_with_login_and_token_blocks_imports_and_instantiates(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A rendered tree carrying both blocks was only ever parsed, never
        imported: a LoginConfig or TokenConfig the framework rejects at
        construction time is a runtime failure ast.parse cannot see."""
        observe_base = tmp_path / "observe"
        monkeypatch.setattr("graftpunk.cli.observe_commands.OBSERVE_BASE_DIR", observe_base)
        run_dir = observe_base / "myshop" / "run-1"
        run_dir.mkdir(parents=True)
        login_page = (
            '<meta name="X-CSRF-Token" content="abc">'
            '<form action="/login" method="post">'
            '<input type="email" name="email" id="email-field">'
            '<input type="password" name="password" id="pw">'
            '<button type="submit" id="go">Go</button>'
            "</form>"
        )
        entries = [
            _entry(
                "GET",
                "https://api.myshop.example.com/login",
                content_type="text/html",
                body=login_page,
            ),
            _entry("GET", "https://api.myshop.example.com/orders/1", body='{"id": 1}'),
        ]
        # The header half of the token pair: the meta tag above supplies the
        # value, this request carries it back.
        entries[1]["request"]["headers"] = [{"name": "X-CSRF-Token", "value": "abc"}]
        har = {"log": {"version": "1.2", "entries": entries}}
        (run_dir / "network.har").write_text(json.dumps(har))

        target = tmp_path / "out"
        result = runner.invoke(
            _build_app(),
            [
                "plugin",
                "new",
                "myshop",
                "--from-run",
                "myshop",
                "--run",
                "run-1",
                "--dir",
                str(target),
            ],
        )
        assert result.exit_code == 0, result.output
        plugin_code = (target / "src" / "graftpunk_myshop" / "plugin.py").read_text()
        assert "login_config = LoginConfig(" in plugin_code
        assert "token_config = TokenConfig(" in plugin_code

        script = (
            "from graftpunk_myshop.plugin import MyshopPlugin\n"
            "plugin = MyshopPlugin()\n"
            "assert plugin.site_name == 'myshop'\n"
            "assert plugin.login_config is not None\n"
            "assert plugin.token_config is not None\n"
            "print('OK')\n"
        )
        env = {**os.environ, "PYTHONPATH": str(target / "src")}
        run = subprocess.run(  # noqa: S603 - argv is fixed, not untrusted input
            [sys.executable, "-c", script], capture_output=True, text=True, env=env, timeout=60
        )
        assert run.returncode == 0, run.stdout + run.stderr
        assert "OK" in run.stdout


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


class TestTwoPluginsInOneSuite:
    def test_the_second_add_succeeds_and_leaves_the_rest_of_pyproject_alone(
        self, tmp_path: Path
    ) -> None:
        """The second add used to refuse on the tests/fixtures/.gitkeep the
        first one created."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\nname = "mysuite"\n\n'
            '[project.entry-points."graftpunk.plugins"]\n'
            'existing = "mysuite.existing:ExistingPlugin"\n\n'
            "[tool.hatch.build.targets.wheel]\n"
            'packages = ["src/mysuite"]\n\n'
            "[tool.ruff]\n"
            "line-length = 88\n"
        )
        for name in ("widgets", "gadgets"):
            result = runner.invoke(
                _build_app(),
                [
                    "plugin",
                    "new",
                    name,
                    "--url",
                    "https://myshop.example.com",
                    "--dir",
                    str(tmp_path),
                ],
            )
            assert result.exit_code == 0, result.output

        assert (tmp_path / "src" / "graftpunk_widgets" / "plugin.py").exists()
        assert (tmp_path / "src" / "graftpunk_gadgets" / "plugin.py").exists()
        assert (tmp_path / "tests" / "fixtures" / "widgets" / ".gitkeep").exists()
        assert (tmp_path / "tests" / "fixtures" / "gadgets" / ".gitkeep").exists()
        after = pyproject.read_text()
        assert 'widgets = "graftpunk_widgets.plugin:WidgetsPlugin"' in after
        assert 'gadgets = "graftpunk_gadgets.plugin:GadgetsPlugin"' in after
        assert '"src/graftpunk_widgets"' in after
        assert '"src/graftpunk_gadgets"' in after
        assert 'existing = "mysuite.existing:ExistingPlugin"' in after
        assert "line-length = 88" in after


class TestPyprojectEditErrorRefusedCleanly:
    def test_include_only_wheel_table_refuses_without_a_traceback(self, tmp_path: Path) -> None:
        pyproject = tmp_path / "pyproject.toml"
        original = (
            '[project]\nname = "mysuite"\n\n'
            '[project.entry-points."graftpunk.plugins"]\n'
            'existing = "mysuite.existing:ExistingPlugin"\n\n'
            "[tool.hatch.build.targets.wheel]\n"
            'include = ["src/mysuite/**"]\n'
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
        assert result.exit_code == 1
        assert "include" in result.output.lower()
        assert "Traceback" not in result.output
        assert result.exception is None or isinstance(result.exception, SystemExit)
        # A refusal leaves the suite byte-identical: no partial pyproject.toml
        # edit, no new package directory, no new test module, no .gitignore
        # created where none existed.
        assert pyproject.read_text() == original
        assert not (tmp_path / "src" / "graftpunk_widgets").exists()
        assert not (tmp_path / "tests" / "test_widgets.py").exists()
        assert not (tmp_path / ".gitignore").exists()


class TestUnwritableTargetDirIsARefusal:
    def test_unwritable_dir_refuses_without_a_traceback(self, tmp_path: Path) -> None:
        if os.geteuid() == 0:
            pytest.skip("root ignores mode bits, so the write would succeed")
        readonly = tmp_path / "readonly"
        readonly.mkdir()
        readonly.chmod(0o500)
        try:
            result = runner.invoke(
                _build_app(),
                [
                    "plugin",
                    "new",
                    "myshop",
                    "--url",
                    "https://myshop.example.com",
                    "--dir",
                    str(readonly),
                ],
            )
            assert result.exit_code == 1, result.output
            assert "could not write" in result.output.lower()
            assert "Traceback" not in result.output
            assert result.exception is None or isinstance(result.exception, SystemExit)
            assert list(readonly.iterdir()) == []
        finally:
            # Restored so pytest's own tmp_path cleanup can remove the tree.
            readonly.chmod(0o700)


@contextmanager
def _captured_debug_logs():
    """capture_logs, with the level lowered so debug events reach it.

    structlog's filtering bound logger drops a debug call before any processor
    runs, and capture_logs replaces processors only, so a refusal logged at
    debug is invisible to it at the CLI's own WARNING default. The previous
    configuration is restored on the way out.
    """
    previous = structlog.get_config()
    configure_logging(level="DEBUG")
    try:
        with capture_logs() as events:
            yield events
    finally:
        structlog.configure(**previous)


class TestRefusalReasons:
    """A directory holding someone else's project and a name the generator
    cannot use are different conditions; both logged reason="invalid_name"."""

    @staticmethod
    def _reasons(events: list[dict]) -> list[str]:
        return [e.get("reason") for e in events if e.get("event") == "scaffold_refused"]

    def test_a_non_suite_pyproject_logs_its_own_reason(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "unrelated-package"\n')
        with _captured_debug_logs() as events:
            result = runner.invoke(
                _build_app(),
                ["plugin", "new", "myshop", "--dir", str(tmp_path)],
            )
        assert result.exit_code == 1, result.output
        assert "entry-point" in result.output.lower()
        assert self._reasons(events) == ["not_a_plugin_suite"]

    def test_an_invalid_name_still_logs_invalid_name(self, tmp_path: Path) -> None:
        with _captured_debug_logs() as events:
            result = runner.invoke(
                _build_app(),
                ["plugin", "new", "2fa-site", "--dir", str(tmp_path)],
            )
        assert result.exit_code == 1, result.output
        assert self._reasons(events) == ["invalid_name"]

    def test_a_refusal_logs_at_debug_so_the_console_line_stands_alone(self, tmp_path: Path) -> None:
        """LOG.warning is reserved for an anomaly the console does not report.
        Every refusal already prints its own red line, and a warning beside it
        made a plain refusal two lines on two streams."""
        with _captured_debug_logs() as events:
            runner.invoke(_build_app(), ["plugin", "new", "2fa-site", "--dir", str(tmp_path)])
        refusals = [e for e in events if e.get("event") == "scaffold_refused"]
        assert refusals
        assert {e["log_level"] for e in refusals} == {"debug"}

    def test_a_conflict_refusal_logs_at_debug_in_both_modules(self, tmp_path: Path) -> None:
        (tmp_path / "README.md").write_text("already here")
        with _captured_debug_logs() as events:
            runner.invoke(
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
        logged = [
            e for e in events if e.get("event") in {"scaffold_refused", "scaffold_write_refused"}
        ]
        assert {e["event"] for e in logged} == {"scaffold_refused", "scaffold_write_refused"}
        assert {e["log_level"] for e in logged} == {"debug"}


class TestReservedNamesSnapshot:
    def test_a_later_site_plugin_name_is_not_in_the_snapshot(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """register() snapshots reserved names once, at attach time. A site
        plugin's own sub-app, mounted onto the same app afterward (exactly
        what register_plugin_commands does next), must not retroactively
        become reserved: the snapshot is not a live query."""
        from graftpunk.cli import scaffold_commands
        from graftpunk.cli.scaffold_commands import register, reserved_cli_names

        # register() overwrites the module-global _reserved_names snapshot,
        # which real_app already populated at import time from its own full
        # command tree (other tests in this file invoke gp plugin new
        # through real_app and rely on that real snapshot). Restore it so
        # this test's throwaway app doesn't leave that global pointing at a
        # near-empty snapshot for whichever test in this worker runs next.
        monkeypatch.setattr(scaffold_commands, "_reserved_names", scaffold_commands._reserved_names)

        app = typer.Typer()
        register(app)
        assert "myshop" not in reserved_cli_names()

        site_app = typer.Typer(name="myshop")
        app.add_typer(site_app)

        assert "myshop" not in reserved_cli_names()


class TestCheckName:
    """--check-name answers "is this name acceptable" with gp plugin new's own
    validation, writing nothing (graft skill spec, 2026-09-21)."""

    def test_a_valid_name_is_accepted_and_nothing_is_written(self, tmp_path: Path) -> None:
        from graftpunk.cli.main import app as real_app

        before = set(tmp_path.iterdir())
        result = runner.invoke(
            real_app, ["plugin", "new", "myshop", "--check-name", "--dir", str(tmp_path)]
        )
        assert result.exit_code == 0, result.output
        assert "acceptable" in _plain(result.output)
        assert set(tmp_path.iterdir()) == before

    @pytest.mark.parametrize("name", ["observe", "2fa-site", "a" * 41])
    def test_a_refusal_is_the_same_text_gp_plugin_new_prints(
        self, tmp_path: Path, name: str
    ) -> None:
        from graftpunk.cli.main import app as real_app

        # before, not an empty-dir assertion: the isolated_config autouse
        # fixture (tests/conftest.py) already created tmp_path/graftpunk for
        # this test's own settings before the test body ever runs.
        before = set(tmp_path.iterdir())
        checked = runner.invoke(real_app, ["plugin", "new", name, "--check-name"])
        created = runner.invoke(real_app, ["plugin", "new", name, "--dir", str(tmp_path)])
        assert checked.exit_code == created.exit_code == 1
        assert _without_log_lines(_plain(checked.output)) == _without_log_lines(
            _plain(created.output)
        )
        assert set(tmp_path.iterdir()) == before
