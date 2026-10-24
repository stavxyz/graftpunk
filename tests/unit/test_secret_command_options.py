"""A command option marked ``secret`` stays out of an observe run and out of sight (#289).

A plugin command whose ``--api-key`` option carries a secret sends it in a
query string, a header, a JSON body, a form body and a binary request body
that the recorder writes to ``bodies/``. Under ``gp --observe=full`` the run's
HAR and body files must not hold it in any wire form the scrub covers; an
option without the marker is recorded as before.
"""

from __future__ import annotations

import base64
import io
import json
import os
import sys
import urllib.parse
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import typer
from structlog.testing import capture_logs
from typer.testing import CliRunner

from graftpunk.exceptions import PluginError
from graftpunk.plugins.cli_plugin import PluginParamSpec, SitePlugin, command
from tests.unit.cli_harness import strip_ansi

# Characters that change under URL-encoding and JSON-escaping, so each wire form differs.
API_KEY = 'k3y/with+odd=chars&"quote'


def _runner() -> CliRunner:
    """A runner wide enough that typer>=0.26's rich panels neither wrap nor cut a line."""
    return CliRunner(env={"COLUMNS": "200"})


def _forms(secret: str) -> dict[str, str]:
    return {
        "raw": secret,
        "quote_plus": urllib.parse.quote_plus(secret),
        "quote": urllib.parse.quote(secret, safe=""),
        "json": json.dumps(secret)[1:-1],
        "base64": base64.b64encode(secret.encode()).decode(),
    }


def _plugin(site: str, spec: PluginParamSpec) -> SitePlugin:
    """A plugin whose ``search`` sends ``--api-key`` everywhere a value can go."""

    class KeyPlugin(SitePlugin):
        site_name = "keyshop"
        session_name = "keyshop"
        help_text = "t"
        requires_session = False
        base_url = site

        @command(help="Search with a key", params=[spec])
        def search(self, ctx: Any, api_key: str | None = None) -> dict[str, Any]:
            key = api_key or ""
            token = base64.b64encode(key.encode()).decode()
            ctx.session.get(
                f"{self.base_url}/home",
                params={"key": key},
                headers={"X-Api-Key": key, "X-Api-Token": token},
            )
            ctx.session.post(f"{self.base_url}/api/search", json={"key": key})
            ctx.session.post(f"{self.base_url}/login", data={"key": key})
            # Not UTF-8, so the recorder writes it to bodies/ instead of the HAR.
            ctx.session.post(f"{self.base_url}/login", data=b"\xff\xfe" + key.encode())
            return {}

        @command(help="Fail with the key in the URL", params=[spec])
        def fail(self, ctx: Any, api_key: str | None = None) -> dict[str, Any]:
            ctx.session.get(f"{self.base_url}/error", params={"key": api_key}).raise_for_status()
            return {}

        @command(help="Return the key", params=[spec])
        def echo(self, ctx: Any, api_key: str | None = None) -> dict[str, Any]:
            received.append(api_key)
            return {}

    received: list[str | None] = []
    plugin = KeyPlugin()
    plugin.received = received  # ty: ignore[unresolved-attribute]
    return plugin


def _app(plugin: SitePlugin, name: str = "search") -> typer.Typer:
    from graftpunk.cli.command_factory import synthesize_command_fn
    from graftpunk.cli.plugin_runtime import run_plugin_command

    spec = next(s for s in plugin.get_commands() if s.name == name)
    app = typer.Typer()
    app.command(name=name)(
        synthesize_command_fn(
            name=name,
            param_specs=list(spec.params),
            body=lambda ctx, **kw: run_plugin_command(plugin, spec, ctx, **kw),
            plugin_name=plugin.site_name,
        )
    )
    # A second command keeps Typer from collapsing the app into its one command.
    app.command(name="noop")(lambda: None)
    return app


def _observed_run(
    tmp_path: Path, plugin: SitePlugin, args: list[str], name: str = "search", **invoke: Any
) -> tuple[str, list[bytes]]:
    """Run *name* under observe full; return the HAR text and the request body files."""
    with (
        patch("graftpunk.observe.context.OBSERVE_BASE_DIR", tmp_path),
        patch("graftpunk.cli.plugin_runtime.format_output"),
    ):
        result = _runner().invoke(
            _app(plugin, name), [name, *args], obj={"observe_mode": "full"}, **invoke
        )
    assert result.exit_code == 0, strip_ansi(result.output)
    (run,) = [p for p in tmp_path.glob("[!.]*/*") if p.is_dir()]
    bodies = [p.read_bytes() for p in sorted((run / "bodies").glob("*.request*"))]
    return (run / "network.har").read_text(), bodies


