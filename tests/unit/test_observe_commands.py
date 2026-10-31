"""resolve_run, gp observe digest, and gp observe fixtures (plugin tooling spec, 2026-09-11)."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
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
from graftpunk.testing.plugin import check_fixtures_tree
from tests.unit.cli_harness import strip_ansi
from tests.unit.guide_harness import REPO_ROOT

runner = CliRunner()

# A cookie value shaped like a session id: flagged_names leaves out a cookie whose
# every value is form state, so a cookie that must be flagged needs one like it.
_SESSION_VALUE = "Zq9XkLmPwR4t7Bv2Hn8Ks3Jd"


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

    @pytest.mark.usefixtures("gp_logging")
    def test_digest_json_flag(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("graftpunk.cli.observe_commands.OBSERVE_BASE_DIR", tmp_path)
        _write_run(
            tmp_path, "myshop", "run-1", [_entry("GET", "https://api.myshop.example.com/orders")]
        )
        app = _build_app()
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

    @pytest.mark.usefixtures("gp_logging")
    def test_endpoints_json_prints_the_projection(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        observe_base = tmp_path / "observe"
        monkeypatch.setattr("graftpunk.cli.observe_commands.OBSERVE_BASE_DIR", observe_base)
        _write_run(
            observe_base,
            "myshop",
            "run-1",
            [_entry("GET", "https://api.myshop.example.com/orders/1001", body='{"id": 1}')],
        )
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
        assert result.stdout == ""
        assert "not both" in strip_ansi(result.stderr)


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
            [_entry("GET", "https://api.myshop.example.com/orders/1001", body='{"id": 1}')],
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
            [_entry("GET", "https://api.myshop.example.com/orders/1001")],
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
            [_entry("GET", "https://api.myshop.example.com/orders/1001", body='{"id": 1}')],
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
            [_entry("GET", "https://api.myshop.example.com/orders/1001", body='{"id": 1}')],
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
            [_entry("GET", "https://api.myshop.example.com/orders/1001")],
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
        assert "no entries matched --match post /nothing-here." in strip_ansi(result.output).lower()
        assert not out_dir.exists() or not list(out_dir.iterdir())

    def test_each_pattern_that_matched_nothing_is_named(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        observe_base = tmp_path / "observe"
        monkeypatch.setattr("graftpunk.cli.observe_commands.OBSERVE_BASE_DIR", observe_base)
        _write_run(
            observe_base,
            "myshop",
            "run-1",
            [_entry("GET", "https://api.myshop.example.com/orders/1001", body='{"id": 1}')],
        )
        out_dir = tmp_path / "out"
        result = _invoke_fixtures(out_dir, "--match", "GET /carts/{cart_id}")
        assert result.exit_code == 0, result.output
        assert "No entries matched --match GET /carts/{cart_id}." in strip_ansi(result.output)
        assert "--match GET /orders/{order_id}." not in strip_ansi(result.output)
        assert list(out_dir.glob("get_orders_*.json"))

    def test_two_templates_that_name_one_file_are_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """/a_b and /a/b both slug to get_a_b; nothing is written."""
        observe_base = tmp_path / "observe"
        monkeypatch.setattr("graftpunk.cli.observe_commands.OBSERVE_BASE_DIR", observe_base)
        entries = [
            _entry("GET", "https://api.myshop.example.com/a_b", body='{"x": 1}'),
            _entry("GET", "https://api.myshop.example.com/a/b", body='{"x": 2}'),
        ]
        _write_run(observe_base, "myshop", "run-1", entries)
        out_dir = tmp_path / "out"
        result = runner.invoke(
            _build_app(),
            ["observe", "fixtures", "myshop", "--match", "GET /*", "--out", str(out_dir)],
        )
        assert result.exit_code == 1, result.output
        output = strip_ansi(result.output).replace("\n", "")
        assert "get_a_b" in output and "GET /a/b" in output and "GET /a_b" in output
        assert not out_dir.exists() or not list(out_dir.glob("*.json"))

    def test_two_templates_that_share_a_stem_across_extensions_are_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The files get_a_b.json and get_a_b.html share the stem FixtureSession
        looks up."""
        observe_base = tmp_path / "observe"
        monkeypatch.setattr("graftpunk.cli.observe_commands.OBSERVE_BASE_DIR", observe_base)
        entries = [
            _entry("GET", "https://api.myshop.example.com/a_b", body='{"x": 1}'),
            _entry(
                "GET",
                "https://api.myshop.example.com/a/b",
                content_type="text/html",
                body="<p>b</p>",
            ),
        ]
        _write_run(observe_base, "myshop", "run-1", entries)
        out_dir = tmp_path / "out"
        result = runner.invoke(
            _build_app(),
            ["observe", "fixtures", "myshop", "--match", "GET /*", "--out", str(out_dir)],
        )
        assert result.exit_code == 1, result.output
        output = strip_ansi(result.output).replace("\n", "")
        assert "get_a_b" in output and "GET /a/b" in output and "GET /a_b" in output
        assert not out_dir.exists() or not any(out_dir.iterdir())

    def test_a_bodyless_redirect_s_stem_collides_too(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A redirect recorded with "text": null gets a fixture, so its stem counts."""
        observe_base = tmp_path / "observe"
        monkeypatch.setattr("graftpunk.cli.observe_commands.OBSERVE_BASE_DIR", observe_base)
        hop = _entry("GET", "https://api.myshop.example.com/a/b", content_type="text/html")
        hop["response"]["status"] = 302
        hop["response"]["content"] = {"mimeType": "text/html", "text": None, "size": 0}
        entries = [_entry("GET", "https://api.myshop.example.com/a_b", body='{"x": 1}'), hop]
        _write_run(observe_base, "myshop", "run-1", entries)
        out_dir = tmp_path / "out"
        result = runner.invoke(
            _build_app(),
            ["observe", "fixtures", "myshop", "--match", "GET /*", "--out", str(out_dir)],
        )
        assert result.exit_code == 1, result.output
        output = strip_ansi(result.output).replace("\n", "")
        assert "get_a_b" in output and "GET /a/b" in output and "GET /a_b" in output

    def test_stems_that_differ_only_in_case_are_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One would overwrite the other on a case-insensitive filesystem."""
        observe_base = tmp_path / "observe"
        monkeypatch.setattr("graftpunk.cli.observe_commands.OBSERVE_BASE_DIR", observe_base)
        entries = [
            _entry("GET", "https://api.myshop.example.com/Users", body='{"x": 1}'),
            _entry("GET", "https://api.myshop.example.com/users", body='{"x": 2}'),
        ]
        _write_run(observe_base, "myshop", "run-1", entries)
        out_dir = tmp_path / "out"
        result = runner.invoke(
            _build_app(),
            ["observe", "fixtures", "myshop", "--match", "GET /*", "--out", str(out_dir)],
        )
        assert result.exit_code == 1, result.output
        output = strip_ansi(result.output).replace("\n", "")
        assert "GET /Users" in output and "GET /users" in output
        # The refusal names a real stem, the first one seen, not its folded key.
        assert "Refusing to write get_Users:" in output

    def test_the_sidecar_is_committable_and_records_the_capture(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        observe_base = tmp_path / "observe"
        monkeypatch.setattr("graftpunk.cli.observe_commands.OBSERVE_BASE_DIR", observe_base)
        order = _entry("GET", "https://api.myshop.example.com/orders/1001?page=2", body='{"id": 1}')
        order["response"]["cookies"] = [{"name": "shop_session", "value": _SESSION_VALUE}]
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
            "flagged_reasons",
            "checked_names",
            "redacted_names",
        }
        assert sidecar["schema"] == 3
        assert sidecar["capture_sha256"] == hashlib.sha256(fixture.read_bytes()).hexdigest()
        assert sidecar["flagged_names"] == ["shop_session"]
        assert sidecar["flagged_reasons"] == {
            "shop_session": ["session_name", "value_not_form_state"]
        }
        # The body, {"id": 1}, does not hold the session's value, but no request
        # sent the cookie: a value seen only in a Set-Cookie leaves it checked.
        assert sidecar["checked_names"] == ["shop_session"]
        assert _SESSION_VALUE not in json.dumps(sidecar)
        assert "page=2" not in json.dumps(sidecar)

    def test_the_sidecar_counts_an_id_cookie_name_and_holds_no_trace_of_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A cookie name that holds an id is counted in redacted_names and
        written in no form (plain, hashed, or as its digits)."""
        observe_base = tmp_path / "observe"
        monkeypatch.setattr("graftpunk.cli.observe_commands.OBSERVE_BASE_DIR", observe_base)
        order = _entry("GET", "https://api.myshop.example.com/orders/1001", body='{"id": 1}')
        order["response"]["cookies"] = [
            {"name": "shop_session", "value": _SESSION_VALUE},
            {"name": "sess_40912873", "value": _SESSION_VALUE},
        ]
        _write_run(observe_base, "myshop", "run-1", [order])
        out_dir = tmp_path / "out"
        result = _invoke_fixtures(out_dir)
        assert result.exit_code == 0, result.output
        (meta,) = out_dir.glob("*.meta.json")
        text = meta.read_text()
        sidecar = json.loads(text)
        assert sidecar["redacted_names"] == 1
        assert sidecar["flagged_names"] == ["shop_session"]
        digest_of_name = hashlib.sha256(b"sess_40912873").hexdigest()
        for trace in ("sess_40912873", "40912873", digest_of_name):
            assert trace not in text
            assert trace not in result.output

    def test_a_collapsed_family_is_matched_and_named_by_the_digests_template(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Twelve slugs under /products/ are one endpoint in the digest,
        GET /products/{product_id}; --match takes that template as the digest
        prints it, and the files carry the name the generated tests look for."""
        observe_base = tmp_path / "observe"
        monkeypatch.setattr("graftpunk.cli.observe_commands.OBSERVE_BASE_DIR", observe_base)
        words = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot"]
        words += ["golf", "hotel", "india", "juliet", "kilo", "lima"]
        entries = [
            _entry(
                "GET",
                f"https://api.myshop.example.com/products/{word}-widget-2024",
                body=json.dumps({"id": word}),
            )
            for word in words
        ]
        _write_run(observe_base, "myshop", "run-1", entries)
        out_dir = tmp_path / "out"
        result = runner.invoke(
            _build_app(),
            [
                "observe",
                "fixtures",
                "myshop",
                "--match",
                "GET /products/{product_id}",
                "--limit",
                "2",
                "--out",
                str(out_dir),
            ],
        )
        assert result.exit_code == 0, result.output
        written = sorted(p.name for p in out_dir.iterdir() if not p.name.endswith(".meta.json"))
        assert written == ["get_products_{product_id}#1.json", "get_products_{product_id}.json"]

    def test_flagged_names_cover_cookies_set_on_a_static_response_and_by_another_host(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The digest leaves static and out-of-scope entries out of its cookie list,
        but a --match glob can still write a capture from either, so the sidecar's
        safety-net list names every cookie the recording set."""
        observe_base = tmp_path / "observe"
        monkeypatch.setattr("graftpunk.cli.observe_commands.OBSERVE_BASE_DIR", observe_base)
        orders = [
            _entry("GET", f"https://api.myshop.example.com/orders/{i}", body='{"id": 1}')
            for i in (1001, 1002)
        ]
        asset = _entry(
            "GET",
            "https://api.myshop.example.com/assets/app.js",
            content_type="application/javascript",
            body="var a = 1;",
        )
        asset["response"]["cookies"] = [{"name": "asset_cookie", "value": _SESSION_VALUE}]
        other_host = _entry("GET", "https://tracker.example.net/orders/1003", body='{"id": 3}')
        other_host["response"]["cookies"] = [{"name": "other_host_cookie", "value": _SESSION_VALUE}]
        _write_run(observe_base, "myshop", "run-1", [*orders, asset, other_host])
        out_dir = tmp_path / "out"
        result = _invoke_fixtures(out_dir, "--limit", "1")
        assert result.exit_code == 0, result.output
        sidecar = json.loads((out_dir / "get_orders_{order_id}.json.meta.json").read_text())
        assert sidecar["flagged_names"] == ["asset_cookie", "other_host_cookie"]

    def test_a_json_body_key_that_is_not_a_field_name_is_not_written_to_the_sidecar(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A JSON object keyed by data (an email address) is not a field name;
        the digest's own filter (_plausible_field_name) applies here too, so
        the data never reaches the committed sidecar."""
        observe_base = tmp_path / "observe"
        monkeypatch.setattr("graftpunk.cli.observe_commands.OBSERVE_BASE_DIR", observe_base)
        order = _entry("POST", "https://api.myshop.example.com/orders/1001", body='{"id": 1}')
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

    def test_an_entry_whose_url_cannot_be_split_is_skipped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The digest drops it as an error; the fixtures loop must not crash on it."""
        observe_base = tmp_path / "observe"
        monkeypatch.setattr("graftpunk.cli.observe_commands.OBSERVE_BASE_DIR", observe_base)
        _write_run(
            observe_base,
            "myshop",
            "run-1",
            [
                _entry("GET", "http://[bad/orders/2", body='{"id": 2}'),
                _entry("GET", "https://api.myshop.example.com/orders/1001", body='{"id": 1}'),
            ],
        )
        out_dir = tmp_path / "out"
        result = _invoke_fixtures(out_dir)
        assert result.exit_code == 0, result.output
        (fixture,) = [
            p for p in out_dir.glob("get_orders_*.json") if not p.name.endswith(".meta.json")
        ]
        assert json.loads(fixture.read_text()) == {"id": 1}


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
            [_entry("GET", "https://api.myshop.example.com/orders/1001", body='{"id": 1}')],
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

    def test_a_gitignore_that_is_not_utf8_is_refused_by_name(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = self._repo_with_a_run(tmp_path, monkeypatch)
        gitignore = repo / ".gitignore"
        gitignore.write_bytes(b"\xff\xfe not utf-8\n")
        out_dir = repo / "tests" / "captures"

        result = _invoke_fixtures(out_dir)

        assert result.exit_code == 1, result.output
        assert isinstance(result.exception, SystemExit)
        output = strip_ansi(result.output)
        assert str(gitignore) in output.replace("\n", "")
        assert "not UTF-8 text" in output
        assert gitignore.read_bytes() == b"\xff\xfe not utf-8\n"
        assert not out_dir.exists()

    def test_a_gitignore_that_is_a_directory_is_refused_as_a_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Its own refusal, not a read error."""
        repo = self._repo_with_a_run(tmp_path, monkeypatch)
        (repo / ".gitignore").mkdir()
        out_dir = repo / "tests" / "captures"
        result = _invoke_fixtures(out_dir)
        assert result.exit_code == 1, result.output
        output = strip_ansi(result.output).replace("\n", "")
        assert f"Refusing to write {repo / '.gitignore'}: it is a directory, not a file" in output
        assert not out_dir.exists()

    @pytest.mark.skipif(os.geteuid() == 0, reason="root reads a 000-mode file")
    def test_an_unreadable_gitignore_is_refused_by_name(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = self._repo_with_a_run(tmp_path, monkeypatch)
        gitignore = repo / ".gitignore"
        gitignore.write_text("node_modules/\n")
        gitignore.chmod(0)
        out_dir = repo / "tests" / "captures"
        try:
            result = _invoke_fixtures(out_dir)
        finally:
            gitignore.chmod(0o644)

        assert result.exit_code == 1, result.output
        assert isinstance(result.exception, SystemExit)
        output = strip_ansi(result.output).replace("\n", "")
        assert f"Could not read {gitignore}: Permission denied" in output
        assert not out_dir.exists()

    @pytest.mark.skipif(os.geteuid() == 0, reason="root writes a read-only file")
    def test_a_read_only_gitignore_is_refused_as_a_failed_write(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = self._repo_with_a_run(tmp_path, monkeypatch)
        gitignore = repo / ".gitignore"
        gitignore.write_text("node_modules/\n")
        gitignore.chmod(0o444)
        out_dir = repo / "tests" / "captures"
        try:
            result = _invoke_fixtures(out_dir)
        finally:
            gitignore.chmod(0o644)

        assert result.exit_code == 1, result.output
        output = strip_ansi(result.output).replace("\n", "")
        assert f"Could not write {gitignore}: Permission denied" in output
        assert gitignore.read_text() == "node_modules/\n"

    def test_a_digest_failure_leaves_gitignore_and_the_target_alone(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The digest is computed before any write, so a failure there edits nothing."""
        repo = self._repo_with_a_run(tmp_path, monkeypatch)
        out_dir = repo / "tests" / "captures"

        def fail(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("digest failed")

        monkeypatch.setattr("graftpunk.cli.observe_commands.digest", fail)
        result = _invoke_fixtures(out_dir)

        assert isinstance(result.exception, RuntimeError)
        assert not (repo / ".gitignore").exists()
        assert not out_dir.exists()


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
            [_entry("GET", "https://api.myshop.example.com/orders/1001", body='{"id": 1}')],
        )
        out_dir = tmp_path / "a-fairly-long-directory-name" / "and-another-one-here" / "captures"

        result = _invoke_fixtures(out_dir)

        assert result.exit_code == 0, result.output
        expected = str(out_dir / "get_orders_{order_id}.json")
        assert len(expected) > 80
        assert expected in strip_ansi(result.output)


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
        assert "--limit" in strip_ansi(result.output)

    def test_fixtures_refuses_a_negative_limit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        observe_base = tmp_path / "observe"
        monkeypatch.setattr("graftpunk.cli.observe_commands.OBSERVE_BASE_DIR", observe_base)
        _write_run(
            observe_base,
            "myshop",
            "run-1",
            [_entry("GET", "https://api.myshop.example.com/orders/1001")],
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
            [_entry("GET", "https://api.myshop.example.com/orders/1001")],
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
            [_entry("GET", "https://api.myshop.example.com/orders/1001", body='{"id": 1}')],
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
        assert f"--match: {caught.value}" in " ".join(strip_ansi(result.output).split())


class TestResponsesRecordedWithNoText:
    def test_an_entry_with_no_text_body_writes_no_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`body or ""` used to put a zero-byte file on disk, which reads back as a
        real but empty fixture."""
        observe_base = tmp_path / "observe"
        monkeypatch.setattr("graftpunk.cli.observe_commands.OBSERVE_BASE_DIR", observe_base)
        binary = _entry("GET", "https://api.myshop.example.com/orders/1001/photo")
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

    def test_the_unsuffixed_fixture_is_the_first_recording_with_a_body(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A 204 then a 200: the generated test reads the unsuffixed file, so it holds
        the 200's body, and the empty 204 takes the suffix."""
        observe_base = tmp_path / "observe"
        monkeypatch.setattr("graftpunk.cli.observe_commands.OBSERVE_BASE_DIR", observe_base)
        empty = _entry("GET", "https://api.myshop.example.com/orders/1001", body="")
        empty["response"]["status"] = 204
        bodied = _entry("GET", "https://api.myshop.example.com/orders/1001", body='{"id": 1}')
        _write_run(observe_base, "myshop", "run-1", [empty, bodied])
        out_dir = tmp_path / "out"

        result = _invoke_fixtures(out_dir)
        assert result.exit_code == 0, result.output
        assert (out_dir / "get_orders_{order_id}.json").read_text() == '{"id": 1}'
        assert (out_dir / "get_orders_{order_id}#1.json").read_text() == ""

    def test_the_unsuffixed_fixture_is_of_the_endpoint_s_main_content_type(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An HTML answer, then two JSON ones: the endpoint is JSON, so the first JSON
        answer takes the unsuffixed name, and the HTML one a suffix."""
        observe_base = tmp_path / "observe"
        monkeypatch.setattr("graftpunk.cli.observe_commands.OBSERVE_BASE_DIR", observe_base)
        url = "https://api.myshop.example.com/orders/1001"
        entries = [
            _entry("GET", url, content_type="text/html", body="<p>sign in</p>"),
            _entry("GET", url, body='{"id": 1}'),
            _entry("GET", url, body='{"id": 2}'),
        ]
        _write_run(observe_base, "myshop", "run-1", entries)
        out_dir = tmp_path / "out"
        result = _invoke_fixtures(out_dir)
        assert result.exit_code == 0, result.output
        assert (out_dir / "get_orders_{order_id}.json").read_text() == '{"id": 1}'
        assert (out_dir / "get_orders_{order_id}#2.html").read_text() == "<p>sign in</p>"

    @pytest.mark.parametrize("status", [302, 204])
    def test_a_bodyless_redirect_or_no_content_writes_an_empty_fixture(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: int
    ) -> None:
        """graftpunk's recorder writes "text": null for a redirect hop; a 3xx or 204
        has no body by definition, so its fixture is empty, with its sidecar. The
        sidecar records no hash, since there is nothing to sanitise, so the
        generated suite's check passes the empty fixture (#236)."""
        observe_base = tmp_path / "observe"
        monkeypatch.setattr("graftpunk.cli.observe_commands.OBSERVE_BASE_DIR", observe_base)
        hop = _entry("GET", "https://api.myshop.example.com/orders/1001", content_type="text/html")
        hop["response"]["status"] = status
        hop["response"]["headers"].append({"name": "Location", "value": "/app"})
        hop["response"]["content"] = {"mimeType": "text/html", "text": None, "size": 0}
        _write_run(observe_base, "myshop", "run-1", [hop])
        out_dir = tmp_path / "out"

        result = _invoke_fixtures(out_dir)
        assert result.exit_code == 0, result.output
        fixture = out_dir / "get_orders_{order_id}.html"
        assert fixture.read_text() == ""
        sidecars = [p for p in out_dir.iterdir() if p != fixture]
        assert len(sidecars) == 1
        sidecar = json.loads(sidecars[0].read_text())
        assert sidecar["status"] == status
        assert sidecar["capture_sha256"] is None
        assert check_fixtures_tree(out_dir).problems == ()


class TestPlainWordCookies:
    """A site that keeps a search form's state in cookies with plain-word names
    (#270): gp's own sidecars and honest fixtures pass the sanitised check, and a
    fixture that still names the session cookie or the CSRF token fails it."""

    def _record(self, observe_base: Path) -> None:
        page = _entry(
            "GET",
            "https://myshop.example.com/search",
            content_type="text/html; charset=utf-8",
            body=(
                '<html><head><meta name="csrf-token" content="Qm7Lp2Xv9Kd4Rt8Wz3Ny6Hb">'
                '</head><body><form><input name="text" value="running shoes">'
                '<input name="lot" value="12"></form></body></html>'
            ),
        )
        page["response"]["cookies"] = [
            {"name": "text", "value": "running%20shoes"},
            {"name": "lot", "value": "12"},
            {"name": "since", "value": "2026-10-01"},
            {"name": "myshop_session", "value": _SESSION_VALUE},
            {"name": "csrftoken", "value": "planted"},
        ]
        results = _entry(
            "GET",
            "https://myshop.example.com/api/search?lot=12",
            body=json.dumps(
                {
                    "query": {"subdivision": "Area One", "block": "4", "lot": "12"},
                    "results": [{"subdivision": "Area One", "block": "4", "lot": "12"}],
                }
            ),
        )
        results["request"]["cookies"] = [
            {"name": "text", "value": "running+shoes"},
            {"name": "lot", "value": "12"},
            {"name": "myshop_session", "value": _SESSION_VALUE},
            {"name": "csrftoken", "value": "planted"},
        ]
        _write_run(observe_base, "myshop", "run-1", [page, results])

    def _fixtures(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        """The recording through gp observe fixtures, with gp's sidecars copied
        unchanged into a fixtures tree."""
        observe_base = tmp_path / "observe"
        monkeypatch.setattr("graftpunk.cli.observe_commands.OBSERVE_BASE_DIR", observe_base)
        self._record(observe_base)
        out_dir = tmp_path / "captures"
        result = runner.invoke(
            _build_app(),
            [
                "observe",
                "fixtures",
                "myshop",
                "--match",
                "GET /search",
                "--match",
                "GET /api/search",
                "--out",
                str(out_dir),
            ],
        )
        assert result.exit_code == 0, result.output
        tree = tmp_path / "fixtures"
        tree.mkdir()
        for meta in out_dir.glob("*.meta.json"):
            (tree / meta.name).write_text(meta.read_text())
        return tree

    def test_the_sidecars_flag_the_session_cookie_and_the_csrf_token_only(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        tree = self._fixtures(tmp_path, monkeypatch)
        for name in ("get_search.html.meta.json", "get_api_search.json.meta.json"):
            sidecar = json.loads((tree / name).read_text())
            assert sidecar["flagged_names"] == ["csrf-token", "csrftoken", "myshop_session"]

    def test_gps_sidecars_and_honest_fixtures_pass_and_a_token_name_still_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The session cookie's value is in neither capture, so its name in a
        fixture is no evidence of a leak and passes (#313); the csrf-token meta
        tag is a token, not a cookie, so its name is looked for in every fixture
        as in 1.32.x."""
        tree = self._fixtures(tmp_path, monkeypatch)
        page_meta = json.loads((tree / "get_search.html.meta.json").read_text())
        api_meta = json.loads((tree / "get_api_search.json.meta.json").read_text())
        assert page_meta["checked_names"] == ["csrf-token"]
        assert api_meta["checked_names"] == ["csrf-token"]
        page = tree / "get_search.html"
        page.write_text(
            "<html><body><form><label>Enter text to search; the context is kept.</label>"
            '<input name="text" value="garden hose"><input name="lot" value="7">'
            "</form></body></html>"
        )
        api = tree / "get_api_search.json"
        row = {"subdivision": "Area Two", "block": "B", "lot": "7"}
        api.write_text(json.dumps({"query": row, "results": [row], "text": "garden hose"}))
        # gp's HTML sidecar says text/html, and the bodies hold the plain words as
        # keys, attribute values and prose.
        assert check_fixtures_tree(tree).problems == ()

        api.write_text(json.dumps({"query": row, "myshop_session": "invented"}))
        assert check_fixtures_tree(tree).problems == ()

        page.write_text("<p>Sign in again to renew your myshop_session cookie.</p>")
        assert check_fixtures_tree(tree).problems == ()

        page.write_text('<meta name="csrf-token" content="invented">')
        (problem,) = check_fixtures_tree(tree).problems
        assert "'csrf-token'" in problem
        assert "whose recorded value the capture held" in problem
        # The csrf-token name in the API fixture, whose capture held no token
        # value: a token name is looked for wherever it is.
        page.write_text("<p>Searching.</p>")
        api.write_text(json.dumps({"query": row, "csrf-token": "invented"}))
        (problem,) = check_fixtures_tree(tree).problems
        assert "'csrf-token'" in problem

    @staticmethod
    def _django_style_sidecar(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, form_state_http_only: bool
    ) -> Path:
        """sessionid set HttpOnly and csrftoken set without it at login, word-shaped
        both; the search response sets the form state (text, lot) by Set-Cookie,
        and the next request sends all four back. Returns gp's sidecar for the
        search capture."""
        observe_base = tmp_path / "observe"
        monkeypatch.setattr("graftpunk.cli.observe_commands.OBSERVE_BASE_DIR", observe_base)
        login = _entry("POST", "https://myshop.example.com/accounts/login/", body="{}")
        login["response"]["cookies"] = [
            {"name": "sessionid", "value": "lanterns", "httpOnly": True, "secure": True},
            {"name": "csrftoken", "value": "harbor", "secure": True},
        ]
        search = _entry(
            "GET",
            "https://myshop.example.com/api/search?lot=12",
            body=json.dumps({"results": [{"subdivision": "Area One", "block": "4", "lot": "12"}]}),
        )
        form_flags = {"secure": True, "httpOnly": True} if form_state_http_only else {}
        search["response"]["cookies"] = [
            {"name": "text", "value": "running%20shoes", **form_flags},
            {"name": "lot", "value": "12", **form_flags},
        ]
        again = _entry("GET", "https://myshop.example.com/api/search?lot=12")
        again["request"]["cookies"] = [
            {"name": "sessionid", "value": "lanterns"},
            {"name": "csrftoken", "value": "harbor"},
            {"name": "text", "value": "running%20shoes"},
            {"name": "lot", "value": "12"},
        ]
        _write_run(observe_base, "myshop", "run-1", [login, search, again])
        out_dir = tmp_path / "captures"
        result = runner.invoke(
            _build_app(),
            [
                "observe",
                "fixtures",
                "myshop",
                "--match",
                "GET /api/search",
                "--limit",
                "1",
                "--out",
                str(out_dir),
            ],
        )
        assert result.exit_code == 0, result.output
        return out_dir / "get_api_search.json.meta.json"

    def test_the_httponly_guard_keeps_form_state_a_server_sets_httponly_flagged(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        meta = self._django_style_sidecar(tmp_path, monkeypatch, form_state_http_only=True)
        assert json.loads(meta.read_text())["flagged_names"] == [
            "csrftoken",
            "lot",
            "sessionid",
            "text",
        ]

    def test_a_django_style_session_stays_flagged_beside_server_set_form_state(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The session and the token stay flagged with word-shaped values; the form
        state a response set without HttpOnly is left out, and the fixture keeping
        its names passes. The search body holds neither the session's value nor the
        token's, so naming the session cookie in the fixture passes too (#313)."""
        meta = self._django_style_sidecar(tmp_path, monkeypatch, form_state_http_only=False)
        sidecar = json.loads(meta.read_text())
        assert sidecar["flagged_names"] == ["csrftoken", "sessionid"]
        assert sidecar["checked_names"] == []
        tree = tmp_path / "fixtures"
        tree.mkdir()
        (tree / meta.name).write_text(meta.read_text())
        fixture = tree / "get_api_search.json"
        fixture.write_text(json.dumps({"results": [{"block": "B", "lot": "7"}], "text": "x"}))
        assert check_fixtures_tree(tree).problems == ()
        fixture.write_text("See the `sessionid` cookie.")
        assert check_fixtures_tree(tree).problems == ()

    def test_httponly_read_from_set_cookie_headers_when_the_cookies_array_omits_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A recording whose response.cookies never says httpOnly while some
        Set-Cookie headers do: sessionid set HttpOnly and Secure, csrftoken set
        both ways, a word-valued remember cookie set HttpOnly in one header of
        two, and eleven form-state cookies set by the server, never HttpOnly,
        some Secure with SameSite=Lax. The form state is left out; the cookies a
        header set HttpOnly stay flagged."""
        observe_base = tmp_path / "observe"
        monkeypatch.setattr("graftpunk.cli.observe_commands.OBSERVE_BASE_DIR", observe_base)
        form = {
            "text": "running%20shoes",
            "lot": "12",
            "block": "4",
            "subdivision": "Area%20One",
            "since": "2026-10-01",
            "until": "2026-10-19",
            "region": "north",
            "sort": "price",
            "order": "asc",
            "page": "2",
            "view": "list",
        }

        def setting(url: str, lines: list[str]) -> dict:
            entry = _entry("GET", url, body=json.dumps({"results": []}))
            entry["response"]["headers"] += [
                {"name": "Set-Cookie", "value": line} for line in lines
            ]
            entry["response"]["cookies"] = [
                {"name": line.split("=", 1)[0], "value": line.split(";")[0].split("=", 1)[1]}
                for line in lines
            ]
            return entry

        login = setting(
            "https://myshop.example.com/accounts/login/",
            [
                "sessionid=lanterns; Path=/; Secure; HttpOnly; SameSite=Lax",
                "csrftoken=harbor; Path=/; Secure; HttpOnly",
                "visitor=lanterns; Path=/",
            ],
        )
        search = setting(
            "https://myshop.example.com/api/search",
            [
                *(
                    f"{name}={value}; Path=/" + ("; Secure; SameSite=Lax" if i % 2 else "")
                    for i, (name, value) in enumerate(form.items())
                ),
                "csrftoken=harbor; Path=/",
                "visitor=lanterns; Path=/; HttpOnly",
            ],
        )
        _write_run(observe_base, "myshop", "run-1", [login, search])
        out_dir = tmp_path / "captures"
        result = runner.invoke(
            _build_app(),
            ["observe", "fixtures", "myshop", "--match", "GET /api/search", "--out", str(out_dir)],
        )
        assert result.exit_code == 0, result.output
        sidecar = json.loads((out_dir / "get_api_search.json.meta.json").read_text())
        assert sidecar["flagged_names"] == ["csrftoken", "sessionid", "visitor"]


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


_OPAQUE_ID = "Xk3Lp9Qv2Rt7Wm4Zb8Nc6Hd"


class TestFlaggedNamesAreJudgedByTheirValues:
    """#313: a recording made with a cached session sends `id=<opaque>`, so `id` is
    flagged. The fixtures check looks for the name only in a fixture whose capture
    held the value."""

    @staticmethod
    def _sidecar(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: object
    ) -> tuple[Path, dict]:
        """gp's sidecar for one `GET /orders/1001` capture of *body*, the request
        sending `id` and `myshop_session` as a cached session does (no response sets
        them), copied into a fixtures tree. Returns the tree and the sidecar."""
        observe_base = tmp_path / "observe"
        monkeypatch.setattr("graftpunk.cli.observe_commands.OBSERVE_BASE_DIR", observe_base)
        order = _entry("GET", "https://api.myshop.example.com/orders/1001", body=json.dumps(body))
        order["request"]["cookies"] = [
            {"name": "id", "value": _OPAQUE_ID},
            {"name": "myshop_session", "value": _SESSION_VALUE},
        ]
        _write_run(observe_base, "myshop", "run-1", [order])
        out_dir = tmp_path / "captures"
        result = _invoke_fixtures(out_dir)
        assert result.exit_code == 0, result.output
        (meta,) = out_dir.glob("*.meta.json")
        tree = tmp_path / "fixtures"
        tree.mkdir()
        (tree / meta.name).write_text(meta.read_text())
        return tree, json.loads(meta.read_text())

    def test_an_honest_id_key_passes_when_the_recording_sent_an_id_cookie(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        tree, sidecar = self._sidecar(tmp_path, monkeypatch, {"id": 4411, "total": "12.50"})
        assert sidecar["flagged_names"] == ["id", "myshop_session"]
        assert sidecar["checked_names"] == []
        (tree / "get_orders_{order_id}.json").write_text(json.dumps({"id": 7, "total": "1.00"}))
        assert check_fixtures_tree(tree).problems == ()

    def test_the_same_fixture_under_the_schema_2_sidecar_still_fails_with_the_way_out(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A sidecar committed by 1.32.x keeps today's behaviour: every flagged
        name is looked for, and the problem says how to regenerate it."""
        tree, sidecar = self._sidecar(tmp_path, monkeypatch, {"id": 4411, "total": "12.50"})
        del sidecar["checked_names"]
        sidecar["schema"] = 2
        (meta,) = tree.glob("*.meta.json")
        meta.write_text(json.dumps(sidecar))
        (tree / "get_orders_{order_id}.json").write_text(json.dumps({"id": 7, "total": "1.00"}))
        (problem,) = check_fixtures_tree(tree).problems
        assert "contains the flagged name 'id'" in problem
        assert "regenerate the sidecar with gp observe fixtures (sidecar schema 3)" in problem

    @pytest.mark.parametrize(
        "body",
        [
            {"session": {"id": _OPAQUE_ID}},
            {"next": f"/account?id={_OPAQUE_ID}&tab=2"},
            {"next": f"/account?ref=%2Fx%3Fid%3D{_OPAQUE_ID}"},
            {"html": f"<a href='/x?a=1&amp;v={_OPAQUE_ID}'>"},
        ],
        ids=["json-string", "url", "url-encoded", "html-escaped"],
    )
    def test_a_capture_holding_the_value_keeps_the_name_looked_for(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: dict
    ) -> None:
        tree, sidecar = self._sidecar(tmp_path, monkeypatch, body)
        assert sidecar["checked_names"] == ["id"]
        (tree / "get_orders_{order_id}.json").write_text(json.dumps({"id": 7}))
        (problem,) = check_fixtures_tree(tree).problems
        assert "contains the flagged name 'id'" in problem
        assert "whose recorded value the capture held" in problem

    def test_a_value_no_recorded_header_carried_keeps_the_name_checked(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The capture hands the page a value under the cookie's name that no
        recorded Cookie or Set-Cookie header carried (a token rotated after the
        last request, or set by a script): the recording cannot know it, and the
        body holds an opaque value under the name, so the name stays checked and
        a fixture keeping that value fails as under a schema 2 sidecar."""
        rotated = "Rk9mT2vX8Lq4Pz7Nw3Yb6Hc1Jd5Ks0Gf"
        tree, sidecar = self._sidecar(tmp_path, monkeypatch, {"order": 1, "id": rotated})
        assert sidecar["checked_names"] == ["id"]
        (tree / "get_orders_{order_id}.json").write_text(json.dumps({"order": 2, "id": rotated}))
        (problem,) = check_fixtures_tree(tree).problems
        assert "contains the flagged name 'id'" in problem
        assert "other than in a name-value pair holding nothing" in problem

    @pytest.mark.parametrize(
        "body",
        [
            "<script>Cookies.set('id', 'Rk9mT2vX8Lq4Pz7Nw3Yb6Hc1Jd5Ks0Gf');</script>",
            {"cookies": [{"name": "id", "value": "Rk9mT2vX8Lq4Pz7Nw3Yb6Hc1Jd5Ks0Gf"}], "n": 1},
            "<r><id>Rk9mT2vX8Lq4Pz7Nw3Yb6Hc1Jd5Ks0Gf</id><n>1</n></r>",
            '<textarea name="id">Rk9mT2vX8Lq4Pz7Nw3Yb6Hc1Jd5Ks0Gf</textarea>',
            "<script>window.cfg = {id: {value: 'Rk9mT2vX8Lq4Pz7Nw3Yb6Hc1Jd5Ks0Gf'}};</script>",
            "<tr><td>id</td><td>Rk9mT2vX8Lq4Pz7Nw3Yb6Hc1Jd5Ks0Gf</td></tr>",
            {"next": "/orders?id=918273645546372819&page=2"},
            {"id": {"Rk9mT2vX8Lq4Pz7Nw3Yb6Hc1Jd5Ks0Gf": True}, "n": 1},
            {"id": 827364918273649182736491},
        ],
        ids=[
            "js-call",
            "name-value-pair",
            "element-body",
            "textarea",
            "nested-js-literal",
            "table-cell",
            "query-digits",
            "opaque-key",
            "long-number",
        ],
    )
    def test_a_value_beside_the_name_in_a_shape_not_read_keeps_the_name_checked(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: object
    ) -> None:
        """A token rotated or set by a script, beside the cookie's name in a shape
        the reader cannot pair with a value or with a value that is not harmless:
        the name stays checked and the fixture keeping it fails, as under a
        schema 2 sidecar."""
        tree, sidecar = self._sidecar(tmp_path, monkeypatch, body)
        assert sidecar["checked_names"] == ["id"]
        (tree / "get_orders_{order_id}.json").write_text(json.dumps([body, "edited"]))
        (problem,) = check_fixtures_tree(tree).problems
        assert "contains the flagged name 'id'" in problem

    def test_a_cookie_sent_empty_and_then_set_stays_checked(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Its one value came from a Set-Cookie, so what the browser held later is
        unknown, and the empty send does not make it known."""
        observe_base = tmp_path / "observe"
        monkeypatch.setattr("graftpunk.cli.observe_commands.OBSERVE_BASE_DIR", observe_base)
        home = _entry("GET", "https://api.myshop.example.com/", body="{}")
        home["request"]["cookies"] = [{"name": "myshop_session", "value": ""}]
        home["response"]["cookies"] = [{"name": "myshop_session", "value": _SESSION_VALUE}]
        home["response"]["headers"].append(
            {"name": "Set-Cookie", "value": f"myshop_session={_SESSION_VALUE}; Path=/"}
        )
        order = _entry("GET", "https://api.myshop.example.com/orders/1001", body='{"n": 1}')
        _write_run(observe_base, "myshop", "run-1", [home, order])
        out_dir = tmp_path / "captures"
        assert _invoke_fixtures(out_dir).exit_code == 0
        (meta,) = out_dir.glob("*.meta.json")
        assert json.loads(meta.read_text())["checked_names"] == ["myshop_session"]

    @pytest.mark.parametrize(
        "body",
        [{"id": 4411, "total": "12.50"}, {"id": "alice"}, {"id": True, "myshop_session": None}],
        ids=["number", "word", "boolean-and-null"],
    )
    def test_numbers_booleans_and_form_state_under_the_name_leave_it_unchecked(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: dict
    ) -> None:
        _tree, sidecar = self._sidecar(tmp_path, monkeypatch, body)
        assert sidecar["checked_names"] == []

    def test_the_sidecar_holds_no_recorded_value(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _tree, sidecar = self._sidecar(tmp_path, monkeypatch, {"session": {"id": _OPAQUE_ID}})
        text = json.dumps(sidecar)
        assert _OPAQUE_ID not in text
        assert _SESSION_VALUE not in text
        assert hashlib.sha256(_OPAQUE_ID.encode()).hexdigest() not in text

    def test_a_cookie_whose_value_was_not_kept_stays_looked_for(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """gp cannot tell whether a value it never saw is in the body, so the name
        is looked for, as before."""
        observe_base = tmp_path / "observe"
        monkeypatch.setattr("graftpunk.cli.observe_commands.OBSERVE_BASE_DIR", observe_base)
        order = _entry("GET", "https://api.myshop.example.com/orders/1001", body='{"id": 1}')
        order["request"]["cookies"] = [{"name": "myshop_session"}]
        _write_run(observe_base, "myshop", "run-1", [order])
        out_dir = tmp_path / "captures"
        assert _invoke_fixtures(out_dir).exit_code == 0
        (meta,) = out_dir.glob("*.meta.json")
        assert json.loads(meta.read_text())["checked_names"] == ["myshop_session"]

    @staticmethod
    def _token_run(run_dir: Path, source: str, old: str, new: str) -> tuple[str, list[dict]]:
        """The flagged token name and the HAR entries of a run where the token's
        value *new* is held only by *source*, a place the recording keeps it outside
        any page the HAR's HTML responses show; files go into *run_dir*."""
        page = _entry(
            "GET",
            "https://myshop.example.com/account",
            content_type="text/html",
            body=f'<html><head><meta name="csrf-token" content="{old}"></head></html>',
        )
        if source == "page-source":
            (run_dir / "page-source.html").write_text(f'<meta name="csrf-token" content="{new}">')
            return "csrf-token", [page]
        if source == "dom-snapshot":
            (run_dir / "snapshots").mkdir()
            (run_dir / "snapshots" / "1.html").write_text(
                f'<meta name="csrf-token" content="{new}">'
            )
            post = _entry("POST", "https://myshop.example.com/account")
            post["request"]["_domSnapshot"] = "snapshots/1.html"
            post["request"]["_domSnapshotUrl"] = "https://myshop.example.com/account"
            return "csrf-token", [page, post]
        if source == "request-body":
            # One page shows the input empty, and the form posts a value no page shows.
            hidden = '<form><input type="hidden" name="csrfmiddlewaretoken" value="{}"></form>'
            login = _entry(
                "GET",
                "https://myshop.example.com/login",
                content_type="text/html",
                body=hidden.format(old),
            )
            account = _entry(
                "GET",
                "https://myshop.example.com/account",
                content_type="text/html",
                body=hidden.format(""),
            )
            post = _entry("POST", "https://myshop.example.com/account")
            post["request"]["postData"] = {
                "mimeType": "application/x-www-form-urlencoded",
                "text": f"csrfmiddlewaretoken={new}&n=2",
            }
            return "csrfmiddlewaretoken", [login, account, post]
        api = _entry("POST", "https://myshop.example.com/api/cart")
        api["request"]["headers"] = [{"name": "X-CSRF-Token", "value": old}]
        api["response"]["headers"].append({"name": "X-CSRF-Token", "value": new})
        return "X-CSRF-Token", [api]

    @pytest.mark.parametrize(
        "source", ["page-source", "dom-snapshot", "request-body", "response-header"]
    )
    def test_a_token_name_stays_checked_wherever_its_value_came_from(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str
    ) -> None:
        """A token's value can come from a place a reader of the recording does not
        read, so a token name is looked for in every fixture, as in 1.32.x, even
        when the capture held none of its values: a fixture keeping a value only
        that place held under the name fails the suite, and ``fixture-leaks.py
        --har`` reports it too."""
        old, new = "OldTok9Qv2Rt7Wm4Zb8Nc6", "NewTok3Lp9Qv2Rt7Wm4Zb8"
        observe_base = tmp_path / "observe"
        monkeypatch.setattr("graftpunk.cli.observe_commands.OBSERVE_BASE_DIR", observe_base)
        run_dir = observe_base / "myshop" / "run-1"
        run_dir.mkdir(parents=True)
        name, entries = self._token_run(run_dir, source, old, new)
        order = _entry("GET", "https://myshop.example.com/orders/1001", body='{"n": 1}')
        har = run_dir / "network.har"
        har.write_text(json.dumps({"log": {"version": "1.2", "entries": [*entries, order]}}))
        out_dir = tmp_path / "captures"
        result = _invoke_fixtures(out_dir)
        assert result.exit_code == 0, result.output
        (meta,) = out_dir.glob("*.meta.json")
        sidecar = json.loads(meta.read_text())
        assert name in sidecar["flagged_names"]
        assert sidecar["checked_names"] == [name]
        tree = tmp_path / "fixtures"
        tree.mkdir()
        (tree / meta.name).write_text(meta.read_text())
        fixture = tree / meta.name.removesuffix(".meta.json")
        fixture.write_text(json.dumps({name: new, "n": 2}))
        (problem,) = check_fixtures_tree(tree).problems
        assert f"contains the flagged name {name!r}" in problem
        capture = out_dir / fixture.name
        script = REPO_ROOT / "skills" / "graft" / "scripts" / "fixture-leaks.py"
        found = subprocess.run(  # noqa: S603 - argv is fixed, not untrusted input
            [sys.executable, "-I", str(script), str(capture), str(fixture), "--har", str(har)],
            capture_output=True,
            text=True,
        )
        assert f"the recorded value of flagged {name!r} is still in the fixture (line 1)" in (
            found.stdout
        )
        assert new not in found.stdout

    def test_a_quoted_cookie_value_the_body_holds_bare_keeps_the_name_checked(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _out, sidecar = self._sidecar_of(
            tmp_path, monkeypatch, f"token {_OPAQUE_ID} end", {"id": f'"{_OPAQUE_ID}"'}
        )
        assert sidecar["checked_names"] == ["id"]

    def test_a_flagged_value_pasted_in_under_its_name_passes_the_suite_and_fails_har(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """What CI stops catching with schema 3: the capture never held the session
        value, so the suite does not look for `myshop_session`, and a fixture that
        pasted the value in under that name passes it. ``fixture-leaks.py --har``
        on the developer's machine is what catches it (CHANGELOG 1.33.0)."""
        tree, sidecar = self._sidecar(tmp_path, monkeypatch, {"id": 4411})
        assert sidecar["checked_names"] == []
        fixture = tree / "get_orders_{order_id}.json"
        fixture.write_text(json.dumps({"id": 7, "myshop_session": _SESSION_VALUE}))
        assert check_fixtures_tree(tree).problems == ()
        capture = tmp_path / "captures" / fixture.name
        har = tmp_path / "observe" / "myshop" / "run-1" / "network.har"
        script = REPO_ROOT / "skills" / "graft" / "scripts" / "fixture-leaks.py"
        result = subprocess.run(  # noqa: S603 - argv is fixed, not untrusted input
            [sys.executable, "-I", str(script), str(capture), str(fixture), "--har", str(har)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 1
        assert (
            "the recorded value of flagged 'myshop_session' is still in the fixture (line 1)"
            in result.stdout
        )
        assert _SESSION_VALUE not in result.stdout

    @staticmethod
    def _sidecar_of(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str, cookies: dict[str, str]
    ) -> tuple[Path, dict]:
        """gp's sidecar for one `GET /orders/1001` text capture of *body*, the
        request sending *cookies*."""
        observe_base = tmp_path / "observe"
        monkeypatch.setattr("graftpunk.cli.observe_commands.OBSERVE_BASE_DIR", observe_base)
        order = _entry(
            "GET",
            "https://api.myshop.example.com/orders/1001",
            content_type="text/plain",
            body=body,
        )
        order["request"]["cookies"] = [{"name": k, "value": v} for k, v in cookies.items()]
        _write_run(observe_base, "myshop", "run-1", [order])
        out_dir = tmp_path / "captures"
        assert _invoke_fixtures(out_dir).exit_code == 0
        (meta,) = out_dir.glob("*.meta.json")
        return out_dir, json.loads(meta.read_text())


class TestUnwritableTargetIsARefusal:
    """An OSError on write is a red line and exit 1, never a Rich traceback."""

    def test_digest_output_into_an_unwritable_directory(self, tmp_path: Path) -> None:
        har_path = tmp_path / "network.har"
        entries = [_entry("GET", "https://api.myshop.example.com/orders/1001")]
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
            [_entry("GET", "https://api.myshop.example.com/orders/1001", body='{"id": 1}')],
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


def test_the_all_hosts_help_says_the_default_keeps_the_primary_hosts_domain() -> None:
    """The default digest keeps every host under the primary host's domain
    (api.myshop.example beside myshop.example), not the primary host alone."""
    digest_command = typer.main.get_command(observe_app).commands["digest"]
    (option,) = [p for p in digest_command.params if "--all-hosts" in p.opts]
    assert option.help == "Model every host, not just the primary host's domain"
