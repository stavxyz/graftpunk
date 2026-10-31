"""The writing side of 'captures never enter git' (plugin tooling spec, 2026-09-11).

captures.py applies the rule to disk; the pure rule, the directory and the
.gitignore text edit, is owned by captures_rule.py and tested at the end of
this module."""

from __future__ import annotations

import ast
import hashlib
import json
import subprocess
from pathlib import Path

import pytest

import graftpunk.devtools.captures_rule as captures_rule
from graftpunk.devtools.captures import (
    checked_names_of,
    ensure_ignored,
    find_repo_root,
    is_tracked,
    write_sidecar,
)
from graftpunk.devtools.captures_rule import CAPTURES_DIR, with_ignored
from graftpunk.testing.sidecar import load_sidecar, sidecar_path


def _git(argv: list[str], cwd: Path) -> None:
    # argv is always a fixed git invocation built by this test module, never
    # untrusted input; a runtime list argument (rather than a literal) also
    # keeps ruff's S607 (partial executable path) from flagging every call
    # site below, matching graftpunk.devtools.captures's own convention.
    subprocess.run(argv, cwd=cwd, check=True)  # noqa: S603


def _init_repo(root: Path) -> None:
    _git(["git", "init", "-q"], root)
    _git(["git", "config", "user.email", "alice@example.com"], root)
    _git(["git", "config", "user.name", "alice"], root)


class TestCapturesDir:
    def test_is_tests_captures(self) -> None:
        assert CAPTURES_DIR == "tests/captures"


