"""resolve_run, gp observe digest, and gp observe fixtures (plugin tooling spec, 2026-09-11)."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
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
    observe_app,
    resolve_run,
)

runner = CliRunner()


def _plain(text: str) -> str:
    """*text* without ANSI escapes. Rich colours paths and usage errors when a
    terminal or FORCE_COLOR is detected, and the codes land inside the words
    these tests look for; CI and local runs differ on that, the words do not."""
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def _git(argv: list[str], cwd: Path) -> None:
    # argv is always a fixed git invocation built by this test module, never
    # untrusted input; a runtime list argument (rather than a literal) also
    # keeps ruff's S607 (partial executable path) from flagging every call
    # site below, matching graftpunk.devtools.captures's own convention.
    subprocess.run(argv, cwd=cwd, check=True)  # noqa: S603


@pytest.fixture(autouse=True)
def _no_session_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep GRAFTPUNK_SESSION unset, so resolve_session(None) stays harmless
    for every test in this module, as _build_app's comment assumes."""
    monkeypatch.delenv("GRAFTPUNK_SESSION", raising=False)


def _build_app() -> typer.Typer:
    # Mounts the real observe sub-app, so observe_callback runs before every
    # command here; these tests rely on resolve_session(None) being harmless.
    app = typer.Typer()
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


