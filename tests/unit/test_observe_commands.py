"""resolve_run, gp observe digest, and gp observe fixtures (plugin tooling spec, 2026-09-11)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

# digest_cmd and fixtures_cmd: unused directly (exercised through the CLI
# below), imported anyway as an existence check that the names are really on
# the module.
from graftpunk.cli.observe_commands import (  # noqa: F401
    digest_cmd,
    fixtures_cmd,
    register,
    resolve_run,
)

runner = CliRunner()


def _git(argv: list[str], cwd: Path) -> None:
    # argv is always a fixed git invocation built by this test module, never
    # untrusted input; a runtime list argument (rather than a literal) also
    # keeps ruff's S607 (partial executable path) from flagging every call
    # site below, matching graftpunk.devtools.captures's own convention.
    subprocess.run(argv, cwd=cwd, check=True)  # noqa: S603


def _build_app() -> typer.Typer:
    app = typer.Typer()
    observe_app = typer.Typer(name="observe")
    register(observe_app)
    app.add_typer(observe_app)
    return app


def _write_run(base_dir: Path, session: str, run_id: str, entries: list[dict]) -> Path:
    run_dir = base_dir / session / run_id
    run_dir.mkdir(parents=True)
    har = {"log": {"version": "1.2", "entries": entries}}
    (run_dir / "network.har").write_text(json.dumps(har))
    return run_dir


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


class TestResolveRun:
    def test_defaults_to_the_newest_run(self, tmp_path: Path) -> None:
        (tmp_path / "myshop" / "20260101-080000").mkdir(parents=True)
        (tmp_path / "myshop" / "20260101-120000").mkdir(parents=True)
        run_dir = resolve_run("myshop", None, base_dir=tmp_path)
        assert run_dir.name == "20260101-120000"

    def test_explicit_run_id(self, tmp_path: Path) -> None:
        (tmp_path / "myshop" / "run-specific").mkdir(parents=True)
        run_dir = resolve_run("myshop", "run-specific", base_dir=tmp_path)
        assert run_dir.name == "run-specific"

    def test_missing_session_exits_1(self, tmp_path: Path) -> None:
        with pytest.raises(typer.Exit) as exc:
            resolve_run("ghost", None, base_dir=tmp_path)
        assert exc.value.exit_code == 1

    def test_missing_run_id_exits_1(self, tmp_path: Path) -> None:
        (tmp_path / "myshop").mkdir(parents=True)
        with pytest.raises(typer.Exit):
            resolve_run("myshop", "no-such-run", base_dir=tmp_path)


class TestDigestCommand:
    def test_digest_over_a_run(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("graftpunk.cli.observe_commands.OBSERVE_BASE_DIR", tmp_path)
        _write_run(
            tmp_path, "myshop", "run-1", [_entry("GET", "https://api.myshop.example.com/orders")]
        )
        app = _build_app()
        result = runner.invoke(app, ["observe", "digest", "myshop"])
        assert result.exit_code == 0, result.output
        assert "## Summary" in result.output

    def test_digest_json_flag(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("graftpunk.cli.observe_commands.OBSERVE_BASE_DIR", tmp_path)
        _write_run(
            tmp_path, "myshop", "run-1", [_entry("GET", "https://api.myshop.example.com/orders")]
        )
        app = _build_app()
        # graftpunk.logging's own docstring: "a later structlog.reset_defaults()
        # restores structlog's stdout builtins and graftpunk does not
        # re-arm the default." Several other test modules call
        # reset_defaults() (test_chrome_orphans.py, test_site_requests.py,
        # and others), which is process-global structlog state; whichever
        # of those tests happens to run earlier in this worker leaves
        # structlog back on its unconfigured default (an unfiltered
        # PrintLogger dynamically bound to sys.stdout), so
        # parse_har_file's INFO "har_file_parsed" event prints straight
        # onto stdout ahead of the JSON and breaks json.loads (found
        # running this test after TestResolveRun's tests: reproduces
        # deterministically in that order, passes in isolation). Real `gp`
        # usage never hits this: main.py's bootstrap always calls
        # configure_logging() before a command runs. This test bypasses
        # that bootstrap (it builds the Typer app directly from
        # observe_commands.register), so it restores the same guarantee
        # explicitly. Deviation from the brief, which asserted on
        # result.output without this.
        from graftpunk.logging import configure_logging

        configure_logging(level="WARNING")
        result = runner.invoke(app, ["observe", "digest", "myshop", "--json"])
        assert result.exit_code == 0
        assert json.loads(result.stdout)["primary_host"] == "api.myshop.example.com"

    def test_digest_har_flag_on_a_bare_file(self, tmp_path: Path) -> None:
        har_path = tmp_path / "network.har"
        har_path.write_text(
            json.dumps(
                {
                    "log": {
                        "version": "1.2",
                        "entries": [_entry("GET", "https://api.myshop.example.com/orders")],
                    }
                }
            )
        )
        app = _build_app()
        result = runner.invoke(app, ["observe", "digest", "--har", str(har_path)])
        assert result.exit_code == 0
        assert "## Summary" in result.output

    def test_digest_refuses_both_session_and_har(self, tmp_path: Path) -> None:
        app = _build_app()
        result = runner.invoke(
            app, ["observe", "digest", "myshop", "--har", str(tmp_path / "x.har")]
        )
        assert result.exit_code == 1
        assert "not both" in result.output.lower()

    def test_digest_missing_har_file_is_one_line_error(self, tmp_path: Path) -> None:
        app = _build_app()
        result = runner.invoke(app, ["observe", "digest", "--har", str(tmp_path / "missing.har")])
        assert result.exit_code == 1
        assert "not found" in result.output.lower()

    def test_digest_missing_session_or_har_is_an_error(self) -> None:
        app = _build_app()
        result = runner.invoke(app, ["observe", "digest"])
        assert result.exit_code == 1


class TestFixturesCommand:
    def test_writes_matching_files_with_sidecars(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        observe_base = tmp_path / "observe"
        monkeypatch.setattr("graftpunk.cli.observe_commands.OBSERVE_BASE_DIR", observe_base)
        _write_run(
            observe_base,
            "myshop",
            "run-1",
            [_entry("GET", "https://api.myshop.example.com/orders/1", body='{"id": 1}')],
        )
        out_dir = tmp_path / "out"
        app = _build_app()
        result = runner.invoke(
            app,
            [
                "observe",
                "fixtures",
                "myshop",
                "--match",
                "GET /orders/{order_id}",
                "--out",
                str(out_dir),
            ],
        )
        assert result.exit_code == 0, result.output
        # Excludes sidecars the same way graftpunk.testing.FixtureSession does
        # (tests/../src/graftpunk/testing/__init__.py: "not p.name.endswith(
        # '.meta.json')"): a sidecar's name is the fixture's name plus
        # ".meta.json", which itself ends in ".json", so a bare "*.json" glob
        # matches both (found running this test unmodified: 2 matches, not
        # 1). Deviation from the brief, which globbed without excluding it.
        written = [
            p for p in out_dir.glob("get_orders_*.json") if not p.name.endswith(".meta.json")
        ]
        assert len(written) == 1
        assert json.loads(written[0].read_text()) == {"id": 1}
        assert (out_dir / f"{written[0].name}.meta.json").exists()

    def test_limit_caps_files_per_template(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        observe_base = tmp_path / "observe"
        monkeypatch.setattr("graftpunk.cli.observe_commands.OBSERVE_BASE_DIR", observe_base)
        entries = [
            _entry("GET", f"https://api.myshop.example.com/orders/{i}", body=json.dumps({"id": i}))
            for i in range(10)
        ]
        _write_run(observe_base, "myshop", "run-1", entries)
        out_dir = tmp_path / "out"
        app = _build_app()
        result = runner.invoke(
            app,
            [
                "observe",
                "fixtures",
                "myshop",
                "--match",
                "GET /orders/{order_id}",
                "--out",
                str(out_dir),
                "--limit",
                "2",
            ],
        )
        assert result.exit_code == 0
        # See test_writes_matching_files_with_sidecars: excludes sidecars.
        written = [
            p for p in out_dir.glob("get_orders_*.json") if not p.name.endswith(".meta.json")
        ]
        assert len(written) == 2

    def test_refuses_a_tracked_target_without_allow_tracked(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        observe_base = tmp_path / "observe"
        monkeypatch.setattr("graftpunk.cli.observe_commands.OBSERVE_BASE_DIR", observe_base)
        _write_run(
            observe_base,
            "myshop",
            "run-1",
            [_entry("GET", "https://api.myshop.example.com/orders/1")],
        )
        repo = tmp_path / "repo"
        repo.mkdir()
        _git(["git", "init", "-q"], repo)
        _git(["git", "config", "user.email", "alice@example.com"], repo)
        _git(["git", "config", "user.name", "alice"], repo)
        out_dir = repo / "tests" / "captures"
        out_dir.mkdir(parents=True)
        tracked = out_dir / "get_orders_{order_id}.json"
        tracked.write_text("{}")
        _git(["git", "add", "-A"], repo)
        _git(["git", "commit", "-q", "-m", "seed"], repo)

        app = _build_app()
        result = runner.invoke(
            app,
            [
                "observe",
                "fixtures",
                "myshop",
                "--match",
                "GET /orders/{order_id}",
                "--out",
                str(out_dir),
            ],
        )
        assert result.exit_code == 1
        assert "tracked" in result.output.lower()

    def test_missing_match_is_an_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("graftpunk.cli.observe_commands.OBSERVE_BASE_DIR", tmp_path)
        app = _build_app()
        result = runner.invoke(app, ["observe", "fixtures", "myshop"])
        assert result.exit_code == 1
