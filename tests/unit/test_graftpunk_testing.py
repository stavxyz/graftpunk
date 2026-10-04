"""graftpunk.testing: make_context, FixtureSession, fixture_context, site_env_scrubber
(plugin tooling spec, 2026-09-11)."""

from __future__ import annotations

import ast
import errno
import hashlib
import json
import os
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
from graftpunk.testing.sidecar import FIXTURES_PLACEHOLDER, Sidecar, SidecarError, sidecar_text


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
        flagged_names=flagged,
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
        assert problem.endswith("rename it in the fixture.")

    def test_a_flagged_name_in_the_sidecar_fails(self, tmp_path: Path) -> None:
        sidecar = Sidecar(
            status=200,
            content_type="application/vnd.myshop_session+json",
            capture_sha256=hashlib.sha256(b"captured").hexdigest(),
            flagged_names=("myshop_session",),
        )
        _fixture(tmp_path, "get_orders.json", b'{"orders": []}', sidecar)
        (problem,) = check_fixtures_tree(tmp_path).problems
        assert problem.startswith("get_orders.json.meta.json")

    def test_a_flagged_name_in_body_params_alone_passes(self, tmp_path: Path) -> None:
        """body_params holds request field names, which can legitimately include
        a flagged CSRF field's name; sidecar_scannable_text exempts body_params,
        so this is not a leak."""
        sidecar = Sidecar(
            status=200,
            content_type="application/json",
            body_params=("authenticity_token",),
            capture_sha256=hashlib.sha256(b"captured").hexdigest(),
            flagged_names=("authenticity_token",),
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

    def test_a_symlink_cycle_under_the_tree_terminates_and_checks_once(
        self, tmp_path: Path
    ) -> None:
        """Two symlinks pointing at the same real directory do not each get
        walked (which would report its fixture's missing sidecar twice);
        a stat-identity set lets only the first one through."""
        real = tmp_path / "real"
        real.mkdir()
        _fixture(real, "get_orders.json", b"{}", None)
        (tmp_path / "link_one").symlink_to(real)
        (tmp_path / "link_two").symlink_to(real)
        report = check_fixtures_tree(tmp_path)
        no_sidecar = [p for p in report.problems if "get_orders.json: no sidecar" in p]
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
        """An empty string in flagged_names would match every fixture as a substring."""
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

    def test_a_flagged_name_is_matched_case_insensitively_in_the_sidecar(
        self, tmp_path: Path
    ) -> None:
        sidecar = Sidecar(
            status=200,
            content_type="application/vnd.x-csrf-token+json",
            capture_sha256=hashlib.sha256(b"captured").hexdigest(),
            flagged_names=("X-Csrf-Token",),
        )
        _fixture(tmp_path, "get_orders.json", b'{"orders": []}', sidecar)
        (problem,) = check_fixtures_tree(tmp_path).problems
        assert problem.startswith("get_orders.json.meta.json")


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


_PROBE_ONLY_ATTRIBUTES = frozenset(
    {
        "exists",
        "is_dir",
        "is_file",
        "is_symlink",
        "lstat",
        "stat",
        "isdir",
        "isfile",
        "islink",
        "lexists",
        "is_junction",
        "access",
    }
)


def _calls_outside_stat_kind(tree: ast.Module) -> list[tuple[int, str]]:
    """Every call in *tree* to one of ``_PROBE_ONLY_ATTRIBUTES``, as (line, name),
    whose nearest enclosing function is not named ``_stat_kind``: a second
    filesystem presence/kind check sitting outside the one probe
    ``testing/plugin.py`` allows."""
    violations: list[tuple[int, str]] = []

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
            is_probe_only_call = (
                isinstance(node.func, ast.Attribute) and node.func.attr in _PROBE_ONLY_ATTRIBUTES
            )
            if is_probe_only_call and (not self.stack or self.stack[-1] != "_stat_kind"):
                violations.append((node.lineno, node.func.attr))
            self.generic_visit(node)

    _Visitor().visit(tree)
    return violations


def test_only_stat_kind_calls_exists_is_dir_is_file_is_symlink_lstat_or_stat() -> None:
    """``_stat_kind`` is the one place in this module allowed to ask the
    filesystem what is at a path; a later edit that adds a second
    ``.exists()``, ``.is_dir()``, ``.is_file()``, ``.is_symlink()``,
    ``.lstat()``, or ``.stat()`` call anywhere else reintroduces the
    version-dependent pathlib predicate this round removed, so this test
    fails loudly on it rather than waiting for a reviewer to find it by hand
    again."""
    source = Path(plugin_module.__file__).read_text()
    violations = _calls_outside_stat_kind(ast.parse(source))
    assert violations == []
