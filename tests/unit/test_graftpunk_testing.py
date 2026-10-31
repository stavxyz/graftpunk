"""graftpunk.testing: make_context, FixtureSession, fixture_context, site_env_scrubber
(plugin tooling spec, 2026-09-11)."""

from __future__ import annotations

import ast
import errno
import hashlib
import json
import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
import requests

import graftpunk.testing.plugin as plugin_module
from graftpunk.graftpunk_session import GraftpunkSession
from graftpunk.plugins.cli_plugin import CommandContext
from graftpunk.testing import FixtureSession, fixture_context, make_context
from graftpunk.testing.plugin import check_fixtures_tree
from graftpunk.testing.recorded import recorded_values
from graftpunk.testing.sidecar import (
    FIXTURES_PLACEHOLDER,
    ObserveRun,
    Sidecar,
    SidecarError,
    load_sidecar,
    sidecar_text,
)


class TestMakeContext:
    def test_builds_a_valid_command_context(self) -> None:
        ctx = make_context(
            plugin_name="myshop", command_name="orders", base_url="https://myshop.example.com"
        )
        assert isinstance(ctx, CommandContext)
        assert ctx.plugin_name == "myshop"
        assert ctx.command_name == "orders"
        assert ctx.base_url == "https://myshop.example.com"

    def test_default_session_is_a_graftpunk_session(self) -> None:
        ctx = make_context()
        assert isinstance(ctx.session, GraftpunkSession)

    def test_given_session_is_used_as_is(self) -> None:
        session = requests.Session()
        ctx = make_context(session=session)
        assert ctx.session is session

    def test_defaults_are_reasonable(self) -> None:
        ctx = make_context()
        assert ctx.plugin_name == "test"
        assert ctx.command_name == "test"
        assert ctx.api_version == 1


class TestFixtureSession:
    def test_answers_a_get_from_the_matching_file(self, tmp_path: Path) -> None:
        (tmp_path / "get_orders.json").write_text('{"orders": []}')
        session = FixtureSession(tmp_path)
        response = session.get("https://myshop.example.com/orders")
        assert response.status_code == 200
        assert response.json() == {"orders": []}

    def test_templated_path_matches_any_id(self, tmp_path: Path) -> None:
        (tmp_path / "get_orders_{order_id}.json").write_text('{"id": 1}')
        session = FixtureSession(tmp_path)
        assert session.get("https://myshop.example.com/orders/1001").json() == {"id": 1}
        assert session.get("https://myshop.example.com/orders/999").json() == {"id": 1}

    def test_unmatched_request_returns_404(self, tmp_path: Path) -> None:
        session = FixtureSession(tmp_path)
        response = session.get("https://myshop.example.com/nothing-here")
        assert response.status_code == 404

    def test_sidecar_supplies_status_and_content_type(self, tmp_path: Path) -> None:
        (tmp_path / "get_orders.json").write_text("Forbidden")
        (tmp_path / "get_orders.json.meta.json").write_text(
            sidecar_text(Sidecar(status=403, content_type="text/plain"))
        )
        session = FixtureSession(tmp_path)
        response = session.get("https://myshop.example.com/orders")
        assert response.status_code == 403
        assert response.headers["Content-Type"] == "text/plain"

    def test_without_sidecar_status_is_200_and_type_from_extension(self, tmp_path: Path) -> None:
        (tmp_path / "get_page.html").write_text("<html></html>")
        session = FixtureSession(tmp_path)
        response = session.get("https://myshop.example.com/page")
        assert response.status_code == 200
        assert response.headers["Content-Type"] == "text/html"

    def test_json_wins_when_a_stem_has_several_extensions(self, tmp_path: Path) -> None:
        """Plain sorted order served the .html error page beside a JSON
        endpoint's own fixture."""
        (tmp_path / "get_orders.html").write_text("<html>error</html>")
        (tmp_path / "get_orders.json").write_text('{"orders": []}')
        session = FixtureSession(tmp_path)
        response = session.get("https://myshop.example.com/orders")
        assert response.headers["Content-Type"] == "application/json"
        assert response.json() == {"orders": []}

    def test_without_a_json_fixture_the_first_in_sorted_order_is_used(self, tmp_path: Path) -> None:
        (tmp_path / "get_orders.html").write_text("<html></html>")
        (tmp_path / "get_orders.txt").write_text("plain")
        session = FixtureSession(tmp_path)
        assert session.get("https://myshop.example.com/orders").text == "<html></html>"

    def test_never_opens_a_socket(self, tmp_path: Path) -> None:
        """No fixture file for this path: still answers 404 rather than connecting.

        The address is RFC 5737 TEST-NET-1, reserved for documentation. The
        cloud metadata address it used to name is a live endpoint on any cloud
        instance, so a regression here would have reached for credentials
        rather than simply failing.
        """
        session = FixtureSession(tmp_path)
        response = session.get("http://192.0.2.1/nonexistent")
        assert response.status_code == 404

    def test_a_sidecar_with_no_schema_is_refused_not_half_read(self, tmp_path: Path) -> None:
        (tmp_path / "get_orders.json").write_text("{}")
        (tmp_path / "get_orders.json.meta.json").write_text(json.dumps({"status": 403}))
        session = FixtureSession(tmp_path)
        with pytest.raises(SidecarError, match="get_orders.json.meta.json"):
            session.get("https://myshop.example.com/orders")

    def test_a_malformed_sidecar_is_not_caught_by_except_value_error(self, tmp_path: Path) -> None:
        """Plugin command code routinely wraps a fixture-backed request in
        ``except ValueError`` (around ``.json()``, say); SidecarError must not be
        a ValueError or a malformed sidecar would be silently swallowed there."""
        (tmp_path / "get_orders.json").write_text("{}")
        (tmp_path / "get_orders.json.meta.json").write_text(json.dumps({"status": 403}))
        session = FixtureSession(tmp_path)
        with pytest.raises(SidecarError):
            try:
                session.get("https://myshop.example.com/orders")
            except ValueError:
                pytest.fail("SidecarError was caught by except ValueError")


class TestFixtureContext:
    def test_is_make_context_with_a_fixture_session(self, tmp_path: Path) -> None:
        (tmp_path / "get_orders.json").write_text("{}")
        ctx = fixture_context(tmp_path, plugin_name="myshop", base_url="https://myshop.example.com")
        assert isinstance(ctx.session, FixtureSession)
        assert ctx.request_json("GET", "/orders") == {}


pytest_plugins = ["pytester"]


