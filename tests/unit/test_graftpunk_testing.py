"""graftpunk.testing: make_context, FixtureSession, fixture_context, site_env_scrubber
(plugin tooling spec, 2026-09-11)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
import requests

from graftpunk.graftpunk_session import GraftpunkSession
from graftpunk.plugins.cli_plugin import CommandContext
from graftpunk.testing import FixtureSession, fixture_context, make_context


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
        assert session.get("https://myshop.example.com/orders/1").json() == {"id": 1}
        assert session.get("https://myshop.example.com/orders/999").json() == {"id": 1}

    def test_unmatched_request_returns_404(self, tmp_path: Path) -> None:
        session = FixtureSession(tmp_path)
        response = session.get("https://myshop.example.com/nothing-here")
        assert response.status_code == 404

    def test_sidecar_supplies_status_and_content_type(self, tmp_path: Path) -> None:
        (tmp_path / "get_orders.json").write_text("Forbidden")
        (tmp_path / "get_orders.json.meta.json").write_text(
            json.dumps({"status": 403, "content_type": "text/plain"})
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

    def test_never_opens_a_socket(self, tmp_path: Path) -> None:
        """No fixture file for this path: still answers 404 rather than connecting."""
        session = FixtureSession(tmp_path)
        response = session.get("http://169.254.169.254/nonexistent")
        assert response.status_code == 404


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
            pytest_plugins = ["graftpunk.testing.plugin"]
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
