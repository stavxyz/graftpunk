"""Two accounts on one plugin coexist; nothing resolves silently (#151)."""

from __future__ import annotations

import pytest
import requests

from graftpunk.cache import cache_session


@pytest.fixture(autouse=True)
def _no_ambient_session(monkeypatch, tmp_path):  # noqa: ANN001, ANN201
    """No ambient pin from the developer's shell or a stray .gp-session.

    The chdir matters as much as the env var: get_active_session() reads a
    .gp-session file in the CWD, which is the repo root under pytest.
    """
    monkeypatch.delenv("GRAFTPUNK_SESSION", raising=False)
    monkeypatch.chdir(tmp_path)


def _cache(name: str, identifier: str) -> None:
    from graftpunk.session_identity import GP_ACCOUNT_ATTR

    s = requests.Session()
    setattr(s, GP_ACCOUNT_ATTR, identifier)
    cache_session(s, name)


def _shop_plugin(cookie: str = "visited"):  # noqa: ANN202
    """A plugin whose one command dirties the session, so the write-back is real.

    *cookie* names the cookie the handler sets: distinct per invocation, so a
    second run's write-back is visible in the stored cookie count rather than
    overwriting the first one's.
    """
    from graftpunk.plugins.cli_plugin import SitePlugin, command

    class ShopPlugin(SitePlugin):
        site_name = "fmtsite"
        base_url = "https://fmt.example.com"
        session_name = "myshop"
        requires_session = True

        @command(help="List items", saves_session=True)
        def items(self, ctx):  # noqa: ANN001, ANN202
            # Dirty the session so the command performs a REAL write-back on
            # the real backend: the operating name has to key that write, or a
            # bare "myshop" slot appears beside the labelled ones.
            ctx.session.cookies.set(cookie, "1")
            ctx.save_session()
            return {"ok": True}

    return ShopPlugin()


def test_two_accounts_coexist_and_commands_require_pinning(
    fresh_backend,
    monkeypatch,  # noqa: ANN001
    tmp_path,  # noqa: ANN001
) -> None:
    """The spec's e2e, through the CLI surface on the real local backend."""
    from graftpunk.cache import get_session_metadata, list_sessions
    from tests.unit.cli_harness import invoke_plugin_app

    _cache("myshop@alice", "alice@example.com")
    _cache("myshop@bob", "bob@example.com")

    # Stored metadata answers "whose session is this?" without a network call.
    assert get_session_metadata("myshop@alice")["account_identifier"] == "alice@example.com"
    assert get_session_metadata("myshop@bob")["account_identifier"] == "bob@example.com"

    # Unpinned: the command refuses and names both candidates.
    result = invoke_plugin_app(_shop_plugin(), ["fmtsite", "items"])
    assert result.exit_code != 0
    assert "myshop@alice" in result.output and "myshop@bob" in result.output

    # Pinning each way works (a fresh app per invocation; re-registering the
    # same plugin is idempotent for the session map).
    ok_flag = invoke_plugin_app(_shop_plugin(), ["fmtsite", "items", "--session", "myshop@bob"])
    assert ok_flag.exit_code == 0, ok_flag.output
    ok_env = invoke_plugin_app(
        _shop_plugin(), ["fmtsite", "items"], GRAFTPUNK_SESSION="myshop@alice"
    )
    assert ok_env.exit_code == 0, ok_env.output

    names = set(list_sessions())
    assert {"myshop@alice", "myshop@bob"} <= names
    # The write-backs landed on the accounts they were loaded from: no bare
    # slot was forked open by a save that forgot the operating name.
    assert "myshop" not in names

    # The write-backs really wrote: each account's cookie count and identifier
    # moved together, on its own slot.
    for name, identifier in (
        ("myshop@alice", "alice@example.com"),
        ("myshop@bob", "bob@example.com"),
    ):
        meta = get_session_metadata(name)
        assert meta["cookie_count"] == 1, name
        assert meta["account_identifier"] == identifier