class TestFindRepoRoot:
    def test_finds_the_root_of_a_git_work_tree(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        nested = tmp_path / "a" / "b"
        nested.mkdir(parents=True)
        assert find_repo_root(nested) == tmp_path.resolve()

    def test_none_outside_a_work_tree(self, tmp_path: Path) -> None:
        assert find_repo_root(tmp_path) is None

    def test_a_path_that_does_not_exist_yet_still_finds_the_root(self, tmp_path: Path) -> None:
        """The fixtures command's default target does not exist on a first run:
        the search starts from the nearest existing ancestor rather than failing
        to launch git at all."""
        _init_repo(tmp_path)
        not_yet_created = tmp_path / "tests" / "captures"
        assert not not_yet_created.exists()
        assert find_repo_root(not_yet_created) == tmp_path.resolve()

    def test_a_path_that_does_not_exist_yet_outside_a_work_tree_is_none(
        self, tmp_path: Path
    ) -> None:
        assert find_repo_root(tmp_path / "tests" / "captures") is None


class TestEnsureIgnored:
    def test_adds_the_line_when_absent(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        added = ensure_ignored(tmp_path, CAPTURES_DIR)
        assert added is True
        assert "tests/captures/" in (tmp_path / ".gitignore").read_text()

    def test_idempotent_second_call_reports_no_change(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        ensure_ignored(tmp_path, CAPTURES_DIR)
        added_again = ensure_ignored(tmp_path, CAPTURES_DIR)
        assert added_again is False
        assert (tmp_path / ".gitignore").read_text().count("tests/captures/") == 1

    def test_creates_gitignore_when_missing(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        ensure_ignored(tmp_path, CAPTURES_DIR)
        assert (tmp_path / ".gitignore").exists()


class TestIsTracked:
    def test_tracked_file_is_true(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        tracked = tmp_path / "committed.txt"
        tracked.write_text("hi")
        _git(["git", "add", "committed.txt"], tmp_path)
        _git(["git", "commit", "-q", "-m", "add"], tmp_path)
        assert is_tracked(tracked) is True

    def test_untracked_file_is_false(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        untracked = tmp_path / "scratch.txt"
        untracked.write_text("hi")
        assert is_tracked(untracked) is False


class TestWriteSidecar:
    def test_writes_the_schema_keys_and_the_hash_of_the_file_on_disk(self, tmp_path: Path) -> None:
        fixture = tmp_path / "get_orders.json"
        fixture.write_text('{"orders": []}', encoding="utf-8")
        path = write_sidecar(
            fixture,
            status=200,
            content_type="application/json",
            body_params=["page", "archived"],
            flagged_reasons={
                "shop_session": ["session_name", "http_only", "session_name"],
                "csrf-token": ["token_name"],
            },
        )
        assert path == sidecar_path(fixture)
        sidecar = load_sidecar(path)
        assert sidecar.capture_sha256 == hashlib.sha256(fixture.read_bytes()).hexdigest()
        assert sidecar.body_params == ("archived", "page")
        assert sidecar.flagged_names == ("csrf-token", "shop_session")
        assert sidecar.flagged_reasons == {
            "csrf-token": ("token_name",),
            "shop_session": ("http_only", "session_name"),
        }
        assert "url" not in json.loads(path.read_text())
        assert "captured_at" not in json.loads(path.read_text())

    def test_an_empty_capture_records_no_hash(self, tmp_path: Path) -> None:
        """An empty body has nothing to sanitise, and its fixture is empty too, so a
        hash would fail the suite's unchanged-copy check on a fixture that holds no
        captured value (#236)."""
        fixture = tmp_path / "get_orders.html"
        fixture.write_bytes(b"")
        path = write_sidecar(
            fixture, status=204, content_type="text/html", body_params=[], flagged_reasons={}
        )
        assert load_sidecar(path).capture_sha256 is None
        assert json.loads(path.read_text())["capture_sha256"] is None


_VALUE = "Rk9mT2vX8Lq4Pz7Nw3Yb6Hc"


class TestCheckedNamesOf:
    @pytest.mark.parametrize(
        "body",
        [
            f'{{"token": "{_VALUE}"}}',
            f"/next?csrftoken={_VALUE}&x=1",
            f"/next?ref=%2Fa%3Fcsrftoken%3D{_VALUE}",
            f"&lt;i&gt;{_VALUE}",
            '{"token": "\\u0052' + _VALUE[1:] + '"}',
        ],
        ids=["json", "query", "percent-encoded", "html-escaped", "json-escaped"],
    )
    def test_a_value_the_body_holds_in_any_form_is_checked(self, body: str) -> None:
        assert checked_names_of({"csrftoken": frozenset({_VALUE})}, body) == ("csrftoken",)

    def test_a_value_recorded_percent_encoded_is_found_decoded(self) -> None:
        values = {"csrftoken": frozenset({"ab%2Bcd%3D%3D"})}
        assert checked_names_of(values, '{"t": "ab+cd=="}') == ("csrftoken",)

    def test_a_name_whose_values_the_body_does_not_hold_is_not_checked(self) -> None:
        values = {"id": frozenset({_VALUE}), "csrftoken": frozenset({"other-value"})}
        assert checked_names_of(values, '{"id": 7, "csrftoken": "invented"}') == ()

    def test_a_value_is_matched_case_sensitively(self) -> None:
        assert checked_names_of({"id": frozenset({_VALUE})}, _VALUE.lower()) == ()

    def test_values_not_known_keep_the_name_checked(self) -> None:
        assert checked_names_of({"b": None, "a": frozenset()}, "{}") == ("b",)

    def test_a_plus_in_a_recorded_value_is_also_read_as_a_space(self) -> None:
        """A form-encoded cookie value spells a space as +; the body holds the
        space."""
        values = {"csrftoken": frozenset({"Xk3+Lp9Qv2"})}
        assert checked_names_of(values, '{"t": "Xk3 Lp9Qv2"}') == ("csrftoken",)

    @pytest.mark.parametrize("quoted", [f'"{_VALUE}"', f"%22{_VALUE}%22"])
    def test_a_quoted_cookie_value_is_found_without_its_quotes(self, quoted: str) -> None:
        """RFC 6265 lets a server quote a cookie value, and the browser sends the
        quotes; a body holds the value inside a text node without them."""
        body = f"<p>token {_VALUE} end</p>"
        assert checked_names_of({"id": frozenset({quoted})}, body) == ("id",)

    def test_a_value_json_escaped_inside_an_html_attribute_is_found(self) -> None:
        """A value holding & and / in JSON state inside a data attribute is both
        HTML-escaped and JSON-escaped; the two decodings are combined."""
        body = '<div data-x="{&quot;t&quot;:&quot;a&amp;b\\/cdefgh&quot;}">'
        assert checked_names_of({"id": frozenset({"a&b/cdefgh"})}, body) == ("id",)


class TestWriteSidecarCheckedNames:
    def test_without_values_every_flagged_name_is_checked(self, tmp_path: Path) -> None:
        fixture = tmp_path / "get_orders.json"
        fixture.write_text('{"id": 7}', encoding="utf-8")
        path = write_sidecar(
            fixture,
            status=200,
            content_type="application/json",
            body_params=[],
            flagged_reasons={"id": ["value_not_form_state"], "sid": ["session_name"]},
        )
        assert load_sidecar(path).checked_names == ("id", "sid")

    def test_with_values_only_the_names_whose_value_the_file_holds(self, tmp_path: Path) -> None:
        fixture = tmp_path / "get_orders.json"
        fixture.write_text(json.dumps({"id": 7, "token": _VALUE}), encoding="utf-8")
        path = write_sidecar(
            fixture,
            status=200,
            content_type="application/json",
            body_params=[],
            flagged_reasons={"id": ["value_not_form_state"], "sid": ["session_name"]},
            flagged_values={
                "id": frozenset({"Zq9XkLmPwR4t7Bv2Hn8Ks3Jd"}),
                "sid": frozenset({_VALUE}),
            },
        )
        assert load_sidecar(path).checked_names == ("sid",)
        assert _VALUE not in path.read_text()

    def test_a_flagged_name_missing_from_the_values_is_checked(self, tmp_path: Path) -> None:
        fixture = tmp_path / "get_orders.json"
        fixture.write_text("{}", encoding="utf-8")
        path = write_sidecar(
            fixture,
            status=200,
            content_type="application/json",
            body_params=[],
            flagged_reasons={"id": ["value_not_form_state"]},
            flagged_values={},
        )
        assert load_sidecar(path).checked_names == ("id",)


class TestWithIgnored:
    def test_the_line_is_added_once(self) -> None:
        assert with_ignored("", "tests/captures") == "tests/captures/\n"
        assert with_ignored("dist/", "tests/captures") == "dist/\ntests/captures/\n"
        assert with_ignored("tests/captures\n", "tests/captures/") == "tests/captures\n"

    def test_a_crlf_file_gets_a_crlf_line(self) -> None:
        assert with_ignored("dist/\r\n", "tests/captures") == "dist/\r\ntests/captures/\r\n"
        assert with_ignored("dist/", "tests/captures") == "dist/\ntests/captures/\n"
        assert with_ignored("a\r\ndist/", "tests/captures") == "a\r\ndist/\r\ntests/captures/\r\n"


def test_ensure_ignored_appends_a_crlf_line_to_a_crlf_gitignore(tmp_path: Path) -> None:
    gitignore = tmp_path / ".gitignore"
    gitignore.write_bytes(b"*.pyc\r\ndist/\r\n")
    assert ensure_ignored(tmp_path, CAPTURES_DIR)
    assert gitignore.read_bytes() == b"*.pyc\r\ndist/\r\ntests/captures/\r\n"


def test_the_rule_module_imports_nothing_that_touches_the_filesystem() -> None:
    """Scaffold modules import the rule from here, so it stays pure text."""
    assert captures_rule.__file__ is not None
    tree = ast.parse(Path(captures_rule.__file__).read_text(encoding="utf-8"))
    imported = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)} | {
        a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names
    }
    assert imported <= {"__future__"}