def _assert_no_secret(har: str, bodies: list[bytes], secret: str = API_KEY) -> None:
    for form_name, form in _forms(secret).items():
        assert form not in har, f"{form_name} form of the secret is in the HAR"
    assert bodies, "the binary body was not written to bodies/"
    for body in bodies:
        assert secret.encode() not in body


@pytest.fixture
def tty_stdin(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """stdin is a real terminal (a pty) while the command is built."""
    controller, terminal = os.openpty()
    with os.fdopen(terminal) as stdin:
        monkeypatch.setattr(sys, "stdin", stdin)
        try:
            yield
        finally:
            os.close(controller)


class TestReproduction:
    """Without the marker an option's value is recorded, which is what #289 reports."""

    def test_a_plain_option_lands_in_query_header_and_body(self, site: str, tmp_path: Path) -> None:
        spec = PluginParamSpec.option("api_key", help="API key")
        har, bodies = _observed_run(tmp_path, _plugin(site, spec), ["--api-key", API_KEY])

        entries = json.loads(har)["log"]["entries"]
        get = entries[0]["request"]
        assert get["url"].endswith(f"/home?key={_forms(API_KEY)['quote_plus']}")
        headers = {h["name"]: h["value"] for h in get["headers"]}
        assert headers["X-Api-Key"] == API_KEY
        assert headers["X-Api-Token"] == _forms(API_KEY)["base64"]
        assert entries[1]["request"]["postData"]["text"] == json.dumps({"key": API_KEY})
        form = entries[2]["request"]["postData"]["text"]
        assert form == f"key={_forms(API_KEY)['quote_plus']}"
        assert any(API_KEY.encode() in body for body in bodies)


class TestTheRunHoldsNoSecret:
    def test_no_wire_form_of_a_secret_option_is_in_the_har_or_a_body_file(
        self, site: str, tmp_path: Path
    ) -> None:
        from graftpunk.observe.run import REDACTED

        spec = PluginParamSpec.option("api_key", help="API key", secret=True)
        har, bodies = _observed_run(tmp_path, _plugin(site, spec), ["--api-key", API_KEY])

        _assert_no_secret(har, bodies)
        get = json.loads(har)["log"]["entries"][0]["request"]
        headers = {h["name"]: h["value"] for h in get["headers"]}
        assert headers["X-Api-Key"] == REDACTED
        assert headers["X-Api-Token"] == REDACTED
        assert get["url"].endswith(f"/home?key={REDACTED}")

    def test_a_value_from_an_environment_variable_is_scrubbed_too(
        self, site: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("KEYSHOP_API_KEY", API_KEY)
        spec = PluginParamSpec.option(
            "api_key", secret=True, click_kwargs={"envvar": "KEYSHOP_API_KEY"}
        )
        har, bodies = _observed_run(tmp_path, _plugin(site, spec), [])

        _assert_no_secret(har, bodies)

    def test_a_default_value_is_scrubbed_too(self, site: str, tmp_path: Path) -> None:
        spec = PluginParamSpec.option("api_key", default=API_KEY, secret=True)
        har, bodies = _observed_run(tmp_path, _plugin(site, spec), [])

        _assert_no_secret(har, bodies)

    def test_the_login_credentials_are_still_scrubbed_beside_it(
        self, site: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from graftpunk.plugins.cli_plugin import LoginConfig, LoginStep

        monkeypatch.setenv("KEYSHOP_PASSWORD", "hunter2-pass")
        spec = PluginParamSpec.option("api_key", secret=True)

        class LoginPlugin(SitePlugin):
            site_name = "keyshop"
            session_name = "keyshop"
            help_text = "t"
            requires_session = False
            base_url = site
            login_config = LoginConfig(
                steps=[LoginStep(fields={"password": "#p"}, submit="#s")], url="/login"
            )

            @command(help="Send both", params=[spec])
            def both(self, ctx: Any, api_key: str | None = None) -> dict[str, Any]:
                ctx.session.post(
                    f"{self.base_url}/api/search", json={"k": api_key, "p": "hunter2-pass"}
                )
                return {}

        har, _ = _observed_run(tmp_path, LoginPlugin(), ["--api-key", API_KEY], name="both")

        assert API_KEY not in har
        assert "hunter2-pass" not in har


class TestNoEcho:
    def test_help_shows_no_default(self, site: str) -> None:
        spec = PluginParamSpec.option("api_key", default="default-key-value", secret=True)
        result = _runner().invoke(_app(_plugin(site, spec)), ["search", "--help"])

        assert result.exit_code == 0, strip_ansi(result.output)
        assert "--api-key" in strip_ansi(result.output)
        assert "default-key-value" not in strip_ansi(result.output)

    def test_help_shows_the_variable_name_and_not_its_value(
        self, site: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("KEYSHOP_API_KEY", API_KEY)
        spec = PluginParamSpec.option(
            "api_key", secret=True, click_kwargs={"envvar": "KEYSHOP_API_KEY"}
        )
        result = _runner().invoke(_app(_plugin(site, spec)), ["search", "--help"])

        assert result.exit_code == 0, strip_ansi(result.output)
        assert "KEYSHOP_API_KEY" in strip_ansi(result.output)
        assert API_KEY not in strip_ansi(result.output)

    def test_show_default_is_refused(self, site: str) -> None:
        spec = PluginParamSpec.option(
            "api_key", default="x", secret=True, click_kwargs={"show_default": True}
        )
        with pytest.raises(PluginError, match="cannot show its default"):
            _app(_plugin(site, spec))

    @pytest.mark.parametrize("observe_mode", ["off", "full"])
    def test_an_error_message_and_its_log_line_hold_no_secret(
        self, site: str, tmp_path: Path, observe_mode: str
    ) -> None:
        spec = PluginParamSpec.option("api_key", secret=True)
        with (
            patch("graftpunk.observe.context.OBSERVE_BASE_DIR", tmp_path),
            capture_logs() as logs,
        ):
            result = _runner().invoke(
                _app(_plugin(site, spec), "fail"),
                ["fail", "--api-key", API_KEY],
                obj={"observe_mode": observe_mode},
            )

        assert result.exit_code == 1
        assert "Command failed: 404 Client Error" in strip_ansi(result.output)
        for form in _forms(API_KEY).values():
            assert form not in strip_ansi(result.output)
            assert form not in json.dumps(logs, default=str)
        (failed,) = [e for e in logs if e["event"] == "plugin_command_failed"]
        assert failed["log_level"] == "error"
        assert "exc_info" not in failed

    def test_a_command_without_a_secret_still_logs_its_traceback(
        self, site: str, tmp_path: Path
    ) -> None:
        spec = PluginParamSpec.option("api_key")
        with capture_logs() as logs:
            result = _runner().invoke(
                _app(_plugin(site, spec), "fail"), ["fail", "--api-key", API_KEY]
            )

        assert result.exit_code == 1
        (failed,) = [e for e in logs if e["event"] == "plugin_command_failed"]
        assert failed.get("exc_info") is True


class TestPrompt:
    def test_a_missing_required_secret_is_prompted_for_hidden_on_a_tty(
        self, site: str, tty_stdin: None
    ) -> None:
        spec = PluginParamSpec.option("api_key", required=True, secret=True)
        plugin = _plugin(site, spec)
        app = _app(plugin, "echo")

        with patch("graftpunk.cli.plugin_runtime.format_output"):
            result = _runner().invoke(app, ["echo"], input=f"{API_KEY}\n")

        assert result.exit_code == 0, strip_ansi(result.output)
        assert "Api key:" in strip_ansi(result.output)
        assert API_KEY not in strip_ansi(result.output)
        assert plugin.received == [API_KEY]  # ty: ignore[unresolved-attribute]

    def test_a_given_secret_is_not_prompted_for(self, site: str, tty_stdin: None) -> None:
        spec = PluginParamSpec.option("api_key", required=True, secret=True)
        plugin = _plugin(site, spec)

        with patch("graftpunk.cli.plugin_runtime.format_output"):
            result = _runner().invoke(_app(plugin, "echo"), ["echo", "--api-key", API_KEY])

        assert result.exit_code == 0, strip_ansi(result.output)
        assert "Api key:" not in strip_ansi(result.output)
        assert plugin.received == [API_KEY]  # ty: ignore[unresolved-attribute]

    def test_a_missing_required_secret_is_an_error_without_a_tty(
        self, site: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sys, "stdin", io.StringIO(f"{API_KEY}\n"))
        spec = PluginParamSpec.option("api_key", required=True, secret=True)
        plugin = _plugin(site, spec)

        result = _runner().invoke(_app(plugin, "echo"), ["echo"], input=f"{API_KEY}\n")

        assert result.exit_code == 2
        assert "Missing option '--api-key'" in strip_ansi(result.output)
        assert "Api key:" not in strip_ansi(result.output)
        assert plugin.received == []  # ty: ignore[unresolved-attribute]

    def test_a_plain_required_option_is_not_prompted_for_on_a_tty(
        self, site: str, tty_stdin: None
    ) -> None:
        spec = PluginParamSpec.option("api_key", required=True)
        result = _runner().invoke(_app(_plugin(site, spec), "echo"), ["echo"], input=f"{API_KEY}\n")

        assert result.exit_code == 2
        assert "Missing option '--api-key'" in strip_ansi(result.output)


class TestWhatASecretCannotBe:
    @pytest.mark.parametrize(
        ("spec", "message"),
        [
            (PluginParamSpec.option("pin", type=int, secret=True), "must be type str"),
            (
                PluginParamSpec.option("on", type=bool, default=False, secret=True),
                "must be type str",
            ),
            (
                PluginParamSpec.option("keys", secret=True, click_kwargs={"multiple": True}),
                "cannot be a flag or multiple",
            ),
        ],
    )
    def test_a_secret_option_that_could_show_its_value_is_refused(
        self, spec: PluginParamSpec, message: str
    ) -> None:
        from graftpunk.cli.command_factory import map_param_spec

        with pytest.raises(PluginError, match=message):
            map_param_spec("keyshop", "search", spec)

    def test_an_argument_cannot_be_secret(self) -> None:
        from graftpunk.cli.command_factory import map_param_spec

        spec = PluginParamSpec.argument("key", click_kwargs={"secret": True})
        with pytest.raises(PluginError, match="unsupported click_kwargs key"):
            map_param_spec("keyshop", "search", spec)

    def test_the_marker_is_the_click_kwargs_key(self) -> None:
        assert PluginParamSpec.option("k", secret=True).secret
        assert PluginParamSpec.option("k", click_kwargs={"secret": True}).secret
        assert not PluginParamSpec.option("k").secret


def _yaml_plugin(tmp_path: Path, site: str, param: str) -> SitePlugin:
    from graftpunk.plugins.yaml_loader import parse_yaml_plugin
    from graftpunk.plugins.yaml_plugin import create_yaml_site_plugin

    path = tmp_path / "keyshop.yaml"
    path.write_text(
        f"""
site_name: keyshop
base_url: "{site}"
requires_session: false
commands:
  search:
    help: "Search with a key"
    url: "/home"
    raise_for_status: false
    params:
{param}
"""
    )
    bundle = parse_yaml_plugin(path)
    return create_yaml_site_plugin(bundle.config, bundle.commands, bundle.headers)


class TestYaml:
    def test_a_secret_yaml_param_is_scrubbed_from_the_har_and_the_logs(
        self, site: str, tmp_path: Path
    ) -> None:
        plugin = _yaml_plugin(
            tmp_path,
            site,
            "      - name: api_key\n        required: true\n        secret: true\n",
        )
        runs = tmp_path / "runs"
        with capture_logs() as logs:
            har, _ = _observed_run(runs, plugin, ["--api-key", API_KEY])

        for form in _forms(API_KEY).values():
            assert form not in har
            assert form not in json.dumps(logs, default=str)
        assert any(e["event"] == "yaml_plugin_request" for e in logs)

    def test_a_plain_yaml_param_is_recorded_as_before(self, site: str, tmp_path: Path) -> None:
        plugin = _yaml_plugin(tmp_path, site, "      - name: api_key\n")
        har, _ = _observed_run(tmp_path / "runs", plugin, ["--api-key", API_KEY])

        assert _forms(API_KEY)["quote_plus"] in har

    def test_a_secret_yaml_param_gets_the_hidden_prompt(
        self, site: str, tmp_path: Path, tty_stdin: None
    ) -> None:
        plugin = _yaml_plugin(
            tmp_path,
            site,
            "      - name: api_key\n        required: true\n        secret: true\n",
        )
        (spec,) = plugin.get_commands()[0].params
        assert spec.secret
        with patch("graftpunk.cli.plugin_runtime.format_output"):
            result = _runner().invoke(_app(plugin), ["search"], input=f"{API_KEY}\n")

        assert result.exit_code == 0, strip_ansi(result.output)
        assert "Api key:" in strip_ansi(result.output)
        assert API_KEY not in strip_ansi(result.output)

    @pytest.mark.parametrize(
        ("param", "message"),
        [
            ("      - name: pin\n        type: int\n        secret: true\n", "str option"),
            ("      - name: key\n        is_option: false\n        secret: true\n", "str option"),
            ("      - name: key\n        secret: 'yes'\n", "must be true or false"),
        ],
    )
    def test_a_yaml_secret_that_could_show_its_value_is_refused(
        self, site: str, tmp_path: Path, param: str, message: str
    ) -> None:
        with pytest.raises(PluginError, match=message):
            _yaml_plugin(tmp_path, site, param)


class TestRecorderLogs:
    def test_a_url_the_recorder_logs_is_scrubbed(self, site: str) -> None:
        import requests

        from graftpunk.observe.http_capture import HttpCaptureBackend

        session = requests.Session()
        capture = HttpCaptureBackend(session, redact=[API_KEY])
        response = session.get(f"{site}/home", params={"key": API_KEY})
        capture.stop_capture()
        with capture_logs() as logs:
            capture.record(response, 0.0, 0.0)

        (dropped,) = [e for e in logs if e["event"] == "http_capture_response_after_stop"]
        assert dropped["url"].endswith("/home?key=***REDACTED***")