def test_bare_pin_resolves_and_the_refresh_follows_the_resolved_slot(
    fresh_backend,
    monkeypatch,  # noqa: ANN001
    tmp_path,  # noqa: ANN001
) -> None:
    """A bare pin names a base (#182), and the write-back keys off what loaded.

    Only ``myshop@alice`` is cached, so both bare pins resolve to it at load
    time. The refresh must land on that slot: keying it off the bare name the
    user typed loses the write silently (nothing is cached under "myshop", so
    update_session_cookies logs a skip and returns).
    """
    from graftpunk.cache import get_session_metadata, list_sessions
    from tests.unit.cli_harness import invoke_plugin_app

    _cache("myshop@alice", "alice@example.com")

    flag = invoke_plugin_app(
        _shop_plugin("visited-flag"), ["fmtsite", "items", "--session", "myshop"]
    )
    assert flag.exit_code == 0, flag.output
    assert get_session_metadata("myshop@alice")["cookie_count"] == 1

    env = invoke_plugin_app(
        _shop_plugin("visited-env"), ["fmtsite", "items"], GRAFTPUNK_SESSION="myshop"
    )
    assert env.exit_code == 0, env.output

    stored = get_session_metadata("myshop@alice")
    assert stored["cookie_count"] == 2  # both invocations' refreshes landed
    assert stored["account_identifier"] == "alice@example.com"
    assert "myshop" not in list_sessions()


def _register_myshop_site_plugin() -> None:
    """Register a plugin whose site_name and session_name are both "myshop" (#186).

    Populates the module-level ``_plugin_session_map`` that
    ``resolve_session_name`` reads. No commands are needed: these tests
    exercise ``gp session ...``, not a plugin subcommand.
    """
    from unittest.mock import patch

    import typer

    from graftpunk.cli.plugin_commands import register_plugin_commands
    from graftpunk.plugins.cli_plugin import SitePlugin

    class MyShopPlugin(SitePlugin):
        site_name = "myshop"
        session_name = "myshop"

    with patch(
        "graftpunk.cli.plugin_commands.discover_all_plugins", return_value=(MyShopPlugin(),)
    ):
        register_plugin_commands(typer.Typer(), notify_errors=False)


class TestSessionCommandsPreferTheExactCachedBareSlot:
    """``gp session clear/show <base>`` operate on an exact cached slot (#186).

    Before the fix, a registered site name was always routed through account
    resolution, so a legacy bare slot next to a labelled account raised
    AmbiguousSessionError and the documented ``gp session clear <base>``
    remedy removed nothing.
    """

    def test_clear_bare_base_removes_only_the_bare_slot(self, fresh_backend) -> None:  # noqa: ANN001
        from typer.testing import CliRunner

        from graftpunk.cache import list_sessions
        from graftpunk.cli.session_commands import session_app

        _cache("myshop", "legacy@example.com")
        _cache("myshop@alice", "alice@example.com")
        _register_myshop_site_plugin()

        result = CliRunner().invoke(session_app, ["clear", "myshop", "--force"])

        assert result.exit_code == 0, result.output
        names = set(list_sessions())
        assert "myshop" not in names
        assert "myshop@alice" in names

    def test_show_bare_base_shows_the_bare_slot(self, fresh_backend) -> None:  # noqa: ANN001
        from typer.testing import CliRunner

        from graftpunk.cli.session_commands import session_app

        _cache("myshop", "legacy@example.com")
        _cache("myshop@alice", "alice@example.com")
        _register_myshop_site_plugin()

        runner = CliRunner()
        bare = runner.invoke(session_app, ["show", "myshop", "--json"])
        assert bare.exit_code == 0, bare.output
        assert '"account_identifier": "legacy@example.com"' in bare.output

        labelled = runner.invoke(session_app, ["show", "myshop@alice", "--json"])
        assert labelled.exit_code == 0, labelled.output
        assert '"account_identifier": "alice@example.com"' in labelled.output

    def test_show_still_resolves_when_only_the_labelled_account_is_cached(
        self,
        fresh_backend,  # noqa: ANN001
    ) -> None:
        from typer.testing import CliRunner

        from graftpunk.cli.session_commands import session_app

        _cache("myshop@alice", "alice@example.com")
        _register_myshop_site_plugin()

        result = CliRunner().invoke(session_app, ["show", "myshop", "--json"])
        assert result.exit_code == 0, result.output
        assert '"account_identifier": "alice@example.com"' in result.output

    def test_show_still_raises_ambiguity_with_no_bare_slot(self, fresh_backend) -> None:  # noqa: ANN001
        from typer.testing import CliRunner

        from graftpunk.cli.session_commands import session_app

        _cache("myshop@alice", "alice@example.com")
        _cache("myshop@bob", "bob@example.com")
        _register_myshop_site_plugin()

        result = CliRunner().invoke(session_app, ["show", "myshop"])
        assert result.exit_code == 1
        assert "myshop@alice" in result.output and "myshop@bob" in result.output