class TestSiteEnvScrubber:
    """Driven through a real inner pytest run (``pytester``), not by reaching
    into the ``@pytest.fixture``-wrapped generator's internals, since a
    fixture is meant to be exercised through pytest's own protocol."""

    def test_removes_prefixed_vars_for_the_test_and_restores_after(
        self, pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MYSHOP_USERNAME", "alice")
        monkeypatch.setenv("OTHER_VAR", "kept")
        pytester.makepyfile(
            conftest="""
            from graftpunk.testing.plugin import site_env_scrubber
            scrub_site_env = site_env_scrubber("MYSHOP_")
            """,
            test_scrub="""
            import os

            def test_scrubbed():
                assert "MYSHOP_USERNAME" not in os.environ
                assert os.environ["OTHER_VAR"] == "kept"
            """,
        )
        result = pytester.runpytest_inprocess("-o", "asyncio_default_fixture_loop_scope=function")
        result.assert_outcomes(passed=1)
        assert os.environ["MYSHOP_USERNAME"] == "alice"


class TestImportableWithoutPytest:
    def test_testing_package_importable_with_pytest_hidden(self) -> None:
        script = textwrap.dedent(
            """
            import builtins

            real_import = builtins.__import__

            def _blocked(name, *args, **kwargs):
                if name == "pytest" or name.startswith("pytest."):
                    raise ImportError("pytest is not installed")
                return real_import(name, *args, **kwargs)

            builtins.__import__ = _blocked

            from graftpunk.testing import FixtureSession, make_context

            assert callable(make_context)
            assert FixtureSession is not None
            print("OK")
            """
        )
        result = subprocess.run(  # noqa: S603
            [sys.executable, "-c", script], capture_output=True, text=True, timeout=30
        )
        assert result.returncode == 0, result.stderr
        assert "OK" in result.stdout


def test_a_stem_holding_glob_characters_is_matched_literally(tmp_path: Path) -> None:
    """The stem is escaped before globbing."""
    (tmp_path / "get_items_[x].json").write_text('{"id": 1}')
    (tmp_path / "get_items_x.json").write_text('{"id": 2}')
    session = FixtureSession(tmp_path)
    assert session.get("https://myshop.example.com/items/[x]").json() == {"id": 1}


def test_a_repeat_capture_is_never_the_fixture_for_a_numeric_segment(tmp_path: Path) -> None:
    """A repeat suffix cannot read as a path segment."""
    (tmp_path / "get_orders_{order_id}#1.json").write_text('{"repeat": true}')
    session = FixtureSession(tmp_path)
    assert session.get("https://myshop.example.com/orders/1").status_code == 404


def test_a_fixture_is_its_stem_plus_one_extension(tmp_path: Path) -> None:
    """A file named get_api_users.csv.txt is another endpoint's fixture, not the
    fixture for /api/users."""
    (tmp_path / "get_api_users.csv.txt").write_text("a,b")
    (tmp_path / "get_feed.xml.xml").write_text("<feed/>")
    session = FixtureSession(tmp_path)
    assert session.get("https://myshop.example.com/api/users").status_code == 404
    assert session.get("https://myshop.example.com/feed").status_code == 404
    (tmp_path / "get_api_users.json").write_text('{"users": []}')
    assert session.get("https://myshop.example.com/api/users").json() == {"users": []}


_GENERATED_CONFTEST = """
from pathlib import Path

from graftpunk.testing.plugin import fixtures_are_sanitised

FIXTURES_TREE = Path(__file__).parent / "fixtures"

sanitised_fixtures = fixtures_are_sanitised(FIXTURES_TREE)
"""


def _fixture(tree: Path, relative: str, body: bytes, sidecar: Sidecar | None) -> Path:
    path = tree / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    if sidecar is not None:
        path.with_name(path.name + ".meta.json").write_text(sidecar_text(sidecar))
    return path


def _captured(body: bytes, *flagged: str) -> Sidecar:
    return Sidecar(
        status=200,
        content_type="application/json",
        capture_sha256=hashlib.sha256(body).hexdigest(),
        flagged_reasons=dict.fromkeys(flagged, ("session_name",)),
    )


class TestCheckFixturesTree:
    """The one enforcer of "a committed fixture came off no account unchanged"
    (graft skill spec, 2026-09-21)."""

    def test_a_missing_tree_fails(self, tmp_path: Path) -> None:
        report = check_fixtures_tree(tmp_path / "fixtures")
        assert len(report.problems) == 1
        assert "does not exist" in report.problems[0]
        assert "gp plugin upgrade" in report.problems[0]
        assert f"with a {FIXTURES_PLACEHOLDER}" in report.problems[0]

    def test_the_missing_tree_message_names_the_one_placeholder_constant(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The message interpolates FIXTURES_PLACEHOLDER rather than spelling
        .gitkeep beside it, so the two cannot drift apart."""
        monkeypatch.setattr(plugin_module, "FIXTURES_PLACEHOLDER", ".marker")
        report = check_fixtures_tree(tmp_path / "fixtures")
        assert "with a .marker" in report.problems[0]

    def test_an_empty_tree_with_its_placeholder_passes(self, tmp_path: Path) -> None:
        (tmp_path / ".gitkeep").write_text("")
        assert check_fixtures_tree(tmp_path).problems == ()

    def test_a_fixture_with_no_sidecar_fails_and_says_how_to_make_one(self, tmp_path: Path) -> None:
        _fixture(tmp_path, "get_orders.json", b"{}", None)
        (problem,) = check_fixtures_tree(tmp_path).problems
        assert "get_orders.json: no sidecar" in problem
        assert "gp observe fixtures" in problem
        assert '"capture_sha256": null' in problem

    def test_a_suite_member_added_after_the_conftest_is_covered(self, tmp_path: Path) -> None:
        """The walk covers the whole tree, so a second member's root needs no
        per-plugin fact in the conftest."""
        _fixture(tmp_path, "myshop/get_orders.json", b"{}", None)
        _fixture(tmp_path, "widgets/get_widgets.json", b"{}", None)
        problems = check_fixtures_tree(tmp_path).problems
        assert any(p.startswith("myshop/get_orders.json") for p in problems)
        assert any(p.startswith("widgets/get_widgets.json") for p in problems)

    def test_an_unchanged_copy_of_the_capture_fails(self, tmp_path: Path) -> None:
        body = b'{"orders": [{"id": "1001"}]}'
        _fixture(tmp_path, "get_orders.json", body, _captured(body))
        (problem,) = check_fixtures_tree(tmp_path).problems
        assert "unchanged copy" in problem

    def test_a_flagged_name_in_the_body_fails(self, tmp_path: Path) -> None:
        _fixture(
            tmp_path,
            "get_orders.json",
            b'{"myshop_session": "invented"}',
            _captured(b"captured", "myshop_session"),
        )
        (problem,) = check_fixtures_tree(tmp_path).problems
        assert "'myshop_session'" in problem
        assert "(flagged by: session_name)" in problem
        assert problem.endswith("rename it in the fixture.")

    def test_the_missing_sidecar_hint_describes_a_sidecar_that_loads(self, tmp_path: Path) -> None:
        """Every key the hint names, with its value, makes a sidecar the loader
        accepts, so an author following it is not refused."""
        _fixture(tmp_path, "get_orders.json", b"{}", None)
        (problem,) = check_fixtures_tree(tmp_path).problems
        keys = dict(re.findall(r'"(\w+)": (\[\]|\{\}|null|\d+)', problem))
        payload = {key: json.loads(value) for key, value in keys.items()}
        payload |= {"status": 200, "content_type": "application/json"}
        path = tmp_path / "x.json.meta.json"
        path.write_text(json.dumps(payload))
        assert load_sidecar(path).flagged_reasons == {}

    def test_a_schema_1_sidecar_names_no_reasons(self, tmp_path: Path) -> None:
        """A sidecar committed under schema 1 recorded no reasons, so the problem
        line says nothing it cannot back."""
        path = _fixture(tmp_path, "get_orders.json", b'{"myshop_session": "x"}', None)
        payload = {
            "schema": 1,
            "status": 200,
            "content_type": "application/json",
            "body_params": [],
            "capture_sha256": hashlib.sha256(b"captured").hexdigest(),
            "flagged_names": ["myshop_session"],
            "redacted_names": 0,
        }
        path.with_name(path.name + ".meta.json").write_text(json.dumps(payload))
        (problem,) = check_fixtures_tree(tmp_path).problems
        assert "'myshop_session'" in problem
        assert "flagged by" not in problem

    def test_a_flagged_name_in_the_content_type_passes(self, tmp_path: Path) -> None:
        """gp writes content_type from the response; a media type holds no cookie
        value, so a cookie named text does not fail the text/html sidecar gp wrote
        (#270)."""
        sidecar = Sidecar(
            status=200,
            content_type="text/html; charset=utf-8",
            capture_sha256=hashlib.sha256(b"captured").hexdigest(),
            flagged_reasons=dict.fromkeys(
                ("text", "html", "charset", "utf-8"), ("value_not_form_state",)
            ),
        )
        _fixture(tmp_path, "get_search.html", b"<p>Results</p>", sidecar)
        assert check_fixtures_tree(tmp_path).problems == ()

    def test_a_flagged_name_in_body_params_alone_passes(self, tmp_path: Path) -> None:
        """body_params holds request field names, which can legitimately include
        a flagged CSRF field's name; no sidecar field is scanned, so this is not
        a leak."""
        sidecar = Sidecar(
            status=200,
            content_type="application/json",
            body_params=("authenticity_token",),
            capture_sha256=hashlib.sha256(b"captured").hexdigest(),
            flagged_reasons={"authenticity_token": ("token_name",)},
        )
        _fixture(tmp_path, "post_session.json", b'{"ok": true}', sidecar)
        assert check_fixtures_tree(tmp_path).problems == ()

    @pytest.mark.skipif(os.geteuid() == 0, reason="root reads a 000-mode file")
    def test_an_unreadable_fixture_is_a_problem_and_the_check_goes_on(self, tmp_path: Path) -> None:
        """A fixture the runner cannot read is one problem line, not a
        PermissionError out of the check, and the fixtures after it are still
        checked."""
        unreadable = _fixture(
            tmp_path, "a_orders.json", b"{}", Sidecar(status=200, content_type="x")
        )
        _fixture(tmp_path, "b_invoices.json", b"{}", None)
        unreadable.chmod(0o000)
        try:
            problems = check_fixtures_tree(tmp_path).problems
        finally:
            unreadable.chmod(0o644)
        assert len(problems) == 2
        assert problems[0] == f"a_orders.json: cannot be read ({os.strerror(errno.EACCES)})."
        assert problems[1].startswith("b_invoices.json: no sidecar")

    @pytest.mark.skipif(os.geteuid() == 0, reason="root reads a 000-mode directory")
    def test_an_unlistable_subdirectory_is_a_problem_and_the_check_goes_on(
        self, tmp_path: Path
    ) -> None:
        """A subdirectory the runner cannot even list is one problem line, not
        a traceback out of the check, and a readable sibling fixture is still
        checked."""
        _fixture(tmp_path, "sub/get_orders.json", b"{}", Sidecar(status=200, content_type="x"))
        _fixture(tmp_path, "b_invoices.json", b"{}", None)
        sub = tmp_path / "sub"
        sub.chmod(0o000)
        try:
            problems = check_fixtures_tree(tmp_path).problems
        finally:
            sub.chmod(0o755)
        assert len(problems) == 2
        assert problems[0] == f"sub: cannot be read ({os.strerror(errno.EACCES)})."
        assert problems[1].startswith("b_invoices.json: no sidecar")

    @pytest.mark.skipif(os.geteuid() == 0, reason="root reads a 644-mode directory's entries")
    def test_an_untraversable_subdirectory_is_a_problem_and_the_check_goes_on(
        self, tmp_path: Path
    ) -> None:
        """A subdirectory the runner can list (read) but not search (no
        execute) can still be listed, so the fixture inside it shows up as a
        name; checking that name's type raises, not list()ing it, so the same
        one problem line still covers it."""
        _fixture(tmp_path, "sub/get_orders.json", b"{}", None)
        _fixture(tmp_path, "b_invoices.json", b"{}", None)
        sub = tmp_path / "sub"
        sub.chmod(0o644)
        try:
            problems = check_fixtures_tree(tmp_path).problems
        finally:
            sub.chmod(0o755)
        assert len(problems) == 2
        assert problems[0] == f"sub: cannot be read ({os.strerror(errno.EACCES)})."
        assert problems[1].startswith("b_invoices.json: no sidecar")

    @pytest.mark.skipif(os.geteuid() == 0, reason="root reads a 644-mode directory's entries")
    def test_an_untraversable_directory_holding_only_a_subdirectory_is_one_line(
        self, tmp_path: Path
    ) -> None:
        """A directory the runner can list but not search, holding only a
        subdirectory (no file directly inside it), is blamed on itself, not
        on the subdirectory ``os.walk`` would otherwise try to descend
        into: the dirnames loop has to notice the same "unreadable" kind
        the filenames loop already does."""
        group = tmp_path / "group"
        (group / "sub1").mkdir(parents=True)
        (group / "sub1" / "a.json").write_bytes(b"{}")
        group.chmod(0o644)
        try:
            problems = check_fixtures_tree(tmp_path).problems
        finally:
            group.chmod(0o755)
        assert problems == (f"group: cannot be read ({os.strerror(errno.EACCES)}).",)

    @pytest.mark.skipif(os.geteuid() == 0, reason="root reads a 644-mode directory's entries")
    def test_an_untraversable_directory_holding_a_file_and_a_subdirectory_is_still_one_line(
        self, tmp_path: Path
    ) -> None:
        """The same directory, holding a file directly as well as a
        subdirectory, still gets exactly one problem line, not one for the
        file and another for the subdirectory."""
        group = tmp_path / "group"
        (group / "sub1").mkdir(parents=True)
        (group / "sub1" / "a.json").write_bytes(b"{}")
        (group / "z.json").write_bytes(b"{}")
        group.chmod(0o644)
        try:
            problems = check_fixtures_tree(tmp_path).problems
        finally:
            group.chmod(0o755)
        assert problems == (f"group: cannot be read ({os.strerror(errno.EACCES)}).",)

    @pytest.mark.skipif(os.geteuid() == 0, reason="root reads a 000-mode directory")
    def test_an_unlistable_tree_root_is_labelled_by_the_tree_not_a_dot(
        self, tmp_path: Path
    ) -> None:
        """When the fixtures tree itself cannot be listed, the problem names
        the tree (as the caller gave it), not the relative path ".", which
        reads as the working directory rather than the fixtures tree."""
        tmp_path.chmod(0o000)
        try:
            problems = check_fixtures_tree(tmp_path).problems
        finally:
            tmp_path.chmod(0o755)
        assert problems == (f"{tmp_path}: cannot be read ({os.strerror(errno.EACCES)}).",)

    @pytest.mark.skipif(os.geteuid() == 0, reason="root reads a 644-mode directory's entries")
    def test_an_untraversable_tree_root_is_labelled_by_the_tree_not_a_dot(
        self, tmp_path: Path
    ) -> None:
        """When the fixtures tree is listable but not searchable and holds a
        fixture directly, the same "." mislabel happens by the other code
        path (classifying the entry, not listing the directory)."""
        _fixture(tmp_path, "get_orders.json", b"{}", None)
        tmp_path.chmod(0o644)
        try:
            problems = check_fixtures_tree(tmp_path).problems
        finally:
            tmp_path.chmod(0o755)
        assert problems == (f"{tmp_path}: cannot be read ({os.strerror(errno.EACCES)}).",)

    @pytest.mark.skipif(os.geteuid() == 0, reason="root reads a 644-mode directory's entries")
    def test_a_tree_whose_parent_is_untraversable_cannot_be_read(self, tmp_path: Path) -> None:
        """The tree check stats the tree itself, not an ``is_dir()`` predicate,
        so the message does not depend on the Python version."""
        parent = tmp_path / "tests"
        parent.mkdir()
        tree = parent / "fixtures"
        tree.mkdir()
        parent.chmod(0o644)
        try:
            problems = check_fixtures_tree(tree).problems
        finally:
            parent.chmod(0o755)
        assert problems == (f"{tree}: cannot be read ({os.strerror(errno.EACCES)}).",)

    def test_a_tree_that_is_a_regular_file_is_not_a_directory(self, tmp_path: Path) -> None:
        """A tree that exists but is not a directory gets its own line,
        distinct from "does not exist"."""
        tree = tmp_path / "fixtures"
        tree.write_text("")
        problems = check_fixtures_tree(tree).problems
        assert problems == (f"{tree}: not a directory. Move it aside, then run gp plugin upgrade.",)

    def test_a_dangling_symlink_tree_is_not_a_directory_not_does_not_exist(
        self, tmp_path: Path
    ) -> None:
        """A dangling symlink at the tree path is told apart from a plainly
        missing tree: gp plugin upgrade can create a directory where there
        is nothing, but refuses to move a symlink aside itself, so the
        advice has to be the "not a directory" line, matching what gp
        plugin upgrade and gp plugin check say about the same shape."""
        tree = tmp_path / "fixtures"
        tree.symlink_to(tmp_path / "nowhere")
        problems = check_fixtures_tree(tree).problems
        assert problems == (f"{tree}: not a directory. Move it aside, then run gp plugin upgrade.",)

    def test_a_self_looping_symlink_tree_is_not_a_directory(self, tmp_path: Path) -> None:
        tree = tmp_path / "fixtures"
        tree.symlink_to(tree)
        problems = check_fixtures_tree(tree).problems
        assert problems == (f"{tree}: not a directory. Move it aside, then run gp plugin upgrade.",)

    @pytest.mark.skipif(os.geteuid() == 0, reason="root reads a 000-mode directory")
    def test_a_symlink_entry_into_an_unreadable_location_is_its_own_problem(
        self, tmp_path: Path
    ) -> None:
        """A symlink entry whose own target cannot be reached is that entry's
        problem, not the whole directory's: the walk goes on to check its
        siblings."""
        priv = tmp_path / "priv" / "inner"
        priv.mkdir(parents=True)
        (priv / "x.json").write_text("{}")
        fx = tmp_path / "fx"
        fx.mkdir()
        (fx / "z_orders.json").write_bytes(b"{}")
        (fx / "m_link.json").symlink_to(Path("..") / "priv" / "inner" / "x.json")
        (tmp_path / "priv").chmod(0o000)
        try:
            problems = check_fixtures_tree(fx).problems
        finally:
            (tmp_path / "priv").chmod(0o755)
        assert len(problems) == 2
        assert f"m_link.json: cannot be read ({os.strerror(errno.EACCES)})." in problems
        assert any(p.startswith("z_orders.json: no sidecar") for p in problems)

    @pytest.mark.skipif(os.geteuid() == 0, reason="root reads a 000-mode directory")
    def test_a_sidecar_into_an_unreadable_location_cannot_be_read(self, tmp_path: Path) -> None:
        """A sidecar that is a symlink into a blocked location is "cannot be
        read", never a traceback and never a false "no sidecar"."""
        priv = tmp_path / "priv" / "inner"
        priv.mkdir(parents=True)
        (priv / "s.meta.json").write_text(sidecar_text(Sidecar(status=200, content_type="x")))
        fx = tmp_path / "fx_a"
        fx.mkdir()
        (fx / "a.json").write_bytes(b"{}")
        (fx / "a.json.meta.json").symlink_to(Path("..") / "priv" / "inner" / "s.meta.json")
        (tmp_path / "priv").chmod(0o000)
        try:
            problems = check_fixtures_tree(fx).problems
        finally:
            (tmp_path / "priv").chmod(0o755)
        assert problems == (f"a.json.meta.json: cannot be read ({os.strerror(errno.EACCES)}).",)

    def test_a_sidecar_path_occupied_by_a_directory_is_not_a_regular_file(
        self, tmp_path: Path
    ) -> None:
        """A directory sitting where the sidecar belongs is never told to
        "write" a sidecar, which it already has in some sense: it gets the
        same "not a regular file" advice as any other blocked path."""
        _fixture(tmp_path, "get_orders.json", b"{}", None)
        (tmp_path / "get_orders.json.meta.json").mkdir()
        (problem,) = check_fixtures_tree(tmp_path).problems
        assert problem == (
            "get_orders.json.meta.json: not a regular file. Move it aside and write the sidecar."
        )

    def test_a_looping_symlink_sidecar_is_not_a_regular_file(self, tmp_path: Path) -> None:
        """A sidecar path occupied by a symlink that loops back on itself is
        never told to "write" a sidecar there: a symlink already sits at the
        path, and writing through a loop raises ELOOP."""
        _fixture(tmp_path, "get_orders.json", b"{}", None)
        meta = tmp_path / "get_orders.json.meta.json"
        meta.symlink_to(meta)
        (problem,) = check_fixtures_tree(tmp_path).problems
        assert problem == (
            "get_orders.json.meta.json: not a regular file. Move it aside and write the sidecar."
        )

    def test_a_dangling_symlink_sidecar_is_not_a_regular_file(self, tmp_path: Path) -> None:
        """A sidecar path occupied by a symlink to a missing target gets the
        same advice: following "write it" would land the sidecar at the
        link's target, which can be outside the tree."""
        _fixture(tmp_path, "get_orders.json", b"{}", None)
        (tmp_path / "get_orders.json.meta.json").symlink_to(tmp_path / "nowhere")
        (problem,) = check_fixtures_tree(tmp_path).problems
        assert problem == (
            "get_orders.json.meta.json: not a regular file. Move it aside and write the sidecar."
        )

    def test_a_symlinked_fixtures_subdirectory_is_walked(self, tmp_path: Path) -> None:
        """A per-plugin fixtures directory that is itself a symlink (into a
        captures directory the developer pointed it at, say) is still
        walked: the fixture inside it is checked like any other, not
        silently skipped because the walk never follows the link."""
        captures = tmp_path / "captures" / "myshop"
        captures.mkdir(parents=True)
        body = b'{"orders": [{"id": "1001"}]}'
        _fixture(captures, "orders.json", body, _captured(body))
        fixtures = tmp_path / "fixtures"
        fixtures.mkdir()
        (fixtures / "myshop").symlink_to(captures)
        report = check_fixtures_tree(fixtures)
        assert report.verified == 1
        assert any("unchanged copy of its capture" in p for p in report.problems)

    def test_two_symlinks_to_one_real_directory_check_its_fixture_once(
        self, tmp_path: Path
    ) -> None:
        """Two symlinks pointing at the same real directory (not a cycle:
        neither sits under the other) do not each get walked (which would
        report its fixture's missing sidecar twice); a stat-identity set
        lets only the first one through."""
        real = tmp_path / "real"
        real.mkdir()
        _fixture(real, "get_orders.json", b"{}", None)
        (tmp_path / "link_one").symlink_to(real)
        (tmp_path / "link_two").symlink_to(real)
        report = check_fixtures_tree(tmp_path)
        no_sidecar = [p for p in report.problems if "get_orders.json: no sidecar" in p]
        assert len(no_sidecar) == 1

    def test_a_symlink_back_to_the_tree_is_a_true_cycle_checked_once(self, tmp_path: Path) -> None:
        """A symlink under the tree pointing back at the tree's own root is
        an actual cycle, not just two paths to one directory. The tree's
        own identity is seeded into the visited set before the walk starts
        (``_collect_fixtures``'s *tree_stat* parameter); without that seed,
        the walk would re-enter the tree through the link and report the
        root fixture's missing sidecar a second time."""
        _fixture(tmp_path, "get_orders.json", b"{}", None)
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "back").symlink_to(tmp_path)
        report = check_fixtures_tree(tmp_path)
        no_sidecar = [p for p in report.problems if "no sidecar" in p]
        assert len(no_sidecar) == 1

    def test_a_dangling_symlink_and_a_loop_are_skipped_not_reported(self, tmp_path: Path) -> None:
        """Neither a dangling symlink nor a symlink loop is a fixture or a
        problem; only the real fixture's missing sidecar is reported."""
        (tmp_path / "dangling.json").symlink_to(tmp_path / "missing.json")
        (tmp_path / "loop_a.json").symlink_to(tmp_path / "loop_b.json")
        (tmp_path / "loop_b.json").symlink_to(tmp_path / "loop_a.json")
        _fixture(tmp_path, "get_orders.json", b"{}", None)
        problems = check_fixtures_tree(tmp_path).problems
        assert len(problems) == 1
        assert problems[0].startswith("get_orders.json: no sidecar")

    def test_a_sidecar_of_unknown_schema_fails(self, tmp_path: Path) -> None:
        path = _fixture(tmp_path, "get_orders.json", b"{}", None)
        payload = json.loads(sidecar_text(Sidecar(status=200, content_type="x")))
        payload["schema"] = 99
        path.with_name(path.name + ".meta.json").write_text(json.dumps(payload))
        (problem,) = check_fixtures_tree(tmp_path).problems
        assert "schema 99" in problem

    def test_an_invented_fixture_passes_and_counts_as_verified(self, tmp_path: Path) -> None:
        _fixture(
            tmp_path,
            "get_orders.json",
            b'{"orders": [{"id": "9001"}]}',
            _captured(b'{"orders": [{"id": "1001"}]}', "myshop_session"),
        )
        report = check_fixtures_tree(tmp_path)
        assert (report.problems, report.verified, report.declared) == ((), 1, 0)

    def test_a_hand_made_fixture_with_no_capture_hash_passes_on_declaration(
        self, tmp_path: Path
    ) -> None:
        _fixture(tmp_path, "get_orders.json", b"{}", Sidecar(status=200, content_type="x"))
        report = check_fixtures_tree(tmp_path)
        assert (report.problems, report.verified, report.declared) == ((), 0, 1)
        assert "1 accepted on declaration" in report.summary

    def test_a_binary_fixture_is_checked_without_decoding_errors(self, tmp_path: Path) -> None:
        body = b"%PDF-1.7\n\xff\xfe\x00invented"
        _fixture(tmp_path, "get_invoice.pdf", body, _captured(b"%PDF captured", "myshop_session"))
        assert check_fixtures_tree(tmp_path).problems == ()

    def test_an_empty_flagged_name_is_ignored(self, tmp_path: Path) -> None:
        """An empty string in flagged_names would match between every two characters."""
        _fixture(
            tmp_path,
            "get_orders.json",
            b'{"orders": []}',
            _captured(b"captured", "myshop_session", ""),
        )
        assert check_fixtures_tree(tmp_path).problems == ()

    def test_a_macos_ds_store_is_skipped(self, tmp_path: Path) -> None:
        (tmp_path / ".DS_Store").write_bytes(b"\x00\x01")
        assert check_fixtures_tree(tmp_path).problems == ()

    def test_an_editor_swap_file_is_skipped(self, tmp_path: Path) -> None:
        (tmp_path / ".orders.json.swp").write_bytes(b"swap")
        assert check_fixtures_tree(tmp_path).problems == ()

    def test_a_dotfile_under_a_plugin_directory_is_skipped_too(self, tmp_path: Path) -> None:
        (tmp_path / "myshop").mkdir()
        (tmp_path / "myshop" / ".DS_Store").write_bytes(b"\x00\x01")
        assert check_fixtures_tree(tmp_path).problems == ()

    def test_a_flagged_name_is_matched_case_insensitively_in_the_body(self, tmp_path: Path) -> None:
        _fixture(
            tmp_path,
            "get_orders.json",
            b'{"x-csrf-token": "invented"}',
            _captured(b"captured", "X-Csrf-Token"),
        )
        (problem,) = check_fixtures_tree(tmp_path).problems
        assert "'X-Csrf-Token'" in problem

    @pytest.mark.parametrize(
        "body",
        [
            b'{"text": "invented"}',
            b"<input name='text' value='invented'>",
            b"q=shoes&text=invented",
            b"Enter TEXT to search",
            b"next=%22text%22",
            b"[text]",
            b"Type your search into the text.",
            b"the `text` cookie",
            b"*text* holds the search",
            b"text!",
            b"var $text = 1;",
            b'{"url": "/x?a=1\\u0026text=Qm7Lp2Xv9Kd4"}',
            b'{"note": "\\u0022text\\u0022: zz"}',
            b"var u = '/x?a=1\\x26text=Qm7Lp2Xv9Kd4';",
            b"<a href='/r?next=%2522text%2522'>",
            b"next=a%253D1%2526text%253Dabc",
            b'<a href="/x?a=1&amp;text=abc">',
            b'{"note": "line one\\ntext=Qm7Lp2Xv9Kd4"}',
            b'{"note": "a\\ttext=Qm7Lp2Xv9Kd4"}',
            b'{"raw": "Host: x\\r\\ntext=Qm7Lp2Xv9Kd4"}',
            b"{'cookies': 'a=1\\ntext=Qm7'}",
            b'{"location": "https://app.example.com/cb#text=eyJhbGciOi"}',
            b"next=https%3A%2F%2Fapp.example.com%2Fcb%23text%3DeyJ",
            b"name|text|value",
        ],
        ids=[
            "json-key",
            "single-quoted",
            "after-ampersand",
            "prose",
            "percent-encoded",
            "bracket",
            "sentence-end",
            "backticks",
            "asterisks",
            "exclamation",
            "dollar-variable",
            "json-escaped-ampersand",
            "json-escaped-quote",
            "js-hex-escape",
            "double-percent-quote",
            "double-percent-pair",
            "html-entity",
            "json-newline",
            "json-tab",
            "json-crlf",
            "python-repr-newline",
            "url-fragment",
            "percent-encoded-url-fragment",
            "pipe-separated",
        ],
    )
    def test_a_flagged_name_as_a_whole_name_fails(self, tmp_path: Path, body: bytes) -> None:
        _fixture(tmp_path, "get_search.json", body, _captured(b"captured", "text"))
        (problem,) = check_fixtures_tree(tmp_path).problems
        assert "'text'" in problem

    @pytest.mark.parametrize(
        "body",
        [
            b'{"context": "invented"}',
            b"textarea { text-align: left; }",
            b'{"subtext": 1, "text_size": 2, "text.v2": 3, "a$text": 4, "my.text": 5}',
        ],
        ids=["inside-a-word", "hyphenated", "joined-by-name-characters"],
    )
    def test_a_flagged_name_inside_a_longer_name_passes(self, tmp_path: Path, body: bytes) -> None:
        """A cookie name is an RFC 6265 token, so a flagged name counts only where
        the characters around it cannot be part of the same name (#270): letters,
        digits, _ and -, or token punctuation between two of those."""
        _fixture(tmp_path, "get_search.json", body, _captured(b"captured", "text"))
        assert check_fixtures_tree(tmp_path).problems == ()


_RECORDED_SESSION = "Qz7XkLmPwR4t7Bv2Hn8Ks3JdVv"
_RUN = ObserveRun(plugin="myshop", run="20261006-101500-4242")


def _write_run(observe: Path, session_dir: str, run_id: str, cookie_value: str) -> Path:
    """A run as gp observe stores one: observe/<session>/<run>/network.har, its one
    request sending ``myshop_session`` with *cookie_value*."""
    run_dir = observe / session_dir / run_id
    run_dir.mkdir(parents=True)
    entry = {
        "startedDateTime": "2026-10-06T10:15:00.000Z",
        "time": 1,
        "request": {
            "method": "GET",
            "url": "https://myshop.example.com/api/orders",
            "headers": [],
            "cookies": [{"name": "myshop_session", "value": cookie_value}],
        },
        "response": {
            "status": 200,
            "statusText": "OK",
            "headers": [],
            "cookies": [],
            "content": {"mimeType": "application/json", "text": "{}"},
        },
    }
    (run_dir / "network.har").write_text(json.dumps({"log": {"entries": [entry]}}))
    return run_dir


def _from_run(body: bytes, run: ObserveRun | None = _RUN) -> Sidecar:
    """A schema 4 sidecar whose capture held none of the session's values, so the
    name check does not look for ``myshop_session``."""
    return Sidecar(
        status=200,
        content_type="application/json",
        capture_sha256=hashlib.sha256(b"captured " + body).hexdigest(),
        flagged_reasons={"myshop_session": ("sent_only", "session_name")},
        checked_names=(),
        observe_run=run,
    )


_PASTED = b'{\n  "id": 7,\n  "myshop_session": "' + _RECORDED_SESSION.encode() + b'"\n}\n'


class TestTheLocalValueCheck:
    """#345: with a schema 4 sidecar whose observe run is on this machine, the
    fixture is also compared with every value the run recorded."""

    def test_a_recorded_value_pasted_under_its_name_fails_when_the_run_is_present(
        self, tmp_path: Path
    ) -> None:
        observe = tmp_path / "observe"
        _write_run(observe, "myshop", _RUN.run, _RECORDED_SESSION)
        tree = tmp_path / "fixtures"
        _fixture(tree, "get_orders.json", _PASTED, _from_run(_PASTED))
        report = check_fixtures_tree(tree, observe_dir=observe)
        (problem,) = report.problems
        assert problem.startswith("get_orders.json: the recorded value of flagged 'myshop_session'")
        assert "(line 3)" in problem
        assert "20261006-101500-4242 of myshop" in problem
        assert _RECORDED_SESSION not in problem
        assert _RECORDED_SESSION not in report.summary
        assert report.run_checked == 1
        assert "1 compared with the values their observe run recorded" in report.summary

    def test_the_same_fixture_passes_when_the_run_is_absent(self, tmp_path: Path) -> None:
        """CI holds no run: the check is schema 3's, and says nothing beyond the
        summary's count."""
        tree = tmp_path / "fixtures"
        _fixture(tree, "get_orders.json", _PASTED, _from_run(_PASTED))
        report = check_fixtures_tree(tree, observe_dir=tmp_path / "observe")
        assert report.problems == ()
        assert report.run_checked == 0
        assert "0 compared with the values their observe run recorded" in report.summary

    def test_a_fixture_holding_no_recorded_value_passes_with_the_run_present(
        self, tmp_path: Path
    ) -> None:
        observe = tmp_path / "observe"
        _write_run(observe, "myshop", _RUN.run, _RECORDED_SESSION)
        tree = tmp_path / "fixtures"
        body = b'{"id": 7, "myshop_session": "InventedSessionValue"}'
        _fixture(tree, "get_orders.json", body, _from_run(body))
        report = check_fixtures_tree(tree, observe_dir=observe)
        assert report.problems == ()
        assert report.run_checked == 1

    @pytest.mark.parametrize(
        ("recorded", "fails"),
        [("true", False), ("USD", False), (_RECORDED_SESSION, True)],
        ids=["true", "a-word", "opaque"],
    )
    def test_a_recorded_value_that_holds_nothing_is_not_looked_for(
        self, tmp_path: Path, recorded: str, fails: bool
    ) -> None:
        """A cookie flagged by its name alone can record ``true``; the fixture's
        JSON literal ``true`` cannot be invented away, so such a value is not
        looked for. An opaque recorded value still fails."""
        observe = tmp_path / "observe"
        _write_run(observe, "myshop", _RUN.run, recorded)
        tree = tmp_path / "fixtures"
        body = f'{{"orders": [], "active": true, "currency": "USD", "t": "{_RECORDED_SESSION}"}}'
        _fixture(tree, "get_orders.json", body.encode(), _from_run(body.encode()))
        report = check_fixtures_tree(tree, observe_dir=observe)
        assert bool(report.problems) is fails
        assert report.run_checked == 1

    def test_a_run_of_another_plugin_is_not_used(self, tmp_path: Path) -> None:
        """Only the plugin's own session directories are looked in: the same run id
        under another plugin's directory, or under a directory that merely starts
        with the plugin's name, is not read."""
        observe = tmp_path / "observe"
        _write_run(observe, "othershop", _RUN.run, _RECORDED_SESSION)
        _write_run(observe, "myshopping", _RUN.run, _RECORDED_SESSION)
        tree = tmp_path / "fixtures"
        _fixture(tree, "get_orders.json", _PASTED, _from_run(_PASTED))
        report = check_fixtures_tree(tree, observe_dir=observe)
        assert report.problems == ()
        assert report.run_checked == 0

    def test_a_run_of_an_account_session_is_found_by_the_plugin_alone(self, tmp_path: Path) -> None:
        """The sidecar names ``myshop``, never the account label; the run of
        ``myshop@alice`` is stored under ``myshop-alice`` (session_dirname) and is
        found there, and nothing printed names the label."""
        observe = tmp_path / "observe"
        _write_run(observe, "myshop-alice", _RUN.run, _RECORDED_SESSION)
        (observe / "myshop-bob").mkdir()
        tree = tmp_path / "fixtures"
        _fixture(tree, "get_orders.json", _PASTED, _from_run(_PASTED))
        report = check_fixtures_tree(tree, observe_dir=observe)
        (problem,) = report.problems
        assert "'myshop_session'" in problem
        assert "alice" not in problem
        assert report.run_checked == 1

    def test_a_run_id_under_two_of_the_plugins_sessions_is_reported(self, tmp_path: Path) -> None:
        """Which account's run the capture came from cannot be told, so the check
        reports a run it could not read, naming the run id and no account."""
        observe = tmp_path / "observe"
        _write_run(observe, "myshop", _RUN.run, "SomeOtherValue123")
        _write_run(observe, "myshop-alice", _RUN.run, _RECORDED_SESSION)
        tree = tmp_path / "fixtures"
        _fixture(tree, "get_orders.json", _PASTED, _from_run(_PASTED))
        report = check_fixtures_tree(tree, observe_dir=observe)
        (problem,) = report.problems
        assert problem.startswith("get_orders.json: the observe run 20261006-101500-4242 of myshop")
        assert "2 of the plugin's session directories hold a run 20261006-101500-4242" in problem
        assert "alice" not in problem
        assert _RECORDED_SESSION not in problem
        assert report.run_checked == 0

    @pytest.mark.skipif(os.geteuid() == 0, reason="root lists a directory whatever its mode")
    def test_an_observe_directory_that_cannot_be_listed_is_reported(self, tmp_path: Path) -> None:
        observe = tmp_path / "observe"
        _write_run(observe, "myshop", _RUN.run, _RECORDED_SESSION)
        tree = tmp_path / "fixtures"
        _fixture(tree, "get_orders.json", _PASTED, _from_run(_PASTED))
        observe.chmod(0)
        try:
            (problem,) = check_fixtures_tree(tree, observe_dir=observe).problems
        finally:
            observe.chmod(0o700)
        assert problem.startswith(
            "get_orders.json: the lookup of the observe run 20261006-101500-4242 of myshop "
            "its sidecar names could not be completed (the observe directory cannot be listed"
        )
        assert "is on this machine but" not in problem
        assert "move its directory" not in problem
        assert str(tmp_path) not in problem

    def test_a_sidecar_naming_no_run_is_not_compared(self, tmp_path: Path) -> None:
        observe = tmp_path / "observe"
        _write_run(observe, "myshop", _RUN.run, _RECORDED_SESSION)
        tree = tmp_path / "fixtures"
        _fixture(tree, "get_orders.json", _PASTED, _from_run(_PASTED, None))
        report = check_fixtures_tree(tree, observe_dir=observe)
        assert report.problems == ()
        assert report.run_checked == 0

    @pytest.mark.parametrize(
        ("har", "reason"),
        [
            (None, "it holds no network.har"),
            ("{not json", "network.har is not JSON"),
            ('{"log": {}}', "network.har is not a HAR"),
        ],
        ids=["missing", "not-json", "not-a-har"],
    )
    def test_a_run_that_cannot_be_read_is_reported_once_and_names_still_checked(
        self, tmp_path: Path, har: str | None, reason: str
    ) -> None:
        observe = tmp_path / "observe"
        run_dir = observe / "myshop" / _RUN.run
        run_dir.mkdir(parents=True)
        if har is not None:
            (run_dir / "network.har").write_text(har)
        tree = tmp_path / "fixtures"
        _fixture(tree, "get_orders.json", _PASTED, _from_run(_PASTED))
        named = b'{"csrftoken": "invented"}'
        sidecar = Sidecar(
            status=200,
            content_type="application/json",
            capture_sha256=hashlib.sha256(b"captured").hexdigest(),
            flagged_reasons={"csrftoken": ("token_name",)},
            checked_names=("csrftoken",),
            observe_run=_RUN,
        )
        _fixture(tree, "get_token.json", named, sidecar)
        report = check_fixtures_tree(tree, observe_dir=observe)
        name_problem, run_problem = report.problems
        assert name_problem.startswith("get_token.json: contains the flagged name 'csrftoken'")
        assert run_problem.startswith(
            "get_orders.json and 1 more: the observe run 20261006-101500-4242 of myshop"
        )
        assert f"cannot be read ({reason}" in run_problem
        assert "was not compared" in run_problem
        assert report.run_checked == 0

    def test_a_run_that_is_a_file_is_reported(self, tmp_path: Path) -> None:
        observe = tmp_path / "observe"
        (observe / "myshop").mkdir(parents=True)
        (observe / "myshop" / _RUN.run).write_text("")
        tree = tmp_path / "fixtures"
        _fixture(tree, "get_orders.json", _PASTED, _from_run(_PASTED))
        (problem,) = check_fixtures_tree(tree, observe_dir=observe).problems
        assert "cannot be read (not a directory)" in problem

    @pytest.mark.skipif(os.geteuid() == 0, reason="root reads a directory whatever its mode")
    def test_a_run_whose_directory_cannot_be_searched_is_reported(self, tmp_path: Path) -> None:
        observe = tmp_path / "observe"
        run_dir = _write_run(observe, "myshop", _RUN.run, _RECORDED_SESSION)
        tree = tmp_path / "fixtures"
        _fixture(tree, "get_orders.json", _PASTED, _from_run(_PASTED))
        run_dir.chmod(0)
        try:
            (problem,) = check_fixtures_tree(tree, observe_dir=observe).problems
        finally:
            run_dir.chmod(0o700)
        assert "20261006-101500-4242 of myshop" in problem
        assert "cannot be read" in problem
        assert _RECORDED_SESSION not in problem

    def test_only_the_runs_own_files_are_read(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The check opens files under the named run's directory only, and lists
        only the observe directory itself (the names of its entries), never a
        directory inside it."""
        observe = tmp_path / "observe"
        run_dir = _write_run(observe, "myshop", _RUN.run, _RECORDED_SESSION)
        _write_run(observe, "myshop", "20261007-090000-1111", "OtherRunValue1234")
        tree = tmp_path / "fixtures"
        _fixture(tree, "get_orders.json", _PASTED, _from_run(_PASTED))
        opened: list[Path] = []
        listed: list[Path] = []
        real_read_text = Path.read_text
        real_iterdir = Path.iterdir
        real_scandir = os.scandir
        real_listdir = os.listdir

        def read_text(self: Path, *args: object, **kwargs: object) -> str:
            opened.append(self)
            return real_read_text(self, *args, **kwargs)  # type: ignore[arg-type]

        def iterdir(self: Path):  # noqa: ANN202
            listed.append(self)
            return real_iterdir(self)

        def scandir(path: object = ".") -> object:
            listed.append(Path(str(path)))
            return real_scandir(path)  # type: ignore[arg-type]

        monkeypatch.setattr(Path, "read_text", read_text)
        monkeypatch.setattr(Path, "iterdir", iterdir)

        def listdir(path: object = ".") -> list[str]:
            listed.append(Path(str(path)))
            return real_listdir(path)  # type: ignore[arg-type]

        monkeypatch.setattr(os, "scandir", scandir)
        monkeypatch.setattr(os, "listdir", listdir)
        check_fixtures_tree(tree, observe_dir=observe)
        under_observe = [p for p in opened if p.is_relative_to(observe)]
        assert under_observe
        assert all(p.resolve().is_relative_to(run_dir.resolve()) for p in under_observe)
        assert [p for p in listed if p.is_relative_to(observe)] == [observe]

    @pytest.mark.parametrize("loop", ["self", "mutual"])
    def test_a_symlink_loop_in_a_run_is_a_run_that_cannot_be_read(
        self, tmp_path: Path, loop: str
    ) -> None:
        """Before Python 3.13 ``Path.resolve`` raises RuntimeError on a loop, and
        from 3.13 the read raises ELOOP; either way the check reports the file by
        its path in the run, and prints neither the account label nor a path
        outside the run."""
        observe = tmp_path / "observe"
        run_dir = _write_run(observe, "myshop-alice", _RUN.run, _RECORDED_SESSION)
        if loop == "self":
            (run_dir / "page-source.html").symlink_to("page-source.html")
            named = "page-source.html"
        else:
            har_path = run_dir / "network.har"
            har = json.loads(har_path.read_text())
            har["log"]["entries"][0]["response"]["headers"] = [
                {"name": "Content-Type", "value": "text/html"}
            ]
            har["log"]["entries"][0]["response"]["content"] = {
                "mimeType": "text/html",
                "_bodyFile": "bodies/a.html",
            }
            har_path.write_text(json.dumps(har))
            (run_dir / "bodies").mkdir()
            (run_dir / "bodies" / "a.html").symlink_to("b.html")
            (run_dir / "bodies" / "b.html").symlink_to("a.html")
            named = "bodies/a.html"
        tree = tmp_path / "fixtures"
        _fixture(tree, "get_orders.json", _PASTED, _from_run(_PASTED))
        report = check_fixtures_tree(tree, observe_dir=observe)
        (problem,) = report.problems
        assert problem.startswith("get_orders.json: the observe run 20261006-101500-4242 of myshop")
        assert f"cannot read {named} (a symlink loop)" in problem
        assert "alice" not in problem
        assert str(tmp_path) not in problem
        assert report.run_checked == 0

    @pytest.mark.skipif(os.geteuid() == 0, reason="root reads a file whatever its mode")
    def test_a_run_file_that_cannot_be_read_fails_the_local_check(self, tmp_path: Path) -> None:
        """A page source the check needs and cannot read is a run it could not
        read, never a quiet pass on the values it did read."""
        observe = tmp_path / "observe"
        run_dir = _write_run(observe, "myshop-alice", _RUN.run, _RECORDED_SESSION)
        page = run_dir / "page-source.html"
        page.write_text('<meta name="myshop_session" content="PageOnlyValue123">')
        page.chmod(0)
        tree = tmp_path / "fixtures"
        body = b'{"id": 7, "myshop_session": "PageOnlyValue123"}'
        _fixture(tree, "get_orders.json", body, _from_run(body))
        try:
            report = check_fixtures_tree(tree, observe_dir=observe)
        finally:
            page.chmod(0o600)
        (problem,) = report.problems
        assert "cannot read page-source.html (Permission denied)" in problem
        assert "alice" not in problem
        assert str(tmp_path) not in problem
        assert report.run_checked == 0

    def test_a_body_file_the_har_names_that_is_gone_fails_the_local_check(
        self, tmp_path: Path
    ) -> None:
        """gp names a body file in the HAR only after writing it, so one that is
        gone means the run was changed since: the values only it held cannot be
        compared, and the run is not counted as compared."""
        observe = tmp_path / "observe"
        run_dir = _write_run(observe, "myshop-alice", _RUN.run, _RECORDED_SESSION)
        har = json.loads((run_dir / "network.har").read_text())
        response = har["log"]["entries"][0]["response"]
        response["headers"] = [{"name": "Content-Type", "value": "text/html"}]
        content = response["content"]
        del content["text"]
        content["_bodyFile"] = "bodies/1.html"
        (run_dir / "network.har").write_text(json.dumps(har))
        tree = tmp_path / "fixtures"
        _fixture(tree, "get_orders.json", _PASTED, _from_run(_PASTED))
        report = check_fixtures_tree(tree, observe_dir=observe)
        (problem,) = report.problems
        assert "cannot be read (cannot read bodies/1.html (missing))" in problem
        assert "alice" not in problem
        assert str(tmp_path) not in problem
        assert report.run_checked == 0

    @pytest.mark.skipif(os.geteuid() == 0, reason="root searches a directory whatever its mode")
    @pytest.mark.parametrize("with_the_run", [True, False])
    def test_a_session_directory_that_cannot_be_searched_is_not_a_second_copy(
        self, tmp_path: Path, with_the_run: bool
    ) -> None:
        observe = tmp_path / "observe"
        if with_the_run:
            _write_run(observe, "myshop-alice", _RUN.run, _RECORDED_SESSION)
        bob = observe / "myshop-bob"
        bob.mkdir(parents=True)
        bob.chmod(0)
        tree = tmp_path / "fixtures"
        _fixture(tree, "get_orders.json", _PASTED, _from_run(_PASTED))
        try:
            report = check_fixtures_tree(tree, observe_dir=observe)
        finally:
            bob.chmod(0o700)
        (problem,) = report.problems
        assert problem == (
            "get_orders.json: the lookup of the observe run 20261006-101500-4242 of myshop "
            "its sidecar names could not be completed (1 of the plugin's session "
            "directories cannot be searched (Permission denied), so the check cannot tell "
            "whether it holds the run), so the fixture was not compared with the values "
            "the run may have recorded. Make the observe directory "
            "(graftpunk.observe.OBSERVE_BASE_DIR) listable and the plugin's session "
            "directories in it searchable, so the check can tell whether the run is on "
            "this machine."
        )
        assert report.run_checked == 0
        assert "alice" not in problem
        assert "bob" not in problem
        assert str(tmp_path) not in problem

    @pytest.mark.parametrize("case", ["missing-har", "ambiguous", "leak"])
    def test_no_problem_names_the_account_or_the_observe_path(
        self, tmp_path: Path, case: str
    ) -> None:
        observe = tmp_path / "observe"
        run_dir = _write_run(observe, "myshop-alice", _RUN.run, _RECORDED_SESSION)
        if case == "missing-har":
            (run_dir / "network.har").unlink()
        elif case == "ambiguous":
            _write_run(observe, "myshop-alicia", _RUN.run, _RECORDED_SESSION)
        tree = tmp_path / "fixtures"
        _fixture(tree, "get_orders.json", _PASTED, _from_run(_PASTED))
        report = check_fixtures_tree(tree, observe_dir=observe)
        assert report.problems
        for text in (*report.problems, report.summary):
            assert "alice" not in text
            assert str(tmp_path) not in text
            assert str(observe) not in text


def _containment_run(run_dir: Path, outside: Path) -> dict:
    """A run whose HAR names four files outside it, each holding a recorded value:
    a climbing ``_bodyFile``, an absolute ``_postDataFile``, a body that is a
    symlink out of the run, and a ``page-source.html`` that is one."""
    (outside / "climb.html").write_text('<meta name="csrf-token" content="ClimbedOut123">')
    (outside / "absolute.txt").write_text("csrf-token=AbsolutePath123")
    (outside / "linked.html").write_text('<meta name="csrf-token" content="LinkedOut123">')
    (outside / "page.html").write_text('<meta name="csrf-token" content="PageOut123">')
    (run_dir / "bodies").mkdir(parents=True)
    (run_dir / "bodies" / "linked.html").symlink_to(outside / "linked.html")
    (run_dir / "page-source.html").symlink_to(outside / "page.html")
    html = [{"name": "Content-Type", "value": "text/html"}]

    def entry(content: dict, request: dict) -> dict:
        return {
            "request": {"url": "https://myshop.example.com/", "headers": [], **request},
            "response": {"headers": html, "content": {"mimeType": "text/html", **content}},
        }

    climb = f"bodies/../../{outside.name}/climb.html"
    return {
        "log": {
            "entries": [
                entry({"_bodyFile": climb}, {}),
                entry({}, {"_postDataFile": str(outside / "absolute.txt")}),
                entry({"_bodyFile": "bodies/linked.html"}, {}),
            ]
        }
    }


def test_the_package_reads_no_file_outside_the_run(tmp_path: Path) -> None:
    """``graftpunk.testing.recorded`` refuses a HAR's file reference that climbs
    out of the run, one that is absolute, a body symlinked out of the run, and a
    symlinked ``page-source.html``: none is read and none is an error."""
    run_dir = tmp_path / "run"
    outside = tmp_path / "outside"
    outside.mkdir()
    har = _containment_run(run_dir, outside)
    assert recorded_values(har, {"csrf-token"}, run_dir) == {}


_LOCAL_CHECK_CONFTEST = """
from pathlib import Path

from graftpunk.testing.plugin import fixtures_are_sanitised

FIXTURES_TREE = Path(__file__).parent / "fixtures"

sanitised_fixtures = fixtures_are_sanitised(
    FIXTURES_TREE, observe_dir=Path(__file__).parent / "observe"
)
"""


class TestTheLocalValueCheckInASuite:
    def test_a_pasted_value_fails_the_suite_and_no_output_holds_it(
        self, pytester: pytest.Pytester
    ) -> None:
        _write_run(pytester.path / "observe", "myshop", _RUN.run, _RECORDED_SESSION)
        _fixture(pytester.path / "fixtures", "get_orders.json", _PASTED, _from_run(_PASTED))
        pytester.makepyfile(conftest=_LOCAL_CHECK_CONFTEST, test_one="def test_one():\n    pass\n")
        result = pytester.runpytest_inprocess(
            "-o", "asyncio_default_fixture_loop_scope=function", "-rA"
        )
        result.assert_outcomes(errors=1)
        result.stdout.fnmatch_lines(
            [
                "*1 compared with the values their observe run recorded*",
                "*get_orders.json: the recorded value of flagged 'myshop_session'*",
            ]
        )
        output = "\n".join(result.outlines + result.errlines)
        assert _RECORDED_SESSION not in output

    def test_the_suite_passes_and_says_so_when_the_run_is_absent(
        self, pytester: pytest.Pytester
    ) -> None:
        _fixture(pytester.path / "fixtures", "get_orders.json", _PASTED, _from_run(_PASTED))
        pytester.makepyfile(conftest=_LOCAL_CHECK_CONFTEST, test_one="def test_one():\n    pass\n")
        result = pytester.runpytest_inprocess("-o", "asyncio_default_fixture_loop_scope=function")
        result.assert_outcomes(passed=1)
        result.stdout.fnmatch_lines(["*0 compared with the values their observe run recorded*"])
        output = "\n".join(result.outlines + result.errlines)
        assert "warning" not in output.lower()


class TestFixturesAreSanitisedInASuite:
    """Driven through a real inner pytest run, given FIXTURES_TREE the way the
    generated conftest supplies it."""

    def test_a_clean_tree_passes_and_the_declared_count_is_reported(
        self, pytester: pytest.Pytester
    ) -> None:
        fixtures = pytester.path / "fixtures"
        fixtures.mkdir()
        (fixtures / "get_orders.json").write_text("{}")
        (fixtures / "get_orders.json.meta.json").write_text(
            sidecar_text(Sidecar(status=200, content_type="application/json"))
        )
        pytester.makepyfile(conftest=_GENERATED_CONFTEST, test_one="def test_one():\n    pass\n")
        result = pytester.runpytest_inprocess("-o", "asyncio_default_fixture_loop_scope=function")
        result.assert_outcomes(passed=1)
        result.stdout.fnmatch_lines(["*0 fixture(s) verified*1 accepted on declaration*"])

    def test_a_violation_fails_the_run_with_its_message(self, pytester: pytest.Pytester) -> None:
        fixtures = pytester.path / "fixtures"
        fixtures.mkdir()
        (fixtures / "get_orders.json").write_text("{}")
        pytester.makepyfile(conftest=_GENERATED_CONFTEST, test_one="def test_one():\n    pass\n")
        result = pytester.runpytest_inprocess("-o", "asyncio_default_fixture_loop_scope=function")
        result.assert_outcomes(errors=1)
        result.stdout.fnmatch_lines(["*get_orders.json: no sidecar*"])


def test_graftpunk_testing_plugin_imports_nothing_from_devtools() -> None:
    script = (
        "import sys\n"
        "import graftpunk.testing.plugin\n"
        "print(sorted(m for m in sys.modules if m.startswith('graftpunk.devtools')))\n"
    )
    result = subprocess.run(  # noqa: S603 - argv is fixed, not untrusted input
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=60, check=True
    )
    assert result.stdout.strip() == "[]"


_PROBE_ONLY_NAMES = frozenset({"exists", "lexists", "stat", "lstat", "samefile", "access"})
_OS_PATH_ALLOWED_CALLS = frozenset(
    {"join", "basename", "dirname", "splitext", "split", "normpath", "relpath", "sep"}
)


def _is_probe_only_attribute(name: str) -> bool:
    """True for an attribute call's name the guard bans outright: any
    ``is_*`` predicate (``is_fifo``, ``is_mount``, ``is_dir``, and any
    future sibling, not a fixed list of the ones a past finding named) or
    one of the exact names in ``_PROBE_ONLY_NAMES``. Scoped to attribute
    calls (``candidate.is_fifo()``): a bare function named ``is_whatever``
    that is not a filesystem predicate (``is_sidecar``, say) is not one of
    these just for sharing the prefix."""
    return name.startswith("is_") or name in _PROBE_ONLY_NAMES


def _is_os_path_attribute(node: ast.expr) -> bool:
    """True when *node* is the attribute access ``os.path``."""
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "path"
        and isinstance(node.value, ast.Name)
        and node.value.id == "os"
    )


def _names_imported_from_os_path(tree: ast.Module) -> set[str]:
    """Every local name a ``from os.path import ...`` statement anywhere in
    *tree* binds (the alias, when it has one), so a bare call to an
    imported ``isfile`` is told apart from an unrelated function of the
    same name."""
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "os.path":
            for alias in node.names:
                imported.add(alias.asname or alias.name)
    return imported


def _calls_outside_stat_kind(tree: ast.Module) -> list[tuple[int, str]]:
    """Every call in *tree* banned outside the one probe ``_stat_kind``
    allows, as (line, name): an attribute call whose name starts with
    ``is_`` or is one of ``_PROBE_ONLY_NAMES``; any ``os.path.<name>(...)``
    call other than the pure string functions in ``_OS_PATH_ALLOWED_CALLS``;
    or a bare call to a name in ``_PROBE_ONLY_NAMES`` or bound by a
    ``from os.path import`` and not in the allowed set. A second filesystem
    presence/kind check sitting outside the one probe ``testing/plugin.py``
    allows."""
    violations: list[tuple[int, str]] = []
    imported = _names_imported_from_os_path(tree)

    class _Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.stack: list[str] = []

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self.stack.append(node.name)
            self.generic_visit(node)
            self.stack.pop()

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            self.stack.append(node.name)
            self.generic_visit(node)
            self.stack.pop()

        def visit_Call(self, node: ast.Call) -> None:
            func = node.func
            name: str | None = None
            banned = False
            if isinstance(func, ast.Attribute):
                name = func.attr
                banned = _is_probe_only_attribute(name) or (
                    _is_os_path_attribute(func.value) and name not in _OS_PATH_ALLOWED_CALLS
                )
            elif isinstance(func, ast.Name):
                name = func.id
                banned = name in _PROBE_ONLY_NAMES or (
                    name in imported and name not in _OS_PATH_ALLOWED_CALLS
                )
            if banned and (not self.stack or self.stack[-1] != "_stat_kind"):
                violations.append((node.lineno, name or ""))
            self.generic_visit(node)

    _Visitor().visit(tree)
    return violations


def test_only_stat_kind_calls_exists_is_dir_is_file_is_symlink_lstat_or_stat() -> None:
    """``_stat_kind`` is the one place in this module allowed to ask the
    filesystem what is at a path; a later edit that adds a second
    ``.exists()``, ``.is_dir()``, ``.is_file()``, ``.is_symlink()``,
    ``.lstat()``, or ``.stat()`` call anywhere else reintroduces a
    version-dependent pathlib predicate (before Python 3.14, ``is_dir()``
    and ``is_file()`` raise on a permission error; from 3.14 they answer
    ``False``)."""
    source = Path(plugin_module.__file__).read_text()
    violations = _calls_outside_stat_kind(ast.parse(source))
    assert violations == []


def test_the_stat_kind_guard_bans_every_is_underscore_predicate_and_an_os_path_import() -> None:
    """The guard does not chase names one at a time: any ``is_*`` attribute
    call (``is_fifo``, not just the ones already hit in review), any
    ``os.path.<name>(...)`` call, and a name bound by ``from os.path import``
    and then called bare are all banned outside ``_stat_kind``, not only the
    exact names a past finding happened to name."""
    source = textwrap.dedent(
        """
        from os.path import isfile

        def check(candidate, p):
            candidate.is_fifo()
            os.path.ismount(p)
            isfile(p)
        """
    )
    violations = _calls_outside_stat_kind(ast.parse(source))
    assert {name for _, name in violations} == {"is_fifo", "ismount", "isfile"}


def test_the_stat_kind_guard_allows_the_pure_string_os_path_functions() -> None:
    """``os.path.join`` and its siblings never ask the filesystem anything;
    the guard's ``os.path`` ban exempts them by name, outside the probe."""
    source = textwrap.dedent(
        """
        def build(a, b):
            os.path.join(a, b)
            os.path.basename(a)
            os.path.dirname(a)
            os.path.splitext(a)
            os.path.split(a)
            os.path.normpath(a)
            os.path.relpath(a, b)
        """
    )
    assert _calls_outside_stat_kind(ast.parse(source)) == []


def test_the_stat_kind_guard_still_allows_the_probe_itself() -> None:
    """Every banned call is still allowed inside ``_stat_kind``, the one
    function the guard exempts."""
    source = textwrap.dedent(
        """
        def _stat_kind(path):
            path.stat()
            path.lstat()
            os.path.ismount(path)
            path.is_fifo()
        """
    )
    assert _calls_outside_stat_kind(ast.parse(source)) == []
