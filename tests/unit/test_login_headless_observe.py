"""Tests for LoginConfig.headless / ``login --headless`` and ``--observe=full`` on login.

Issue #148 items 3 and 4: declarative login hardcoded ``headless=False`` with no
override, and ``--observe=full`` produced no observe run for ``<plugin> login``.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from graftpunk.exceptions import PluginError
from graftpunk.observe.capture import create_capture_backend as _real_create_capture_backend
from graftpunk.plugins.cli_plugin import LoginConfig, LoginStep, SitePlugin
from graftpunk.plugins.login_engine import generate_login_method

pytestmark = pytest.mark.usefixtures("_fast_login_timings")


@pytest.fixture(autouse=True)
def _mock_capture_backend():
    """Default header capture mock; tests that assert on the factory re-patch it."""
    capture = MagicMock()
    capture.start_capture_async = AsyncMock()
    capture.get_header_roles = MagicMock(return_value={})
    with patch("graftpunk.observe.capture.create_capture_backend", return_value=capture):
        yield capture


def _make_plugin(backend: str, *, headless: bool = False) -> SitePlugin:
    class P(SitePlugin):
        site_name = "hl"
        session_name = "hl-session"
        help_text = "t"
        base_url = "https://example.com"

    P.backend = backend  # type: ignore[assignment]
    P.login_config = LoginConfig(
        steps=[LoginStep(fields={"username": "#u", "password": "#p"}, submit="#s")],
        url="/login",
        failure="Bad login.",
        headless=headless,
    )
    return P()


def _nodriver_session_mock(tab: MagicMock) -> tuple[MagicMock, MagicMock]:
    mock_bs = MagicMock()
    instance = MagicMock()
    mock_bs.return_value = instance
    instance.__aenter__ = AsyncMock(return_value=instance)
    instance.__aexit__ = AsyncMock(return_value=False)
    instance.driver = MagicMock()
    instance.driver.get = AsyncMock(return_value=tab)
    instance.transfer_nodriver_cookies_to_session = AsyncMock()
    return mock_bs, instance


def _nodriver_tab(page: str = "<html>Bad login.</html>") -> MagicMock:
    """Tab with a stateful fake field: send_keys appends, clear_input empties,
    evaluate reads back what the field holds, so _fill_field's verification
    runs for real instead of silently taking the "unverifiable" branch."""
    state = {"value": ""}
    element = AsyncMock()
    element.send_keys = AsyncMock(
        side_effect=lambda v: state.__setitem__("value", state["value"] + v)
    )
    element.clear_input = AsyncMock(side_effect=lambda: state.__setitem__("value", ""))
    tab = MagicMock()
    tab.select = AsyncMock(return_value=element)
    tab.evaluate = AsyncMock(
        side_effect=lambda *a, **k: json.dumps({"found": True, "value": state["value"]})
    )
    tab.get_content = AsyncMock(return_value=page)
    return tab


def _selenium_session_mock() -> tuple[MagicMock, MagicMock]:
    mock_bs = MagicMock()
    instance = MagicMock()
    mock_bs.return_value = instance
    instance.__enter__ = MagicMock(return_value=instance)
    instance.__exit__ = MagicMock(return_value=False)
    instance.driver = MagicMock()
    instance.driver.page_source = "<html>Bad login.</html>"
    instance.capture = None
    return mock_bs, instance


# ---------------------------------------------------------------------------
# LoginConfig.headless
# ---------------------------------------------------------------------------


class TestLoginConfigHeadless:
    def test_defaults_to_headful(self) -> None:
        cfg = LoginConfig(steps=[LoginStep(fields={"u": "#u"})])
        assert cfg.headless is False

    def test_accepts_true(self) -> None:
        cfg = LoginConfig(steps=[LoginStep(fields={"u": "#u"})], headless=True)
        assert cfg.headless is True

    def test_rejects_non_bool(self) -> None:
        with pytest.raises(TypeError, match="headless"):
            LoginConfig(steps=[LoginStep(fields={"u": "#u"})], headless="yes")  # type: ignore[arg-type]


class TestYamlHeadless:
    def _parse(self, tmp_path: Path, extra: str) -> Any:
        from graftpunk.plugins.yaml_loader import parse_yaml_plugin

        yaml_file = tmp_path / "p.yaml"
        yaml_file.write_text(
            f"""
site_name: mysite
base_url: "https://example.com"
login:
  url: "/login"
{extra}
  steps:
    - fields:
        username: "input#email"
      submit: "button"
commands:
  search:
    url: "/api/search"
