"""The HTTP recorder records without changing what a request does (#283, review round 2).

A request made with the recorder on must return, raise, run its hooks and set
its cookies exactly as with it off. Bodies are read only where requests reads
them. Also: the run's HAR is written atomically, the spool is removed on every
exit, a response that arrives after the run is written is dropped, and the
adapter-wrapping rules (pickling gate, double wrap, late mount).
"""

from __future__ import annotations

import datetime
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
import requests
import requests.adapters
from structlog.testing import capture_logs
from typer.testing import CliRunner

from graftpunk.observe.http_capture import HttpCaptureBackend, _RecordingAdapterSend
from graftpunk.observe.storage import ObserveStorage


def _outcome(site: str, path: str, *, record: bool) -> dict[str, Any]:
    """What one GET does: its result or exception, the session's cookies, the hooks run."""
    session = requests.Session()
    hooks: list[int] = []
    session.hooks["response"].append(lambda r, *a, **k: hooks.append(r.status_code))
    capture = HttpCaptureBackend(session) if record else None
    if capture is not None:
        capture.start_capture()
    try:
        response = session.get(f"{site}{path}", timeout=5)
        result: Any = (response.status_code, [h.status_code for h in response.history])
    except requests.RequestException as exc:
        result = type(exc).__name__
    finally:
        if capture is not None:
            capture.stop_capture()
    return {
        "result": result,
        "cookies": dict(session.cookies),
        "hooks": hooks,
        "entries": capture.get_har_entries() if capture is not None else None,
    }


class TestRecordingChangesNothing:
    @pytest.mark.parametrize(
        "path", ["/badgz-redirect", "/trunc-redirect", "/trunc-cookie", "/truncated", "/home"]
    )
    def test_observe_on_and_off_give_the_same_outcome(self, site: str, path: str) -> None:
        off = _outcome(site, path, record=False)
        on = _outcome(site, path, record=True)

        assert on["result"] == off["result"]
        assert on["cookies"] == off["cookies"]
        assert on["hooks"] == off["hooks"]

    def test_a_redirect_hop_with_a_broken_body_is_followed_and_marked(self, site: str) -> None:
        on = _outcome(site, "/badgz-redirect", record=True)

        assert on["result"] == (200, [302])
        hop, landing = on["entries"]
        assert hop["response"]["status"] == 302
        assert hop["response"]["content"]["_bodyNotCapturedReason"] == "read_failed"
        assert landing["response"]["content"]["text"] == "<h1>home</h1>"

    def test_a_final_body_that_fails_to_read_keeps_its_cookie_and_is_marked(
        self, site: str
    ) -> None:
        on = _outcome(site, "/trunc-cookie", record=True)

        assert on["result"] == "ChunkedEncodingError"
        assert on["cookies"] == {"sid": "abc123"}
        assert on["hooks"] == [200]
        (entry,) = on["entries"]
        assert entry["response"]["content"]["_bodyNotCapturedReason"] == (
            "read_failed: ChunkedEncodingError"
        )

    @pytest.mark.parametrize("observe", [[], ["--no-observe"]])
    def test_gp_http_exits_the_same_on_a_broken_redirect_body(
        self, site: str, tmp_path: Path, observe: list[str]
    ) -> None:
        from graftpunk.cli.main import app

        with patch("graftpunk.observe.context.OBSERVE_BASE_DIR", tmp_path):
            result = CliRunner().invoke(
                app, ["http", "get", "--no-session", *observe, f"{site}/badgz-redirect"]
            )

        assert result.exit_code == 0, result.output
        assert "<h1>home</h1>" in result.output


def _recording(**kwargs: Any) -> tuple[requests.Session, HttpCaptureBackend]:
    session = requests.Session()
    capture = HttpCaptureBackend(session, **kwargs)
    capture.start_capture()
    return session, capture


