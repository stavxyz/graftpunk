"""Two accounts on one plugin coexist; nothing resolves silently (#151)."""

from __future__ import annotations

import requests

from graftpunk.cache import cache_session


def _cache(name: str, identifier: str) -> None:
    from graftpunk.session_identity import GP_ACCOUNT_ATTR

    s = requests.Session()
    setattr(s, GP_ACCOUNT_ATTR, identifier)
    cache_session(s, name)


def test_two_accounts_coexist_and_commands_require_pinning(
    fresh_backend,
    monkeypatch,  # noqa: ANN001
) -> None:
    """The spec's e2e, through the CLI surface on the real local backend."""
    from graftpunk.cache import get_session_metadata, list_sessions
    from graftpunk.plugins.cli_plugin import SitePlugin, command
    from tests.unit.cli_harness import invoke_plugin_app

    monkeypatch.delenv("GRAFTPUNK_SESSION", raising=False)
    _cache("myshop@alice", "alice@example.com")
    _cache("myshop@bob", "bob@example.com")

    # Stored metadata answers "whose session is this?" without a network call.
    assert get_session_metadata("myshop@alice")["account_identifier"] == "alice@example.com"
    assert get_session_metadata("myshop@bob")["account_identifier"] == "bob@example.com"

    class ShopPlugin(SitePlugin):
        site_name = "fmtsite"
        base_url = "https://fmt.example.com"
        session_name = "myshop"
        requires_session = True

        @command(help="List items")
        def items(self, ctx):  # noqa: ANN001, ANN202
            return {"ok": True}

    # Unpinned: the command refuses and names both candidates.
    result = invoke_plugin_app(ShopPlugin(), ["fmtsite", "items"])
    assert result.exit_code != 0
    assert "myshop@alice" in result.output and "myshop@bob" in result.output

    # Pinning each way works (a fresh app per invocation; re-registering the
    # same plugin is idempotent for the session map).
    ok_flag = invoke_plugin_app(ShopPlugin(), ["fmtsite", "items", "--session", "myshop@bob"])
    assert ok_flag.exit_code == 0, ok_flag.output
    ok_env = invoke_plugin_app(ShopPlugin(), ["fmtsite", "items"], GRAFTPUNK_SESSION="myshop@alice")
    assert ok_env.exit_code == 0, ok_env.output

    assert {"myshop@alice", "myshop@bob"} <= set(list_sessions())