"""
        )
        config, _, _ = parse_yaml_plugin(yaml_file)
        return config

    def test_headless_true(self, tmp_path: Path) -> None:
        config = self._parse(tmp_path, "  headless: true")
        assert config.login_config.headless is True

    def test_headless_defaults_false(self, tmp_path: Path) -> None:
        config = self._parse(tmp_path, "")
        assert config.login_config.headless is False

    def test_headless_non_bool_is_plugin_error(self, tmp_path: Path) -> None:
        with pytest.raises(PluginError, match="headless"):
            self._parse(tmp_path, '  headless: "yes"')


# ---------------------------------------------------------------------------
# Engine honours headless
# ---------------------------------------------------------------------------


class TestEngineHeadless:
    @pytest.mark.asyncio
    async def test_nodriver_default_is_headful(self) -> None:
        tab = _nodriver_tab()
        mock_bs, _ = _nodriver_session_mock(tab)
        login = generate_login_method(_make_plugin("nodriver"))

        with patch("graftpunk.BrowserSession", mock_bs):
            await login({"username": "u", "password": "p"})

        assert mock_bs.call_args.kwargs["headless"] is False

    @pytest.mark.asyncio
    async def test_nodriver_config_headless(self) -> None:
        tab = _nodriver_tab()
        mock_bs, _ = _nodriver_session_mock(tab)
        login = generate_login_method(_make_plugin("nodriver", headless=True))

        with patch("graftpunk.BrowserSession", mock_bs):
            await login({"username": "u", "password": "p"})

        assert mock_bs.call_args.kwargs["headless"] is True

    @pytest.mark.asyncio
    async def test_nodriver_call_override_beats_config(self) -> None:
        tab = _nodriver_tab()
        mock_bs, _ = _nodriver_session_mock(tab)
        login = generate_login_method(_make_plugin("nodriver", headless=False))

        with patch("graftpunk.BrowserSession", mock_bs):
            await login({"username": "u", "password": "p"}, headless=True)

        assert mock_bs.call_args.kwargs["headless"] is True

    def test_selenium_config_headless(self) -> None:
        mock_bs, _ = _selenium_session_mock()
        login = generate_login_method(_make_plugin("selenium", headless=True))

        with (
            patch("graftpunk.BrowserSession", mock_bs),
            patch("graftpunk.plugins.login_engine.time"),
        ):
            login({"username": "u", "password": "p"})

        assert mock_bs.call_args.kwargs["headless"] is True

    def test_selenium_call_override(self) -> None:
        mock_bs, _ = _selenium_session_mock()
        login = generate_login_method(_make_plugin("selenium", headless=False))

        with (
            patch("graftpunk.BrowserSession", mock_bs),
            patch("graftpunk.plugins.login_engine.time"),
        ):
            login({"username": "u", "password": "p"}, headless=True)

        assert mock_bs.call_args.kwargs["headless"] is True


# ---------------------------------------------------------------------------
# --observe=full covers login
# ---------------------------------------------------------------------------


class TestEngineObserve:
    @pytest.mark.asyncio
    async def test_nodriver_observe_off_creates_no_run(self, tmp_path: Path) -> None:
        tab = _nodriver_tab()
        mock_bs, _ = _nodriver_session_mock(tab)
        login = generate_login_method(_make_plugin("nodriver"))

        with (
            patch("graftpunk.BrowserSession", mock_bs),
            patch("graftpunk.observe.OBSERVE_BASE_DIR", tmp_path),
            patch("graftpunk.observe.run.save_observe_run", new_callable=AsyncMock) as save,
        ):
            await login({"username": "u", "password": "p"})

        save.assert_not_awaited()
        assert not (tmp_path / "hl-session").exists()

    @pytest.mark.asyncio
    async def test_nodriver_observe_full_saves_run_even_on_failed_login(
        self, tmp_path: Path
    ) -> None:
        """A failed login is exactly the run worth capturing."""
        tab = _nodriver_tab("<html>Bad login.</html>")
        mock_bs, _ = _nodriver_session_mock(tab)
        login = generate_login_method(_make_plugin("nodriver"))
        mock_capture = MagicMock()
        mock_capture.start_capture_async = AsyncMock()
        mock_capture.get_header_roles = MagicMock(return_value={})

        with (
            patch("graftpunk.BrowserSession", mock_bs),
            patch("graftpunk.observe.OBSERVE_BASE_DIR", tmp_path),
            patch(
                "graftpunk.observe.capture.create_capture_backend", return_value=mock_capture
            ) as ccb,
            patch("graftpunk.observe.run.save_observe_run", new_callable=AsyncMock) as save,
        ):
            result = await login({"username": "u", "password": "p"}, observe_mode="full")

        assert result is False
        save.assert_awaited_once()
        storage, backend, label = save.await_args.args
        assert backend is mock_capture
        assert label == "login"
        assert storage.run_dir.parent.parent == tmp_path
        assert storage.run_dir.parent.name == "hl-session"
        # Credentials are scrubbed from the HAR, and the run dir is shown to the user.
        assert sorted(save.await_args.kwargs["redact"]) == ["p", "u"]
        assert save.await_args.kwargs["console"] is not None
        # Full capture: bodies go to the run dir, not the header-only capture.
        assert ccb.call_args.kwargs["bodies_dir"] == storage.run_dir / "bodies"

    @pytest.mark.asyncio
    async def test_nodriver_observe_full_saves_run_on_exception(self, tmp_path: Path) -> None:
        tab = _nodriver_tab()
        tab.select = AsyncMock(return_value=None)  # field never found -> PluginError
        mock_bs, _ = _nodriver_session_mock(tab)
        login = generate_login_method(_make_plugin("nodriver"))
        mock_capture = MagicMock()
        mock_capture.start_capture_async = AsyncMock()

        with (
            patch("graftpunk.BrowserSession", mock_bs),
            patch("graftpunk.observe.OBSERVE_BASE_DIR", tmp_path),
            patch("graftpunk.observe.capture.create_capture_backend", return_value=mock_capture),
            patch("graftpunk.observe.run.save_observe_run", new_callable=AsyncMock) as save,
            pytest.raises(PluginError),
        ):
            await login({"username": "u", "password": "p"}, observe_mode="full")

        save.assert_awaited_once()

    def test_selenium_observe_full_uses_session_capture(self, tmp_path: Path) -> None:
        """Selenium: BrowserSession owns observe (perf-log drain is single-consumer),
        so login must pass observe_mode through and reuse the session's capture for
        header roles rather than starting a second one."""
        mock_bs, instance = _selenium_session_mock()
        session_capture = MagicMock()
        session_capture.get_header_roles = MagicMock(return_value={"x": {}})
        instance.capture = session_capture
        login = generate_login_method(_make_plugin("selenium"))

        with (
            patch("graftpunk.BrowserSession", mock_bs),
            patch("graftpunk.plugins.login_engine.time"),
            patch("graftpunk.observe.capture.create_capture_backend") as ccb,
        ):
            login({"username": "u", "password": "p"}, observe_mode="full")

        assert mock_bs.call_args.kwargs["observe_mode"] == "full"
        assert instance.session_name == "hl-session"
        # The observe HAR carries the login POST; the credentials are scrubbed.
        assert sorted(instance.observe_redact) == ["p", "u"]
        ccb.assert_not_called()

    @pytest.mark.asyncio
    async def test_save_failure_does_not_mask_login_outcome(self, tmp_path: Path) -> None:
        tab = _nodriver_tab("<html>Bad login.</html>")
        mock_bs, _ = _nodriver_session_mock(tab)
        login = generate_login_method(_make_plugin("nodriver"))

        with (
            patch("graftpunk.BrowserSession", mock_bs),
            patch("graftpunk.observe.OBSERVE_BASE_DIR", tmp_path),
            patch(
                "graftpunk.observe.run.save_observe_run",
                new_callable=AsyncMock,
                side_effect=OSError("disk full"),
            ),
            patch("graftpunk.plugins.login_engine.LOG") as mock_log,
        ):
            result = await login({"username": "u", "password": "p"}, observe_mode="full")

        assert result is False
        assert mock_log.error.call_args.args[0] == "login_observe_save_failed"

    def test_start_login_capture_builds_real_full_backend(self, tmp_path: Path) -> None:
        """No factory mock: the real backend receives bodies_dir under the run dir."""
        from graftpunk.observe import capture as capture_mod
        from graftpunk.observe.capture import NodriverCaptureBackend
        from graftpunk.plugins.login_engine import _start_login_capture

        plugin = _make_plugin("nodriver")
        with (
            patch("graftpunk.observe.OBSERVE_BASE_DIR", tmp_path),
            patch.object(capture_mod, "create_capture_backend", _real_create_capture_backend),
        ):
            capture, storage = _start_login_capture(
                plugin, "nodriver", MagicMock(), "full", get_tab=lambda: None
            )

        assert isinstance(capture, NodriverCaptureBackend)
        assert storage is not None
        assert capture._bodies_dir == storage.run_dir / "bodies"
        assert storage.run_dir.parent == tmp_path / "hl-session"

    def test_start_login_capture_slugifies_session_name(self, tmp_path: Path) -> None:
        from graftpunk.plugins.login_engine import _start_login_capture

        plugin = _make_plugin("nodriver")
        type(plugin).session_name = "My Bank"  # type: ignore[assignment]
        with patch("graftpunk.observe.OBSERVE_BASE_DIR", tmp_path):
            _, storage = _start_login_capture(plugin, "nodriver", MagicMock(), "full")

        assert storage is not None
        assert storage.run_dir.parent == tmp_path / "my-bank"

    def test_start_login_capture_storage_error_falls_back_to_header_capture(
        self, tmp_path: Path
    ) -> None:
        """An observe request must never break the login it is observing."""
        from graftpunk.plugins.login_engine import _start_login_capture

        plugin = _make_plugin("nodriver")
        with (
            patch("graftpunk.observe.OBSERVE_BASE_DIR", tmp_path),
            patch("graftpunk.observe.storage.ObserveStorage", side_effect=OSError("read-only fs")),
            patch("graftpunk.plugins.login_engine.gp_console.warn") as warn,
            patch("graftpunk.plugins.login_engine.LOG") as mock_log,
        ):
            capture, storage = _start_login_capture(plugin, "nodriver", MagicMock(), "full")

        assert storage is None
        assert capture is not None
        assert mock_log.warning.call_args.args[0] == "login_observe_unavailable"
        assert "read-only fs" in warn.call_args.args[0]


# ---------------------------------------------------------------------------
# CLI plumbing
# ---------------------------------------------------------------------------


class TestLoginCommandPlumbing:
    def test_login_command_exposes_headless_flag(self) -> None:
        from graftpunk.cli.login_commands import create_login_fn

        plugin = _make_plugin("nodriver")
        fn = create_login_fn(plugin, generate_login_method(plugin), {"username": "#u"})
        assert "headless" in inspect.signature(fn).parameters

    def _run_body(self, login: Any, ctx: Any, **kwargs: Any) -> None:
        from graftpunk.cli.login_commands import make_login_body

        body = make_login_body(SimpleNamespace(site_name="acme"), login, {"username": "#u"})
        body(ctx, **kwargs)

    def test_passes_headless_and_observe_to_declarative_login(self, monkeypatch: Any) -> None:
        monkeypatch.setenv("ACME_USERNAME", "u")
        seen: dict[str, Any] = {}

        def login(
            credentials: dict[str, str], *, headless: bool | None = None, observe_mode: str = "off"
        ) -> bool:
            seen.update(headless=headless, observe_mode=observe_mode)
            return True

        root = SimpleNamespace(obj={"observe_mode": "full"})
        ctx = SimpleNamespace(find_root=lambda: root)
        self._run_body(login, ctx, headless=True)

        assert seen == {"headless": True, "observe_mode": "full"}

    def test_flag_unset_means_use_config(self, monkeypatch: Any) -> None:
        monkeypatch.setenv("ACME_USERNAME", "u")
        seen: dict[str, Any] = {}

        def login(
            credentials: dict[str, str], *, headless: bool | None = None, observe_mode: str = "off"
        ) -> bool:
            seen.update(headless=headless, observe_mode=observe_mode)
            return True

        root = SimpleNamespace(obj={})
        ctx = SimpleNamespace(find_root=lambda: root)
        self._run_body(login, ctx, headless=False)

        assert seen == {"headless": None, "observe_mode": "off"}

    def test_user_defined_login_without_kwargs_still_works(self, monkeypatch: Any) -> None:
        """A plugin's own login(credentials) must not receive kwargs it never declared."""
        monkeypatch.setenv("ACME_USERNAME", "u")
        calls: list[dict[str, str]] = []

        def login(credentials: dict[str, str]) -> bool:
            calls.append(credentials)
            return True

        root = SimpleNamespace(obj={"observe_mode": "full"})
        ctx = SimpleNamespace(find_root=lambda: root)
        self._run_body(login, ctx, headless=True)

        assert calls == [{"username": "u"}]


# ---------------------------------------------------------------------------
# save_observe_run
# ---------------------------------------------------------------------------


class TestSaveObserveRun:
    @pytest.mark.asyncio
    async def test_writes_screenshot_source_har_and_console(self, tmp_path: Path) -> None:
        from graftpunk.observe.run import save_observe_run
        from graftpunk.observe.storage import ObserveStorage

        storage = ObserveStorage(tmp_path, "sess", "run1")
        backend = MagicMock()
        backend.take_screenshot = AsyncMock(return_value=b"\\x89PNG")
        backend.get_page_source = AsyncMock(return_value="<html/>")
        backend.stop_capture_async = AsyncMock()
        backend.get_har_entries = MagicMock(return_value=[{"request": {}, "response": {}}])
        backend.get_console_logs = MagicMock(return_value=[{"level": "log"}])

        await save_observe_run(storage, backend, "login")

        backend.stop_capture_async.assert_awaited_once()
        assert (storage.run_dir / "page-source.html").read_text() == "<html/>"
        assert list((storage.run_dir / "screenshots").glob("*login*.png"))
        assert any(p.suffix == ".har" for p in storage.run_dir.iterdir())