class TestStreamedAndTimed:
    def test_a_streamed_empty_response_is_not_marked(self, site: str) -> None:
        session, capture = _recording()
        session.get(f"{site}/empty", stream=True, timeout=5)
        capture.stop_capture()

        (entry,) = capture.get_har_entries()
        assert "_bodyNotCaptured" not in entry["response"]["content"]

    def test_a_request_is_timed_from_when_it_was_sent(self, site: str) -> None:
        session, capture = _recording()
        response = session.get(f"{site}/slow", timeout=5)
        capture.stop_capture()

        (entry,) = capture.get_har_entries()
        started = datetime.datetime.fromisoformat(entry["startedDateTime"]).timestamp()
        # Before the server had the request, not after the 0.3 s answer.
        assert started <= float(response.headers["X-Received"])

    def test_a_quoted_charset_is_honored(self, site: str) -> None:
        from tests.unit.observe_http_site import LATIN1_TEXT

        session, capture = _recording()
        session.get(f"{site}/latin1-quoted", timeout=5)
        capture.stop_capture()

        (entry,) = capture.get_har_entries()
        assert entry["response"]["content"]["text"] == LATIN1_TEXT

    @pytest.mark.parametrize(
        ("content_type", "charset"),
        [
            ('text/html; charset="iso-8859-1"', "iso-8859-1"),
            ("text/html; charset='utf-8'", "utf-8"),
            ('text/html; charset=""', None),
            ("text/html", None),
        ],
    )
    def test_the_declared_charset_is_read_without_its_quotes(
        self, content_type: str, charset: str | None
    ) -> None:
        # Python's codec lookup happens to ignore the quotes, so the parser is
        # pinned directly.
        from graftpunk.observe.capture import _declared_charset

        assert _declared_charset(content_type) == charset

    def test_one_response_that_cannot_be_recorded_does_not_cost_the_others(self, site: str) -> None:
        from graftpunk.observe import http_capture

        real = http_capture.har_entry_for_response

        def fail_for_the_hop(response: Any, *args: Any, **kwargs: Any) -> Any:
            if response.status_code == 302:
                raise ValueError("cannot build this one")
            return real(response, *args, **kwargs)

        session, capture = _recording()
        with patch.object(http_capture, "har_entry_for_response", side_effect=fail_for_the_hop):
            session.post(f"{site}/login", data={"user": "alice"}, timeout=5)
        capture.stop_capture()

        (entry,) = capture.get_har_entries()
        assert entry["response"]["status"] == 200


class _OwnStateAdapter(requests.adapters.HTTPAdapter):
    """An adapter that pickles its whole instance dict, as some custom ones do."""

    def __getstate__(self) -> dict[str, Any]:
        return dict(self.__dict__)

    def __setstate__(self, state: dict[str, Any]) -> None:
        self.__dict__.update(state)


class TestAdapters:
    def test_an_adapter_with_its_own_pickled_state_is_not_wrapped(self, site: str) -> None:
        import dill

        session = requests.Session()
        session.mount("http://", _OwnStateAdapter())
        capture = HttpCaptureBackend(session)
        with capture_logs() as logs:
            capture.start_capture()
        session.get(f"{site}/home", timeout=5)

        adapter = session.adapters["http://"]
        assert "send" not in vars(adapter)
        assert "http_capture_adapter_not_recorded" in [e["event"] for e in logs]
        blob = dill.dumps(session)
        assert b"<h1>home</h1>" not in blob
        assert b"HttpCaptureBackend" not in blob
        # Still recorded, from the response the session returned.
        assert len(capture.get_har_entries()) == 1

    def test_one_adapter_mounted_twice_is_wrapped_once(self, site: str) -> None:
        session = requests.Session()
        adapter = requests.adapters.HTTPAdapter()
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        capture = HttpCaptureBackend(session)
        capture.start_capture()
        assert isinstance(vars(adapter)["send"], _RecordingAdapterSend)
        assert vars(adapter)["send"].original.__self__ is adapter
        session.get(f"{site}/home", timeout=5)
        capture.stop_capture()

        assert len(capture.get_har_entries()) == 1
        assert "send" not in vars(adapter)

    def test_an_adapter_mounted_after_start_is_recorded_and_warned_about(self, site: str) -> None:
        session, capture = _recording()
        session.mount("http://", requests.adapters.HTTPAdapter(max_retries=2))
        with capture_logs() as logs:
            session.get(f"{site}/home", timeout=5)
        capture.stop_capture()

        (entry,) = capture.get_har_entries()
        assert entry["response"]["content"]["text"] == "<h1>home</h1>"
        assert "http_capture_adapter_not_recorded" in [e["event"] for e in logs]


