# tests/unit/test_plugin_runtime_session.py
"""The plugin-command path computes one operating name for load AND store (#151)."""

from __future__ import annotations

from unittest.mock import patch

import requests

from graftpunk.exceptions import SessionNotFoundError
from tests.unit.cli_harness import invoke_plugin_app as _invoke


def _plugin():  # noqa: ANN202
    from graftpunk.plugins.cli_plugin import SitePlugin, command

    class ShopPlugin(SitePlugin):
        site_name = "fmtsite"
        base_url = "https://fmt.example.com"
        session_name = "myshop"
        requires_session = True
        saves_session = True

        @command(help="List items", saves_session=True)
        def items(self, ctx):  # noqa: ANN001, ANN202
            ctx._session_dirty = True
            return {"ok": True}

    return ShopPlugin()


class TestOperatingNameOnPluginPath:
    @patch("graftpunk.cli.plugin_runtime.update_session_cookies")
    @patch("graftpunk.cli.plugin_runtime.load_session_for_api")
    @patch("graftpunk.cli.plugin_runtime.list_sessions", return_value=["myshop@alice"])
    def test_resolved_name_used_for_load_and_store(self, _ls, mock_load, mock_update) -> None:  # noqa: ANN001
        mock_load.return_value = requests.Session()
        result = _invoke(_plugin(), ["fmtsite", "items"])
        assert result.exit_code == 0, result.output
        assert mock_load.call_args[0][0] == "myshop@alice"
        assert mock_update.call_args[0][1] == "myshop@alice"

    @patch("graftpunk.cli.plugin_runtime.load_session_for_api")
    @patch(
        "graftpunk.cli.plugin_runtime.list_sessions",
        return_value=["myshop@alice", "myshop@bob"],
    )
    def test_session_flag_pins(self, _ls, mock_load) -> None:  # noqa: ANN001
        mock_load.return_value = requests.Session()
        result = _invoke(_plugin(), ["fmtsite", "items", "--session", "myshop@bob"])
        assert result.exit_code == 0, result.output
        assert mock_load.call_args[0][0] == "myshop@bob"

    @patch("graftpunk.cli.plugin_runtime.load_session_for_api")
    @patch(
        "graftpunk.cli.plugin_runtime.list_sessions",
        return_value=["myshop@alice", "myshop@bob"],
    )
    def test_ambiguous_refuses_with_candidates(self, _ls, mock_load) -> None:  # noqa: ANN001
        mock_load.return_value = requests.Session()
        result = _invoke(_plugin(), ["fmtsite", "items"])
        assert result.exit_code != 0
        assert "myshop@alice" in result.output and "GRAFTPUNK_SESSION" in result.output

    @patch("graftpunk.cli.plugin_runtime.load_session_for_api")
    @patch("graftpunk.cli.plugin_runtime.list_sessions", return_value=["myshop@alice"])
    def test_env_var_pins(self, _ls, mock_load) -> None:  # noqa: ANN001
        mock_load.return_value = requests.Session()
        result = _invoke(_plugin(), ["fmtsite", "items"], GRAFTPUNK_SESSION="myshop@alice")
        assert result.exit_code == 0, result.output
        assert mock_load.call_args[0][0] == "myshop@alice"

    @patch("graftpunk.cli.plugin_runtime.load_session_for_api")
    @patch("graftpunk.cli.plugin_runtime.list_sessions", return_value=["myshop@alice"])
    def test_foreign_base_env_var_does_not_steer(self, _ls, mock_load) -> None:  # noqa: ANN001
        """A GRAFTPUNK_SESSION for a different base is ignored (#151); the
        plugin resolves normally against its own cached sessions."""
        mock_load.return_value = requests.Session()
        result = _invoke(
            _plugin(), ["fmtsite", "items"], GRAFTPUNK_SESSION="othersite@someone-else"
        )
        assert result.exit_code == 0, result.output
        assert mock_load.call_args[0][0] == "myshop@alice"

    @patch("graftpunk.cli.plugin_runtime.load_session_for_api")
    @patch("graftpunk.cli.plugin_runtime.list_sessions", return_value=[])
    def test_not_found_names_the_operating_session(self, _ls, mock_load) -> None:  # noqa: ANN001
        """The not-found error names the flagged/computed operating session
        (myshop@wronglabel), not the plugin's bare base name (#151)."""
        mock_load.side_effect = SessionNotFoundError("Session not found")
        result = _invoke(_plugin(), ["fmtsite", "items", "--session", "myshop@wronglabel"])
        assert result.exit_code != 0
        assert "myshop@wronglabel" in result.output