def _invoke_fixtures(out_dir: Path, *extra: str):
    """``gp observe fixtures myshop --match "GET /orders/{order_id}" --out <out_dir>``."""
    return runner.invoke(
        _build_app(),
        [
            "observe",
            "fixtures",
            "myshop",
            "--match",
            "GET /orders/{order_id}",
            "--out",
            str(out_dir),
            *extra,
        ],
    )


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
        # observe_commands.observe_app), so it restores the same guarantee
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

    def test_endpoints_json_prints_the_projection(
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
        # Same structlog reset as test_digest_json_flag above: whichever test
        # ran earlier in this worker may have left structlog on its
        # unconfigured default, which prints parse_har_file's INFO event
        # straight onto stdout ahead of the JSON and breaks json.loads.
        from graftpunk.logging import configure_logging

        configure_logging(level="WARNING")
        result = runner.invoke(_build_app(), ["observe", "digest", "myshop", "--endpoints-json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert payload["schema"] == 1
        assert payload["source"] == {"session": "myshop", "run_id": "run-1", "har": None}

    def test_json_and_endpoints_json_together_is_an_error(self, tmp_path: Path) -> None:
        har = tmp_path / "network.har"
        har.write_text(json.dumps({"log": {"version": "1.2", "entries": []}}))
        result = runner.invoke(
            _build_app(),
            ["observe", "digest", "--har", str(har), "--json", "--endpoints-json"],
        )
        assert result.exit_code == 1
        assert "not both" in _plain(result.output)


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

    def test_allow_tracked_writes_onto_a_tracked_directory(
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
                "--allow-tracked",
            ],
        )
        assert result.exit_code == 0, result.output
        assert json.loads(tracked.read_text()) == {"id": 1}

    def test_outside_a_git_work_tree_warns_and_still_writes(
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
        # No git init here: out_dir is a plain tmp_path directory, not part
        # of this worktree's tree (find_repo_root walks from out_dir, not
        # from the test process's cwd, so it never reaches the repo above).
        # It is created up front so git really runs and reports "not a work
        # tree" by its exit status, rather than the command never launching
        # git at all because the directory is missing.
        out_dir = tmp_path / "no_repo_here" / "captures"
        out_dir.mkdir(parents=True)

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
        assert "not inside a git work tree" in result.output.lower()
        written = [
            p for p in out_dir.glob("get_orders_*.json") if not p.name.endswith(".meta.json")
        ]
        assert len(written) == 1
        assert json.loads(written[0].read_text()) == {"id": 1}

    def test_no_matching_entries_writes_nothing(
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
        out_dir = tmp_path / "out"

        app = _build_app()
        result = runner.invoke(
            app,
            [
                "observe",
                "fixtures",
                "myshop",
                "--match",
                "POST /nothing-here",
                "--out",
                str(out_dir),
            ],
        )
        assert result.exit_code == 0, result.output
        assert "no entries matched --match." in result.output.lower()
        assert not out_dir.exists() or not list(out_dir.iterdir())

    def test_the_sidecar_is_committable_and_records_the_capture(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        observe_base = tmp_path / "observe"
        monkeypatch.setattr("graftpunk.cli.observe_commands.OBSERVE_BASE_DIR", observe_base)
        order = _entry("GET", "https://api.myshop.example.com/orders/1?page=2", body='{"id": 1}')
        order["response"]["cookies"] = [{"name": "shop_session", "value": "planted"}]
        _write_run(observe_base, "myshop", "run-1", [order])
        out_dir = tmp_path / "out"
        result = _invoke_fixtures(out_dir)
        assert result.exit_code == 0, result.output
        (fixture,) = [
            p for p in out_dir.glob("get_orders_*.json") if not p.name.endswith(".meta.json")
        ]
        sidecar = json.loads((out_dir / f"{fixture.name}.meta.json").read_text())
        assert set(sidecar) == {
            "schema",
            "status",
            "content_type",
            "body_params",
            "capture_sha256",
            "flagged_names",
        }
        assert sidecar["schema"] == 1
        assert sidecar["capture_sha256"] == hashlib.sha256(fixture.read_bytes()).hexdigest()
        assert sidecar["flagged_names"] == ["shop_session"]
        assert "page=2" not in json.dumps(sidecar)

    def test_a_json_body_key_that_is_not_a_field_name_is_not_written_to_the_sidecar(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A JSON object keyed by data (an email address) is not a field name;
        the digest's own filter (_plausible_field_name) applies here too, so
        the data never reaches the committed sidecar (review round 1,
        2026-09-23)."""
        observe_base = tmp_path / "observe"
        monkeypatch.setattr("graftpunk.cli.observe_commands.OBSERVE_BASE_DIR", observe_base)
        order = _entry("POST", "https://api.myshop.example.com/orders/1", body='{"id": 1}')
        order["request"]["postData"] = {
            "mimeType": "application/json",
            "text": json.dumps({"quantity": 2, "alice@example.com": {"role": "owner"}}),
        }
        _write_run(observe_base, "myshop", "run-1", [order])
        out_dir = tmp_path / "out"
        result = runner.invoke(
            _build_app(),
            [
                "observe",
                "fixtures",
                "myshop",
                "--match",
                "POST /orders/{order_id}",
                "--out",
                str(out_dir),
            ],
        )
        assert result.exit_code == 0, result.output
        (fixture,) = [
            p for p in out_dir.glob("post_orders_*.json") if not p.name.endswith(".meta.json")
        ]
        sidecar = json.loads((out_dir / f"{fixture.name}.meta.json").read_text())
        assert sidecar["body_params"] == ["quantity"]
        assert "alice@example.com" not in json.dumps(sidecar)


class TestFixturesGitignore:
    """The first run inside a repo is the one that matters: the default target
    does not exist yet, and nothing else protects the bodies about to be
    written there."""

    @staticmethod
    def _repo_with_a_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        observe_base = tmp_path / "observe"
        monkeypatch.setattr("graftpunk.cli.observe_commands.OBSERVE_BASE_DIR", observe_base)
        _write_run(
            observe_base,
            "myshop",
            "run-1",
            [_entry("GET", "https://api.myshop.example.com/orders/1", body='{"id": 1}')],
        )
        repo = tmp_path / "repo"
        repo.mkdir()
        _git(["git", "init", "-q"], repo)
        _git(["git", "config", "user.email", "alice@example.com"], repo)
        _git(["git", "config", "user.name", "alice"], repo)
        return repo

    def test_first_run_into_a_directory_that_does_not_exist_yet_adds_the_line(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = self._repo_with_a_run(tmp_path, monkeypatch)
        out_dir = repo / "tests" / "captures"
        assert not out_dir.exists()

        result = _invoke_fixtures(out_dir)

        assert result.exit_code == 0, result.output
        assert "tests/captures/" in (repo / ".gitignore").read_text()
        assert list(out_dir.glob("get_orders_*.json"))

    def test_a_second_run_does_not_duplicate_the_line(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = self._repo_with_a_run(tmp_path, monkeypatch)
        out_dir = repo / "tests" / "captures"

        _invoke_fixtures(out_dir)
        _invoke_fixtures(out_dir)

        assert (repo / ".gitignore").read_text().count("tests/captures/") == 1

    def test_a_tracked_path_refusal_leaves_gitignore_alone(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The ignore line protects files this command writes, so a run that
        refuses to write anything must not leave the edit behind."""
        repo = self._repo_with_a_run(tmp_path, monkeypatch)
        out_dir = repo / "tests" / "captures"
        out_dir.mkdir(parents=True)
        (out_dir / "get_orders_{order_id}.json").write_text("{}")
        _git(["git", "add", "-A"], repo)
        _git(["git", "commit", "-q", "-m", "seed"], repo)

        result = _invoke_fixtures(out_dir)

        assert result.exit_code == 1, result.output
        assert not (repo / ".gitignore").exists()


class TestWroteListingDoesNotWrapMidWord:
    def test_a_long_written_path_appears_whole(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Console.print's default wrapping breaks a path at 80 columns, so the
        listing could not be copied."""
        observe_base = tmp_path / "observe"
        monkeypatch.setattr("graftpunk.cli.observe_commands.OBSERVE_BASE_DIR", observe_base)
        _write_run(
            observe_base,
            "myshop",
            "run-1",
            [_entry("GET", "https://api.myshop.example.com/orders/1", body='{"id": 1}')],
        )
        out_dir = tmp_path / "a-fairly-long-directory-name" / "and-another-one-here" / "captures"

        result = _invoke_fixtures(out_dir)

        assert result.exit_code == 0, result.output
        expected = str(out_dir / "get_orders_{order_id}.json")
        assert len(expected) > 80
        assert expected in _plain(result.output)


class TestLimitMustBePositive:
    """A zero or negative --limit shows nothing and writes nothing, with no
    explanation; Typer refuses it at parse time instead."""

    def test_digest_refuses_a_zero_limit(self, tmp_path: Path) -> None:
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
        result = runner.invoke(
            _build_app(), ["observe", "digest", "--har", str(har_path), "--limit", "0"]
        )
        assert result.exit_code != 0
        assert "--limit" in _plain(result.output)

    def test_fixtures_refuses_a_negative_limit(
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
        out_dir = tmp_path / "out"
        result = _invoke_fixtures(out_dir, "--limit", "-1")
        assert result.exit_code != 0
        assert not out_dir.exists()


class TestMatchPatternValidation:
    @pytest.mark.parametrize(
        "pattern", ["/orders", "ORDERS /orders", "GET", "GET   ", "get /orders/{order_id}"]
    )
    def test_a_pattern_that_is_not_method_plus_template_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pattern: str
    ) -> None:
        """The matcher partitions on a space, so these match nothing at all: the
        user has to be told, not handed 'No entries matched'."""
        observe_base = tmp_path / "observe"
        monkeypatch.setattr("graftpunk.cli.observe_commands.OBSERVE_BASE_DIR", observe_base)
        _write_run(
            observe_base,
            "myshop",
            "run-1",
            [_entry("GET", "https://api.myshop.example.com/orders/1")],
        )
        out_dir = tmp_path / "out"
        result = runner.invoke(
            _build_app(),
            ["observe", "fixtures", "myshop", "--match", pattern, "--out", str(out_dir)],
        )
        assert result.exit_code == 1, result.output
        assert "METHOD template" in result.output
        assert not out_dir.exists()

    def test_a_well_formed_pattern_is_accepted(
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
        result = runner.invoke(
            _build_app(),
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

    def test_the_matcher_takes_its_halves_from_parse_endpoint(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A grammar change made in parse_endpoint alone reaches the matcher: the
        matcher never splits a pattern itself."""
        observe_base = tmp_path / "observe"
        monkeypatch.setattr("graftpunk.cli.observe_commands.OBSERVE_BASE_DIR", observe_base)
        _write_run(
            observe_base,
            "myshop",
            "run-1",
            [_entry("GET", "https://api.myshop.example.com/synthetic/1", body='{"id": 1}')],
        )
        monkeypatch.setattr(
            "graftpunk.cli.observe_commands.parse_endpoint",
            lambda value: ("GET", "/synthetic/*"),
        )
        out_dir = tmp_path / "out"
        result = runner.invoke(
            _build_app(),
            ["observe", "fixtures", "myshop", "--match", "PUT /whatever", "--out", str(out_dir)],
        )
        assert result.exit_code == 0, result.output
        written = [p for p in out_dir.glob("get_synthetic_*") if not p.name.endswith(".meta.json")]
        assert len(written) == 1

    def test_the_refusal_is_parse_endpoints_own_text(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from graftpunk.har.naming import EndpointSpecError, parse_endpoint

        observe_base = tmp_path / "observe"
        monkeypatch.setattr("graftpunk.cli.observe_commands.OBSERVE_BASE_DIR", observe_base)
        _write_run(
            observe_base, "myshop", "run-1", [_entry("GET", "https://api.myshop.example.com/a")]
        )
        with pytest.raises(EndpointSpecError) as caught:
            parse_endpoint("get /a")
        result = runner.invoke(_build_app(), ["observe", "fixtures", "myshop", "--match", "get /a"])
        assert result.exit_code == 1
        assert f"--match: {caught.value}" in " ".join(_plain(result.output).split())


class TestBinaryBodiesAreSkipped:
    def test_an_entry_with_no_text_body_writes_no_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`body or ""` used to put a zero-byte file on disk, which reads back as a
        real but empty fixture."""
        observe_base = tmp_path / "observe"
        monkeypatch.setattr("graftpunk.cli.observe_commands.OBSERVE_BASE_DIR", observe_base)
        binary = _entry("GET", "https://api.myshop.example.com/orders/1/photo")
        binary["response"]["headers"] = [{"name": "Content-Type", "value": "image/png"}]
        binary["response"]["content"] = {"mimeType": "image/png", "size": 2048}
        _write_run(observe_base, "myshop", "run-1", [binary])
        out_dir = tmp_path / "out"

        result = runner.invoke(
            _build_app(),
            [
                "observe",
                "fixtures",
                "myshop",
                "--match",
                "GET /orders/{order_id}/photo",
                "--out",
                str(out_dir),
            ],
        )
        assert result.exit_code == 0, result.output
        assert "image/png" in result.output
        assert list(out_dir.iterdir()) == []


@contextmanager
def _unwritable_dir(parent: Path, name: str = "readonly") -> Iterator[Path]:
    """A directory nothing may write into, restored so tmp_path cleanup works."""
    if os.geteuid() == 0:
        pytest.skip("root ignores mode bits, so the write would succeed")
    directory = parent / name
    directory.mkdir()
    directory.chmod(0o500)
    try:
        yield directory
    finally:
        directory.chmod(0o700)


class TestUnwritableTargetIsARefusal:
    """An OSError on write is a red line and exit 1, never a Rich traceback."""

    def test_digest_output_into_an_unwritable_directory(self, tmp_path: Path) -> None:
        har_path = tmp_path / "network.har"
        entries = [_entry("GET", "https://api.myshop.example.com/orders/1")]
        har_path.write_text(json.dumps({"log": {"version": "1.2", "entries": entries}}))
        with _unwritable_dir(tmp_path) as readonly:
            result = runner.invoke(
                _build_app(),
                [
                    "observe",
                    "digest",
                    "--har",
                    str(har_path),
                    "--output",
                    str(readonly / "digest.md"),
                ],
            )
            assert result.exit_code == 1, result.output
            assert "could not write" in result.output.lower()
            assert "Traceback" not in result.output
            assert not (readonly / "digest.md").exists()

    def test_fixtures_out_into_an_unwritable_directory(
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
        with _unwritable_dir(tmp_path) as readonly:
            result = runner.invoke(
                _build_app(),
                [
                    "observe",
                    "fixtures",
                    "myshop",
                    "--match",
                    "GET /orders/{order_id}",
                    "--out",
                    str(readonly),
                ],
            )
            assert result.exit_code == 1, result.output
            assert "could not write" in result.output.lower()
            assert "Traceback" not in result.output
            assert list(readonly.iterdir()) == []