class TestTheRunIsWrittenSafely:
    def test_a_failed_har_write_leaves_no_partial_har_and_no_spool(
        self, site: str, tmp_path: Path
    ) -> None:
        from graftpunk.observe import run as run_module

        storage = ObserveStorage(tmp_path, "myshop", "run-1")
        spool = storage.run_dir / "network.har.spool"
        session, capture = _recording(spool_path=spool)
        session.get(f"{site}/home", timeout=5)
        session.get(f"{site}/csv", timeout=5)
        capture.stop_capture()
        real = run_module.redact_har_entries
        calls = {"n": 0}

        def fail_second(entries: Any, secrets: Any) -> Any:
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("scrub failed")
            return real(entries, secrets)

        with patch.object(run_module, "redact_har_entries", side_effect=fail_second):
            written = run_module.write_capture_data(storage, capture, ("x" * 5,))

        assert written == 0
        assert sorted(p.name for p in storage.run_dir.iterdir()) == []

    def test_a_failed_har_write_keeps_an_earlier_har_whole(self, tmp_path: Path) -> None:
        storage = ObserveStorage(tmp_path, "myshop", "run-1")
        storage.write_har([{"request": {"url": "https://example.com/"}}])

        def entries() -> Any:
            yield {"request": {"url": "https://example.com/a"}}
            raise OSError("disk full")

        with pytest.raises(OSError, match="disk full"):
            storage.write_har(entries())

        har = json.loads((storage.run_dir / "network.har").read_text())
        assert har["log"]["entries"] == [{"request": {"url": "https://example.com/"}}]
        assert [p.name for p in storage.run_dir.iterdir()] == ["network.har"]

    def test_a_response_after_stop_does_not_recreate_the_spool(
        self, site: str, tmp_path: Path
    ) -> None:
        spool = tmp_path / "network.har.spool"
        session, capture = _recording(spool_path=spool, bodies_dir=tmp_path / "bodies")
        in_flight = session.send  # what a request already inside send holds
        capture.stop_capture()
        capture.discard_spool()

        in_flight(requests.Request("GET", f"{site}/logo.png").prepare(), timeout=5)

        assert not spool.exists()
        assert not (tmp_path / "bodies").exists()
        assert capture.get_har_entries() == []

    def test_entries_are_scrubbed_before_they_reach_the_spool(
        self, site: str, tmp_path: Path
    ) -> None:
        spool = tmp_path / "network.har.spool"
        session, capture = _recording(spool_path=spool, redact=("hunter2-correct-horse",))
        session.get(f"{site}/home", params={"p": "hunter2-correct-horse"}, timeout=5)

        assert "hunter2-correct-horse" not in spool.read_text()


class TestPrivateFiles:
    def test_an_existing_file_is_made_owner_only(self, tmp_path: Path) -> None:
        import stat

        from graftpunk.observe.storage import write_private_file

        path = tmp_path / "events.jsonl"
        path.write_text("")
        path.chmod(0o644)
        write_private_file(path, b"x", append=True)

        assert stat.S_IMODE(path.stat().st_mode) == 0o600


# ---------------------------------------------------------------------------
# What a run is scrubbed of: every credential; an identifier stays in URLs
# ---------------------------------------------------------------------------

USER, PIN, OTP = "bob@example.com", "48213", "771904"


def _login_har_entry() -> dict[str, Any]:
    """A login POST carrying all three values, from a URL that names the user."""
    return {
        "request": {
            "method": "POST",
            "url": f"https://myshop.example/login?user={USER}",
            "queryString": [{"name": "user", "value": USER}],
            "headers": [{"name": "X-Login", "value": USER}],
            "postData": {"text": f"user={USER}&pin={PIN}&otp={OTP}"},
        },
        "response": {"content": {"text": f"welcome {USER}"}, "redirectURL": ""},
    }


def _assert_scrubbed(entry: dict[str, Any]) -> None:
    from graftpunk.observe.run import REDACTED

    text = json.dumps(entry)
    assert PIN not in text
    assert OTP not in text
    request = entry["request"]
    # The identifier is kept where the digest reads endpoints...
    assert request["url"] == f"https://myshop.example/login?user={USER}"
    assert request["queryString"] == [{"name": "user", "value": USER}]
    # ...and scrubbed from the body and headers.
    assert request["postData"]["text"] == f"user={REDACTED}&pin={REDACTED}&otp={REDACTED}"
    assert request["headers"] == [{"name": "X-Login", "value": REDACTED}]
    assert entry["response"]["content"]["text"] == f"welcome {REDACTED}"


