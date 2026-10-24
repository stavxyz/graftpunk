"""The login's verdict is the last thing it prints, and it says what happened (#275).

The reported run printed hundreds of per-asset warnings, a tool truncated the
output in the middle, and the line that survived said only "see the warning
above". These tests run the real ``gp <site> login`` body around the generated
login and read stderr the way a truncated terminal would: from the bottom.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from graftpunk.plugins.cli_plugin import LoginConfig, LoginStep, SitePlugin

pytestmark = pytest.mark.usefixtures("_fast_login_timings")

_LANDING = "https://app.example.com/login?next=%2Fhome"


class _StuckPage:
    """A page that takes the submit and then never shows the success element."""

    url = _LANDING

    def __init__(self, *, failure_text: str = "") -> None:
        self.send = AsyncMock()
        self._content = f"<html><body>{failure_text}</body></html>"

    def __getattr__(self, name: str) -> MagicMock:
        return MagicMock()

    async def select(self, selector: str, timeout: float | None = None) -> Any:
        return AsyncMock() if selector != ".dashboard" else None

    async def query_selector(self, selector: str) -> Any:
        return await self.select(selector)

    async def get_content(self) -> str:
        return self._content

    async def evaluate(self, expression: str, **kwargs: Any) -> Any:
        if expression == "document.readyState":
            return "complete"
        return json.dumps({"found": True, "value": None})


def _plugin(**config: Any) -> SitePlugin:
    class P(SitePlugin):
        site_name = "acme"
        session_name = "acme"
        help_text = "t"
        base_url = "https://app.example.com"
        backend = "nodriver"
        login_config = LoginConfig(
            steps=[LoginStep(fields={"username": "#user"}, submit="#go")],
            url="/login",
            timeout=0.2,
            settle=0.0,
            **config,
        )

    return P()


def _run_login(
    plugin: SitePlugin,
    page: Any,
    monkeypatch: pytest.MonkeyPatch,
    *,
    login: Any = None,
    observe_mode: str = "off",
) -> int:
    from graftpunk.cli.login_commands import make_login_body
    from graftpunk.plugins.login_engine import generate_login_method

    monkeypatch.setenv("ACME_USERNAME", "alice")
    mock_bs = MagicMock()
    instance = MagicMock()
    mock_bs.return_value = instance
    instance.__aenter__ = AsyncMock(return_value=instance)
    instance.__aexit__ = AsyncMock(return_value=False)
    instance.driver = MagicMock()
    instance.driver.get = AsyncMock(return_value=page)
    root = SimpleNamespace(obj={"observe_mode": observe_mode})
    ctx = SimpleNamespace(find_root=lambda: root, command_path="gp acme login")
    body = make_login_body(plugin, login or generate_login_method(plugin), {"username": "#user"})
    with patch("graftpunk.BrowserSession", mock_bs), pytest.raises(SystemExit) as exc_info:
        body(ctx)
    return int(exc_info.value.code or 0)


def _verdict(err: str) -> str:
    """Everything from the last error mark to the end of stderr, as one line.

    The verdict has to carry its own reason: a warning printed above it may be
    hundreds of lines away, or cut out by a truncating tool. Rich wraps a long
    line at the console width, so whitespace is collapsed before matching.
    """
    mark = err.rfind("\u2717")
    assert mark >= 0, err
    return " ".join(err[mark:].split())


class TestVerdictComesLast:
    def test_a_missing_success_signal_is_named_with_the_url_at_the_bottom(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = _run_login(_plugin(success=".dashboard"), _StuckPage(), monkeypatch)

        assert code == 1
        verdict = _verdict(capsys.readouterr().err)
        assert "Login did not complete for acme" in verdict
        assert "success element '.dashboard'" in verdict
        assert _LANDING in verdict

    def test_failure_text_is_named_at_the_bottom(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        page = _StuckPage(failure_text="Invalid password")
        code = _run_login(
            _plugin(success=".dashboard", failure="Invalid password"), page, monkeypatch
        )

        assert code == 1
        verdict = _verdict(capsys.readouterr().err)
        assert "failure text 'Invalid password'" in verdict
        assert _LANDING in verdict

    def test_a_step_that_failed_names_the_page_it_failed_on(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        class NoModal(_StuckPage):
            async def select(self, selector: str, timeout: float | None = None) -> Any:
                return None

        code = _run_login(_plugin(success=".dashboard"), NoModal(), monkeypatch)

        assert code == 1
        verdict = _verdict(capsys.readouterr().err)
        assert "Login failed: Step 1" in verdict
        assert _LANDING in verdict

    def test_an_observed_run_is_not_told_to_re_run_with_observe(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with patch("graftpunk.plugins.login_engine._start_login_capture") as start:
            capture = MagicMock()
            capture.start_capture_async = AsyncMock()
            start.return_value = (capture, None)
            _run_login(
                _plugin(success=".dashboard"), _StuckPage(), monkeypatch, observe_mode="full"
            )

        verdict = _verdict(capsys.readouterr().err)
        assert "success element '.dashboard'" in verdict
        assert "--observe=full" not in verdict

    def test_a_hand_written_login_that_returns_false_gets_no_invented_reason(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        def login(credentials: dict[str, str]) -> bool:
            return False

        code = _run_login(_plugin(), None, monkeypatch, login=login)

        assert code == 1
        verdict = _verdict(capsys.readouterr().err)
        assert "did not reach the expected post-login state" in verdict
        assert "Page:" not in verdict