class TestCredentialScrub:
    def test_a_login_run_scrubs_pin_and_otp_and_keeps_the_user_in_urls(self) -> None:
        from graftpunk.observe.run import redact_har_entries
        from graftpunk.plugins.cli_plugin import LoginConfig, LoginStep, SitePlugin
        from graftpunk.plugins.login_engine import generate_login_method
        from tests.unit.test_login_headless_observe import (
            _nodriver_session_mock,
            _nodriver_tab,
        )

        class PinPlugin(SitePlugin):
            site_name = "myshop"
            session_name = "myshop"
            help_text = "t"
            base_url = "https://myshop.example"
            backend = "nodriver"
            login_config = LoginConfig(
                steps=[LoginStep(fields={"username": "#u", "pin": "#p", "otp": "#o"}, submit="#s")],
                url="/login",
                failure="Bad login.",
                timeout=0.05,
                settle=0.0,
            )

        mock_bs, _ = _nodriver_session_mock(_nodriver_tab())
        capture = AsyncMock()
        capture.get_header_roles = lambda: {}
        login = generate_login_method(PinPlugin())
        with (
            patch("graftpunk.BrowserSession", mock_bs),
            patch("graftpunk.observe.capture.create_capture_backend", return_value=capture),
            patch("graftpunk.observe.run.save_observe_run", new_callable=AsyncMock) as save,
            patch("graftpunk.observe.OBSERVE_BASE_DIR", Path("/nonexistent-unused")),
            patch("graftpunk.observe.storage.ObserveStorage"),
        ):
            import asyncio

            asyncio.run(login({"username": USER, "pin": PIN, "otp": OTP}, observe_mode="full"))

        redaction = save.await_args.kwargs["redact"]
        (entry,) = redact_har_entries([_login_har_entry()], redaction)
        _assert_scrubbed(entry)

    def test_a_plugin_command_run_scrubs_pin_and_otp_and_keeps_the_user_in_urls(
        self, site: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import typer

        from graftpunk.cli.command_factory import synthesize_command_fn
        from graftpunk.cli.plugin_runtime import run_plugin_command
        from graftpunk.observe.run import REDACTED
        from graftpunk.plugins.cli_plugin import LoginConfig, LoginStep, SitePlugin, command

        for name, value in (("USERNAME", USER), ("PIN", PIN), ("OTP", OTP)):
            monkeypatch.setenv(f"MYSHOP_{name}", value)

        class PinPlugin(SitePlugin):
            site_name = "myshop"
            session_name = "myshop"
            help_text = "t"
            requires_session = False
            base_url = site
            login_config = LoginConfig(
                steps=[LoginStep(fields={"username": "#u", "pin": "#p", "otp": "#o"}, submit="#s")],
                url="/login",
            )

            @command(help="Sign in again")
            def verify(self, ctx: Any) -> dict[str, Any]:
                ctx.session.post(
                    f"{self.base_url}/login",
                    params={"user": USER},
                    data={"user": USER, "pin": PIN, "otp": OTP},
                )
                return {}

        plugin = PinPlugin()
        spec = next(s for s in plugin.get_commands() if s.name == "verify")
        app = typer.Typer()
        app.command(name="verify")(
            synthesize_command_fn(
                name="verify",
                param_specs=list(spec.params),
                body=lambda ctx, **kw: run_plugin_command(plugin, spec, ctx, **kw),
            )
        )
        with (
            patch("graftpunk.observe.context.OBSERVE_BASE_DIR", tmp_path),
            patch("graftpunk.cli.plugin_runtime.format_output"),
        ):
            result = CliRunner().invoke(app, [], obj={"observe_mode": "full"})

        assert result.exit_code == 0, result.output
        (run,) = [p for p in tmp_path.glob("[!.]*/*") if p.is_dir()]
        text = (run / "network.har").read_text()
        assert PIN not in text
        assert OTP not in text
        post = json.loads(text)["log"]["entries"][0]["request"]
        assert post["url"] == f"{site}/login?user=bob%40example.com"
        assert post["postData"]["text"] == f"user={REDACTED}&pin={REDACTED}&otp={REDACTED}"
