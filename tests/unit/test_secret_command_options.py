"""A command option marked ``secret`` stays out of an observe run and out of sight (#289).

A plugin command whose ``--api-key`` option carries a secret sends it in a
query string, a header, a JSON body, a form body and a binary request body
that the recorder writes to ``bodies/``. Under ``gp --observe=full`` the run's
HAR and body files must not hold it in any wire form the scrub covers; an
option without the marker is recorded as before. Error messages, graftpunk's
log lines (read here mostly as the gp CLI renders them) and tracebacks leave it
out, and a missing one is prompted for on stderr.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import textwrap
import urllib.parse
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import requests
import typer
from requests.utils import requote_uri
from structlog.testing import capture_logs
from typer.testing import CliRunner

from graftpunk import workstation_env
from graftpunk.exceptions import CommandError, PluginError
from graftpunk.observe.run import REDACTED
from graftpunk.plugins.cli_plugin import PluginParamSpec, SitePlugin, command
from tests.unit.cli_harness import strip_ansi

# Characters that change under URL-encoding and JSON-escaping, so each wire form differs.
API_KEY = 'k3y/with+odd=chars&"quote'
ENV_NAME = "KEYSHOP_API_KEY"


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
        "requote_uri": requote_uri(secret),
    }


def _assert_no_form(text: str, secret: str = API_KEY) -> None:
    for form_name, form in _forms(secret).items():
        assert form not in text, f"the {form_name} form of the secret is in {text[:300]!r}"


def _plugin(site: str, spec: PluginParamSpec) -> SitePlugin:
    """A plugin whose commands send ``--api-key`` everywhere a value can go."""
    count = PluginParamSpec.option("count", type=int, default=0)
    name = PluginParamSpec.option("name", default="")

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

        @command(help="Refuse with the key in the message", params=[spec])
        def refuse(self, ctx: Any, api_key: str | None = None) -> dict[str, Any]:
            raise CommandError(f"bad key {api_key}")

        @command(help="Break with the key in the message", params=[spec])
        def breaks(self, ctx: Any, api_key: str | None = None) -> dict[str, Any]:
            raise PluginError(f"bad key {api_key}")

        @command(help="Return the key", params=[spec])
        def echo(self, ctx: Any, api_key: str | None = None) -> dict[str, Any]:
            received.append(api_key)
            return {}

        @command(help="Count", params=[spec, count, name])
        def tally(
            self, ctx: Any, api_key: str | None = None, count: int = 0, name: str = ""
        ) -> dict[str, Any]:
            return {}

    received: list[str | None] = []
    plugin = KeyPlugin()
    plugin.received = received  # ty: ignore[unresolved-attribute]
    return plugin


def _app(plugin: SitePlugin) -> typer.Typer:
    """*plugin* registered the way gp registers it: ``keyshop <command>``."""
    from graftpunk.cli.plugin_commands import register_plugin_commands

    app = typer.Typer()
    with patch("graftpunk.cli.plugin_commands.discover_all_plugins", return_value=(plugin,)):
        register_plugin_commands(app, notify_errors=False)
    return app


def _invoke(plugin: SitePlugin, args: list[str], **invoke: Any) -> Any:
    return _runner().invoke(_app(plugin), ["keyshop", *args], **invoke)


def _observed_run(
    tmp_path: Path, plugin: SitePlugin, args: list[str], name: str = "search"
) -> tuple[str, list[bytes]]:
    """Run *name* under observe full; return the HAR text and the request body files."""
    with (
        patch("graftpunk.observe.context.OBSERVE_BASE_DIR", tmp_path),
        patch("graftpunk.cli.plugin_runtime.format_output"),
    ):
        result = _invoke(plugin, [name, *args], obj={"observe_mode": "full"})
    assert result.exit_code == 0, strip_ansi(result.output)
    (run,) = [p for p in tmp_path.glob("[!.]*/*") if p.is_dir()]
    bodies = [p.read_bytes() for p in sorted((run / "bodies").glob("*.request*"))]
    return (run / "network.har").read_text(), bodies


def _assert_no_secret(har: str, bodies: list[bytes], secret: str = API_KEY) -> None:
    _assert_no_form(har, secret)
    assert bodies, "the binary body was not written to bodies/"
    for body in bodies:
        assert secret.encode() not in body


JsonLogs = Callable[[], Callable[[], list[dict[str, Any]]]]


@pytest.fixture
def gp_json_logs(capsys: pytest.CaptureFixture[str]) -> JsonLogs:
    """Call it first in the test body (configure_logging binds the stderr of that
    moment): it logs as ``gp --log-format json -vv`` does and returns a reader of
    the lines written."""
    from graftpunk.logging import configure_logging

    def start() -> Callable[[], list[dict[str, Any]]]:
        configure_logging(level="DEBUG", json_output=True)

        def read() -> list[dict[str, Any]]:
            lines = capsys.readouterr().err.splitlines()
            return [json.loads(line) for line in lines if line.startswith("{")]

        return read

    return start


@pytest.fixture
def gp_console_logs(capsys: pytest.CaptureFixture[str]) -> Callable[[], Callable[[], str]]:
    """As :func:`gp_json_logs`, for ``gp -vv``'s console format, uncoloured."""
    from graftpunk.logging import configure_logging

    def start() -> Callable[[], str]:
        configure_logging(level="DEBUG", json_output=False)
        return lambda: strip_ansi(capsys.readouterr().err)

    return start


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
        monkeypatch.setenv(ENV_NAME, API_KEY)
        spec = PluginParamSpec.option("api_key", secret=True, click_kwargs={"envvar": ENV_NAME})
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

    def test_the_option_is_scrubbed_when_the_credentials_cannot_be_read(
        self, site: str, tmp_path: Path
    ) -> None:
        spec = PluginParamSpec.option("api_key", secret=True)
        with patch(
            "graftpunk.cli.login_commands.known_credentials", side_effect=RuntimeError("no env")
        ):
            har, bodies = _observed_run(tmp_path, _plugin(site, spec), ["--api-key", API_KEY])

        _assert_no_secret(har, bodies)

    @pytest.mark.parametrize("value", ["ab cd/ef", "p@ss w0rd!", "x|y^z key"])
    def test_a_value_put_into_the_url_path_is_scrubbed(
        self, site: str, tmp_path: Path, value: str
    ) -> None:
        plugin = _yaml_plugin(
            tmp_path, site, "      - name: api_key\n        secret: true\n", url="/k/{api_key}"
        )
        har, _ = _observed_run(tmp_path / "runs", plugin, ["--api-key", value])

        url = json.loads(har)["log"]["entries"][0]["request"]["url"]
        assert url.endswith(f"/k/{REDACTED}")
        _assert_no_form(har, value)


class TestNoEcho:
    def test_help_shows_no_default(self, site: str) -> None:
        spec = PluginParamSpec.option("api_key", default="default-key-value", secret=True)
        result = _invoke(_plugin(site, spec), ["search", "--help"])

        assert result.exit_code == 0, strip_ansi(result.output)
        assert "--api-key" in strip_ansi(result.output)
        assert "default-key-value" not in strip_ansi(result.output)

    def test_help_shows_the_variable_name_and_not_its_value(
        self, site: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(ENV_NAME, API_KEY)
        spec = PluginParamSpec.option("api_key", secret=True, click_kwargs={"envvar": ENV_NAME})
        result = _invoke(_plugin(site, spec), ["search", "--help"])

        assert result.exit_code == 0, strip_ansi(result.output)
        assert ENV_NAME in strip_ansi(result.output)
        assert API_KEY not in strip_ansi(result.output)

    def test_show_default_is_refused(self) -> None:
        from graftpunk.cli.command_factory import map_param_spec

        spec = PluginParamSpec.option(
            "api_key", default="x", secret=True, click_kwargs={"show_default": True}
        )
        with pytest.raises(PluginError, match="cannot show its default"):
            map_param_spec("keyshop", "search", spec)

    @pytest.mark.parametrize("observe_mode", ["off", "full"])
    def test_an_error_message_and_its_log_line_hold_no_secret(
        self,
        site: str,
        tmp_path: Path,
        observe_mode: str,
        gp_json_logs: JsonLogs,
    ) -> None:
        read_logs = gp_json_logs()
        spec = PluginParamSpec.option("api_key", secret=True)
        with patch("graftpunk.observe.context.OBSERVE_BASE_DIR", tmp_path):
            result = _invoke(
                _plugin(site, spec),
                ["fail", "--api-key", API_KEY],
                obj={"observe_mode": observe_mode},
            )

        output = strip_ansi(result.output)
        assert result.exit_code == 1
        assert f"Command failed: 404 Client Error: Not Found for url: {site}" in output
        assert f"/error?key={REDACTED}" in output
        _assert_no_form(output)
        logs = read_logs()
        _assert_no_form(json.dumps(logs))
        (failed,) = [e for e in logs if e["event"] == "plugin_command_failed"]
        assert failed["level"] == "error"
        # The traceback is kept, scrubbed.
        assert failed["exception"].startswith("Traceback (most recent call last):")
        assert f"HTTPError: 404 Client Error: Not Found for url: {site}" in (failed["exception"])
        assert f"/error?key={REDACTED}" in failed["exception"]

    def test_the_traceback_renders_scrubbed_in_the_console_format(
        self, site: str, gp_console_logs: Callable[[], Callable[[], str]]
    ) -> None:
        read_logs = gp_console_logs()
        spec = PluginParamSpec.option("api_key", secret=True)
        result = _invoke(_plugin(site, spec), ["fail", "--api-key", API_KEY])

        assert result.exit_code == 1
        text = read_logs()
        assert "plugin_command_failed" in text
        assert "Traceback (most recent call last):" in text
        assert f"/error?key={REDACTED}" in text
        _assert_no_form(text)

    def test_a_command_without_a_secret_still_logs_its_traceback(
        self, site: str, gp_json_logs: JsonLogs
    ) -> None:
        read_logs = gp_json_logs()
        spec = PluginParamSpec.option("api_key")
        result = _invoke(_plugin(site, spec), ["fail", "--api-key", "plain-value"])

        assert result.exit_code == 1
        (failed,) = [e for e in read_logs() if e["event"] == "plugin_command_failed"]
        assert failed["exception"].startswith("Traceback (most recent call last):")
        assert "plain-value" in failed["exception"]

    @pytest.mark.parametrize(
        ("command_name", "prefix"), [("refuse", ""), ("breaks", "Plugin error: ")]
    )
    def test_a_command_error_or_plugin_error_message_holds_no_secret(
        self, site: str, command_name: str, prefix: str
    ) -> None:
        spec = PluginParamSpec.option("api_key", secret=True)
        result = _invoke(_plugin(site, spec), [command_name, "--api-key", API_KEY])

        output = strip_ansi(result.output)
        assert result.exit_code == 1
        assert f"{prefix}bad key {REDACTED}" in output
        _assert_no_form(output)

    @pytest.mark.parametrize(
        ("args", "message"),
        [
            # --count takes the next token as its value and quotes it.
            (["--count", f"--api-key={API_KEY}"], "Invalid value for '--count'"),
            # --name takes "--api-key", and the secret is left over as an extra argument.
            (["--name", "--api-key", API_KEY], "unexpected extra argument"),
        ],
    )
    def test_a_usage_error_about_another_option_holds_no_secret(
        self, site: str, args: list[str], message: str
    ) -> None:
        spec = PluginParamSpec.option("api_key", secret=True)
        result = _invoke(_plugin(site, spec), ["tally", *args])

        output = strip_ansi(result.output)
        assert result.exit_code == 2
        assert message in output
        assert REDACTED in output
        _assert_no_form(output)

    def test_a_usage_error_without_a_secret_option_is_unchanged(self, site: str) -> None:
        spec = PluginParamSpec.option("api_key")
        result = _invoke(_plugin(site, spec), ["tally", "--count", "--api-key=plain-value"])

        assert result.exit_code == 2
        # "integer" on Click, "int" on typer>=0.26's vendored Click.
        assert "'--api-key=plain-value' is not a valid int" in strip_ansi(result.output)

    def test_a_token_403_retry_logs_the_url_scrubbed(
        self, site: str, gp_json_logs: JsonLogs
    ) -> None:
        read_logs = gp_json_logs()
        from graftpunk.tokens import Token, TokenConfig

        spec = PluginParamSpec.option("api_key", secret=True)

        class TokenPlugin(SitePlugin):
            site_name = "keyshop"
            session_name = "keyshop"
            help_text = "t"
            base_url = site
            token_config = TokenConfig(
                tokens=(
                    Token(
                        name="X-Page",
                        source="page",
                        pattern="(home)",
                        page_url="/home",
                        extraction="http",
                    ),
                )
            )

            @command(help="Forbidden", params=[spec])
            def forbidden(self, ctx: Any, api_key: str | None = None) -> dict[str, Any]:
                url = f"{self.base_url}/forbidden"
                ctx.session.get(url, params={"key": api_key}).raise_for_status()
                return {}

        with patch(
            "graftpunk.cli.plugin_runtime.load_session_for_api_resolved",
            return_value=(requests.Session(), "keyshop"),
        ):
            result = _invoke(TokenPlugin(), ["forbidden", "--api-key", API_KEY])

        assert result.exit_code == 1
        logs = read_logs()
        (retry,) = [e for e in logs if e["event"] == "token_403_retry"]
        assert retry["url"] == f"{site}/forbidden?key={REDACTED}"
        _assert_no_form(json.dumps(logs))


def _pty_run(
    argv: list[str], env: dict[str, str], stdout_path: Path, answer: str | None
) -> tuple[int, str]:
    """Run *argv* with a pty as its controlling terminal, stdin and stderr, and
    its stdout to *stdout_path*. Type *answer* once ``Api key:`` appears. Return
    the exit status and what the terminal showed.

    The pty is forked from a helper process: pytest runs threads (the local
    site's server), and forking a multi-threaded process can deadlock the child.
    The request goes on the helper's stdin, so the environment stays out of the
    process list and out of a failure message.
    """
    driver = stdout_path.parent / "pty_driver.py"
    driver.write_text(_PTY_DRIVER)
    request = {"argv": argv, "env": env, "stdout": str(stdout_path), "answer": answer}
    done = subprocess.run(  # noqa: S603 - argv is this test's own interpreter and script
        [sys.executable, str(driver)],
        input=json.dumps(request),
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    assert done.returncode == 0, done.stderr[-2000:]
    reply = json.loads(done.stdout)
    return reply["code"], strip_ansi(reply["shown"])


# Types the answer only once the terminal's echo is off: getpass switches it off
# with TCSAFLUSH, which discards anything typed before, so an answer sent as soon
# as the label appears can be lost. The driver keeps the terminal end of the pty
# open to read that flag, so it watches the child exit instead of waiting for EOF.
_PTY_DRIVER = textwrap.dedent(
    """
    import fcntl
    import json
    import os
    import select
    import sys
    import termios
    import time

    request = json.load(sys.stdin)
    controller, terminal = os.openpty()
    pid = os.fork()
    if pid == 0:
        os.setsid()
        fcntl.ioctl(terminal, termios.TIOCSCTTY, 0)
        os.dup2(terminal, 0)
        os.dup2(terminal, 2)
        out = os.open(request["stdout"], os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.dup2(out, 1)
        os.close(controller)
        os.execve(request["argv"][0], request["argv"], request["env"])
    shown = b""
    answer = request["answer"]
    status = None
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        ready, _, _ = select.select([controller], [], [], 0.1)
        if ready:
            shown += os.read(controller, 4096)
        if answer is not None and b"Api key:" in shown:
            if not termios.tcgetattr(terminal)[3] & termios.ECHO:
                os.write(controller, (answer + "\\n").encode())
                answer = None
        finished, status = os.waitpid(pid, os.WNOHANG)
        if finished:
            break
    else:
        os.kill(pid, 9)
        _, status = os.waitpid(pid, 0)
    while select.select([controller], [], [], 0.1)[0]:
        chunk = os.read(controller, 4096)
        if not chunk:
            break
        shown += chunk
    code = os.waitstatus_to_exitcode(status)
    print(json.dumps({"code": code, "shown": shown.decode(errors="replace")}))
    """
)


_CHILD = textwrap.dedent(
    """
    import contextlib
    import sys
    from typing import Any
    from unittest.mock import patch

    import requests

    from graftpunk.plugins.cli_plugin import PluginParamSpec, SitePlugin, command
    from graftpunk.tokens import Token, TokenConfig

    mode, site = sys.argv[1], sys.argv[2]
    secret = mode != "plain"


    class P(SitePlugin):
        site_name = "keyshop"
        session_name = "keyshop"
        help_text = "t"
        requires_session = mode == "token"
        base_url = "http://127.0.0.1:9" if mode == "token" else site
        if mode == "token":
            token_config = TokenConfig(
                tokens=(
                    Token(name="X-T", source="page", pattern="(x)", page_url="/t",
                          extraction="http"),
                )
            )

        @command(
            help="e",
            params=[PluginParamSpec.option("api_key", required=True, secret=secret)],
        )
        def echo(self, ctx: Any, api_key: str) -> dict[str, Any]:
            return {"got_len": len(api_key)}


    # "escape": an exception no handler catches leaves run_plugin_command, whose
    # frame holds the option values, so Typer's pretty traceback would print them
    # among the frame's locals.
    escape = patch(
        "graftpunk.cli.plugin_runtime.build_observe_context",
        side_effect=RuntimeError("observe broke"),
    )
    with (
        patch("graftpunk.cli.plugin_commands.discover_all_plugins", return_value=(P(),)),
        patch(
            "graftpunk.cli.plugin_runtime.load_session_for_api_resolved",
            return_value=(requests.Session(), "keyshop"),
        ),
        escape if mode == "escape" else contextlib.nullcontext(),
    ):
        from graftpunk.cli.main import app

        sys.argv = ["gp", "keyshop", "echo", *sys.argv[3:]]
        app()
    """
)


@pytest.fixture
def child(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    """The child script running the real gp app, and an env with a scratch HOME."""
    script = tmp_path / "child.py"
    script.write_text(_CHILD)
    home = tmp_path / "home"
    home.mkdir()
    env = {
        **{k: v for k, v in os.environ.items() if k not in ("FORCE_COLOR",)},
        "HOME": str(home),
        "GRAFTPUNK_CONFIG_DIR": str(home / "config"),
        "NO_COLOR": "1",
        "TERM": "dumb",
        "COLUMNS": "200",
        "PYTHONPATH": os.pathsep.join(sys.path),
    }
    return script, env


class TestPrompt:
    def test_a_missing_secret_is_prompted_on_stderr_and_stdout_holds_only_the_result(
        self, site: str, tmp_path: Path, child: tuple[Path, dict[str, str]]
    ) -> None:
        script, env = child
        out = tmp_path / "out.json"
        code, shown = _pty_run([sys.executable, str(script), "secret", site], env, out, API_KEY)

        assert code == 0, shown
        assert "Api key:" in shown
        assert API_KEY not in shown
        assert json.loads(out.read_text()) == {"got_len": len(API_KEY)}

    def test_a_plain_required_option_is_not_prompted_for_on_a_tty(
        self, site: str, tmp_path: Path, child: tuple[Path, dict[str, str]]
    ) -> None:
        script, env = child
        out = tmp_path / "out.json"
        code, shown = _pty_run([sys.executable, str(script), "plain", site], env, out, None)

        assert code == 2, shown
        assert "Missing option '--api-key'" in strip_ansi(shown)
        assert "Api key:" not in shown
        assert out.read_text() == ""

    def test_a_given_secret_is_not_prompted_for(self, site: str) -> None:
        spec = PluginParamSpec.option("api_key", required=True, secret=True)
        plugin = _plugin(site, spec)

        with patch("graftpunk.cli.plugin_runtime.format_output"):
            result = _invoke(plugin, ["echo", "--api-key", API_KEY])

        assert result.exit_code == 0, strip_ansi(result.output)
        assert "Api key:" not in strip_ansi(result.output)
        assert plugin.received == [API_KEY]  # ty: ignore[unresolved-attribute]

    def test_a_missing_required_secret_is_an_error_without_a_tty(self, site: str) -> None:
        spec = PluginParamSpec.option("api_key", required=True, secret=True)
        plugin = _plugin(site, spec)

        # CliRunner's stdin is not a terminal.
        result = _invoke(plugin, ["echo"], input=f"{API_KEY}\n")

        assert result.exit_code == 2
        assert "Missing option '--api-key'" in strip_ansi(result.output)
        assert "Api key:" not in strip_ansi(result.output)
        assert plugin.received == []  # ty: ignore[unresolved-attribute]

    def test_stdin_that_is_none_or_closed_is_not_a_terminal(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from graftpunk.cli.command_factory import _stdin_is_terminal

        monkeypatch.setattr(sys, "stdin", None)
        assert _stdin_is_terminal() is False
        closed = (tmp_path / "in").open("w")
        closed.close()
        monkeypatch.setattr(sys, "stdin", closed)
        assert _stdin_is_terminal() is False


class TestNothingEscapesTheRealApp:
    def test_a_token_page_that_cannot_be_fetched_is_an_error_without_the_secret(
        self, site: str, child: tuple[Path, dict[str, str]]
    ) -> None:
        script, env = child
        result = subprocess.run(  # noqa: S603 - argv is this test's own interpreter and script
            [sys.executable, str(script), "token", site, "--api-key", API_KEY],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

        stderr = strip_ansi(result.stderr)
        assert result.returncode == 1, stderr
        assert "Token extraction failed:" in stderr
        assert "Traceback" not in stderr
        _assert_no_form(strip_ansi(result.stdout) + stderr)

    def test_an_uncaught_exception_prints_no_frame_locals(
        self, site: str, child: tuple[Path, dict[str, str]]
    ) -> None:
        script, env = child
        result = subprocess.run(  # noqa: S603 - argv is this test's own interpreter and script
            [sys.executable, str(script), "escape", site, "--api-key", API_KEY],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

        stderr = strip_ansi(result.stderr)
        assert result.returncode == 1, stderr
        assert "RuntimeError: observe broke" in stderr
        # Typer's locals panel prints each local as "name = value"; the
        # run_plugin_command frame holds both of these.
        assert "kwargs = " not in stderr
        assert "option_secrets = " not in stderr
        _assert_no_form(strip_ansi(result.stdout) + stderr)


class TestLogLines:
    def test_log_secrets_scrubs_every_value_of_a_line_in_both_formats(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from graftpunk.logging import configure_logging, get_logger, log_secrets

        log = get_logger("t")
        for json_output in (True, False):
            configure_logging(level="DEBUG", json_output=json_output)
            with log_secrets([API_KEY]):
                log.warning(
                    "inside",
                    url=f"http://h.test/k?key={_forms(API_KEY)['quote_plus']}",
                    nested={"k": [API_KEY]},
                )
                try:
                    raise RuntimeError(f"bad key {API_KEY}")
                except RuntimeError:
                    log.exception("failed_inside")
            text = strip_ansi(capsys.readouterr().err)
            _assert_no_form(text)
            assert REDACTED in text
            assert "Traceback (most recent call last):" in text
            assert f"RuntimeError: bad key {REDACTED}" in text

    def test_outside_the_block_nothing_is_scrubbed(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from graftpunk.logging import configure_logging, get_logger, log_secrets

        configure_logging(level="DEBUG", json_output=True)
        with log_secrets([API_KEY]):
            pass
        get_logger("t").warning("outside", value=API_KEY)
        assert json.loads(capsys.readouterr().err)["value"] == API_KEY

    def test_a_programs_own_structlog_configuration_gets_scrubbed_lines(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        import structlog

        from graftpunk.logging import get_logger, log_secrets

        structlog.configure(
            processors=[structlog.processors.JSONRenderer()],
            logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        )
        log = get_logger("t").bind(where=f"k={API_KEY}")
        with log_secrets([API_KEY]):
            log.warning("inside", value=API_KEY)
            try:
                raise RuntimeError(f"bad key {API_KEY}")
            except RuntimeError:
                log.exception("failed_inside")
        inside, failed = (json.loads(line) for line in capsys.readouterr().err.splitlines())
        assert inside == {"where": f"k={REDACTED}", "value": REDACTED, "event": "inside"}
        assert failed["exception"].startswith("Traceback (most recent call last):")
        assert failed["exception"].endswith(f"RuntimeError: bad key {REDACTED}")
        _assert_no_form(json.dumps(failed))

    def test_the_unconfigured_logger_scrubs_too(self, capsys: pytest.CaptureFixture[str]) -> None:
        from graftpunk.logging import get_logger, log_secrets

        with log_secrets([API_KEY]):
            get_logger("t").warning("inside", value=API_KEY)
        text = strip_ansi(capsys.readouterr().err)
        assert "inside" in text
        _assert_no_form(text)

    def test_a_handlers_own_graftpunk_log_line_is_scrubbed(
        self, site: str, gp_json_logs: JsonLogs
    ) -> None:
        read_logs = gp_json_logs()
        from graftpunk.logging import get_logger

        spec = PluginParamSpec.option("api_key", secret=True)

        class Logs(SitePlugin):
            site_name = "keyshop"
            session_name = "keyshop"
            help_text = "t"
            requires_session = False
            base_url = site

            @command(help="Log it", params=[spec])
            def chatty(self, ctx: Any, api_key: str | None = None) -> dict[str, Any]:
                get_logger("plugin").warning("navigating", url=f"{site}/k/{api_key}")
                return {}

        with patch("graftpunk.cli.plugin_runtime.format_output"):
            result = _invoke(Logs(), ["chatty", "--api-key", API_KEY])

        assert result.exit_code == 0, strip_ansi(result.output)
        (line,) = [e for e in read_logs() if e["event"] == "navigating"]
        assert line["url"] == f"{site}/k/{REDACTED}"

    def test_graftpunk_client_scrubs_its_403_retry_line_and_its_log_lines(self, site: str) -> None:
        from graftpunk.client import GraftpunkClient
        from graftpunk.logging import get_logger
        from graftpunk.tokens import Token, TokenConfig

        spec = PluginParamSpec.option("api_key", secret=True)

        class TokenPlugin(SitePlugin):
            site_name = "keyshop"
            session_name = "keyshop"
            help_text = "t"
            base_url = site
            token_config = TokenConfig(
                tokens=(Token(name="X-T", source="page", pattern="(x)", page_url="/t"),)
            )

            @command(help="Forbidden", params=[spec])
            def forbidden(self, ctx: Any, api_key: str | None = None) -> dict[str, Any]:
                get_logger("plugin").warning("sending", key=api_key)
                url = f"{self.base_url}/forbidden"
                ctx.session.get(url, params={"key": api_key}).raise_for_status()
                return {}

        with (
            patch("graftpunk.client.get_plugin", return_value=TokenPlugin()),
            patch(
                "graftpunk.client.load_session_for_api_resolved",
                return_value=(requests.Session(), "keyshop"),
            ),
            patch("graftpunk.client.prepare_session"),
            patch("graftpunk.client.clear_cached_tokens"),
            capture_logs() as logs,
            pytest.raises(requests.HTTPError),
        ):
            GraftpunkClient("keyshop").execute("forbidden", api_key=API_KEY)

        (retry,) = [e for e in logs if e["event"] == "token_403_retry"]
        assert retry["url"] == f"{site}/forbidden?key={REDACTED}"

    def test_graftpunk_client_sets_the_log_scope(self, site: str, gp_json_logs: JsonLogs) -> None:
        read_logs = gp_json_logs()
        from graftpunk.client import GraftpunkClient
        from graftpunk.logging import get_logger

        spec = PluginParamSpec.option("api_key", secret=True)

        class Logs(SitePlugin):
            site_name = "keyshop"
            session_name = "keyshop"
            help_text = "t"
            requires_session = False
            base_url = site

            @command(help="Log it", params=[spec])
            def chatty(self, ctx: Any, api_key: str | None = None) -> dict[str, Any]:
                get_logger("plugin").warning("sending", key=api_key)
                return {}

        with patch("graftpunk.client.get_plugin", return_value=Logs()):
            GraftpunkClient("keyshop").execute("chatty", api_key=API_KEY)

        (line,) = [e for e in read_logs() if e["event"] == "sending"]
        assert line["key"] == REDACTED


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
                PluginParamSpec.option("on", secret=True, click_kwargs={"is_flag": True}),
                "cannot be a flag",
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


def _yaml_plugin(
    tmp_path: Path, site: str, param: str, *, url: str = "/home", headers: str = ""
) -> SitePlugin:
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
    url: "{url}"
    raise_for_status: false
{headers}    params:
{param}
"""
    )
    bundle = parse_yaml_plugin(path)
    return create_yaml_site_plugin(bundle.config, bundle.commands, bundle.headers)


SECRET_PARAM = "      - name: api_key\n        required: true\n        secret: true\n"  # noqa: S105 - a YAML snippet, not a password


class TestYaml:
    def test_a_secret_yaml_param_is_scrubbed_from_the_har_and_the_logs(
        self, site: str, tmp_path: Path, gp_json_logs: JsonLogs
    ) -> None:
        read_logs = gp_json_logs()
        plugin = _yaml_plugin(tmp_path, site, SECRET_PARAM)
        har, _ = _observed_run(tmp_path / "runs", plugin, ["--api-key", API_KEY])

        _assert_no_form(har)
        logs = read_logs()
        _assert_no_form(json.dumps(logs))
        (request,) = [e for e in logs if e["event"] == "yaml_plugin_request"]
        assert request["params"] == {"api_key": REDACTED}

    def test_a_secret_in_a_yaml_url_path_is_scrubbed_from_the_logs(
        self, site: str, tmp_path: Path, gp_json_logs: JsonLogs
    ) -> None:
        read_logs = gp_json_logs()
        plugin = _yaml_plugin(tmp_path, site, SECRET_PARAM, url="/home/{api_key}")
        with patch("graftpunk.cli.plugin_runtime.format_output"):
            result = _invoke(plugin, ["search", "--api-key", API_KEY])

        assert result.exit_code == 0, strip_ansi(result.output)
        logs = read_logs()
        _assert_no_form(json.dumps(logs))
        (request,) = [e for e in logs if e["event"] == "yaml_plugin_request"]
        (not_json,) = [e for e in logs if e["event"] == "json_parse_failed_returning_text"]
        assert request["url"] == f"{site}/home/{REDACTED}"
        assert not_json["url"] == f"{site}/home/{REDACTED}"

    def test_a_plain_yaml_param_is_recorded_as_before(self, site: str, tmp_path: Path) -> None:
        plugin = _yaml_plugin(tmp_path, site, "      - name: api_key\n")
        har, _ = _observed_run(tmp_path / "runs", plugin, ["--api-key", API_KEY])

        assert _forms(API_KEY)["quote_plus"] in har

    def test_a_secret_yaml_param_is_optional_to_click_and_checked_by_its_callback(
        self, site: str, tmp_path: Path
    ) -> None:
        plugin = _yaml_plugin(tmp_path, site, SECRET_PARAM)
        (spec,) = plugin.get_commands()[0].params
        assert spec.secret
        result = _invoke(plugin, ["search"])

        assert result.exit_code == 2
        assert "Missing option '--api-key'" in strip_ansi(result.output)

    @pytest.mark.parametrize(
        ("param", "message"),
        [
            ("      - name: pin\n        type: int\n        secret: true\n", "str option"),
            ("      - name: key\n        is_option: false\n        secret: true\n", "str option"),
            ("      - name: key\n        secret: 'yes'\n", "must be true or false"),
            ("      - name: key\n        envvar: ''\n", "'envvar' must name"),
            ("      - name: key\n        envvar: 3\n", "'envvar' must name"),
            ("      - name: key\n        is_option: false\n        envvar: K\n", "on an option"),
        ],
    )
    def test_a_yaml_param_that_could_show_its_value_is_refused(
        self, site: str, tmp_path: Path, param: str, message: str
    ) -> None:
        with pytest.raises(PluginError, match=message):
            _yaml_plugin(tmp_path, site, param)


@pytest.fixture
def config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A scratch graftpunk config directory, its env file empty, the variable unset."""
    directory = tmp_path / "config"
    directory.mkdir()
    monkeypatch.setenv("GRAFTPUNK_CONFIG_DIR", str(directory))
    monkeypatch.delenv(ENV_NAME, raising=False)
    workstation_env._invalidate_cache()
    yield directory
    workstation_env._invalidate_cache()


def _env_default_param(default: str) -> str:
    return f"      - name: api_key\n        secret: true\n        default: '{default}'\n"


class TestYamlEnvironment:
    def test_a_placeholder_default_is_resolved_when_the_command_runs_and_scrubbed(
        self, site: str, tmp_path: Path, config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Loading the plugin with the variable unset is fine: nothing resolves yet.
        plugin = _yaml_plugin(tmp_path, site, _env_default_param(f"${{{ENV_NAME}}}"))
        monkeypatch.setenv(ENV_NAME, API_KEY)
        har, _ = _observed_run(tmp_path / "runs", plugin, [])

        url = json.loads(har)["log"]["entries"][0]["request"]["url"]
        assert url == f"{site}/home?api_key={REDACTED}"
        _assert_no_form(har)

    def test_a_placeholder_default_reads_the_workstation_env_file(
        self, site: str, tmp_path: Path, config_dir: Path
    ) -> None:
        (config_dir / "env").write_text(f"{ENV_NAME}={API_KEY}\n")
        plugin = _yaml_plugin(tmp_path, site, _env_default_param(f"${{{ENV_NAME}}}"))
        har, _ = _observed_run(tmp_path / "runs", plugin, [])

        url = json.loads(har)["log"]["entries"][0]["request"]["url"]
        assert url == f"{site}/home?api_key={REDACTED}"
        _assert_no_form(har)

    def test_an_unset_variable_with_no_default_is_a_usage_error_naming_it(
        self, site: str, tmp_path: Path, config_dir: Path
    ) -> None:
        plugin = _yaml_plugin(tmp_path, site, _env_default_param(f"${{{ENV_NAME}}}"))
        result = _invoke(plugin, ["search"])

        output = strip_ansi(result.output)
        assert result.exit_code == 2
        assert f"Environment variable ${ENV_NAME} is not set" in output
        assert "Traceback" not in output
        assert "${" + ENV_NAME + "}" not in output.replace(f"${ENV_NAME} is", "")

    def test_an_unset_variable_falls_back_to_its_default(
        self, site: str, tmp_path: Path, config_dir: Path
    ) -> None:
        param = f"      - name: api_key\n        default: '${{{ENV_NAME}:fallback-key}}'\n"
        plugin = _yaml_plugin(tmp_path, site, param)
        har, _ = _observed_run(tmp_path / "runs", plugin, [])

        url = json.loads(har)["log"]["entries"][0]["request"]["url"]
        assert url == f"{site}/home?api_key=fallback-key"

    def test_a_given_value_skips_the_placeholder(
        self, site: str, tmp_path: Path, config_dir: Path
    ) -> None:
        plugin = _yaml_plugin(tmp_path, site, _env_default_param(f"${{{ENV_NAME}}}"))
        har, _ = _observed_run(tmp_path / "runs", plugin, ["--api-key", API_KEY])

        url = json.loads(har)["log"]["entries"][0]["request"]["url"]
        assert url == f"{site}/home?api_key={REDACTED}"

    @pytest.mark.parametrize("set_it", [False, True])
    def test_help_resolves_nothing_and_shows_no_value(
        self,
        site: str,
        tmp_path: Path,
        config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        set_it: bool,
    ) -> None:
        if set_it:
            monkeypatch.setenv(ENV_NAME, API_KEY)
        plugin = _yaml_plugin(tmp_path, site, _env_default_param(f"${{{ENV_NAME}}}"))
        result = _invoke(plugin, ["search", "--help"])

        output = strip_ansi(result.output)
        assert result.exit_code == 0, output
        assert "--api-key" in output
        _assert_no_form(output)

    def test_a_yaml_envvar_supplies_the_value_and_is_scrubbed(
        self, site: str, tmp_path: Path, config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        param = f"      - name: api_key\n        secret: true\n        envvar: {ENV_NAME}\n"
        plugin = _yaml_plugin(tmp_path, site, param)
        help_text = strip_ansi(_invoke(plugin, ["search", "--help"]).output)
        assert ENV_NAME in help_text
        monkeypatch.setenv(ENV_NAME, API_KEY)
        har, _ = _observed_run(tmp_path / "runs", plugin, [])

        url = json.loads(har)["log"]["entries"][0]["request"]["url"]
        assert url == f"{site}/home?api_key={REDACTED}"
        _assert_no_form(har)

    def test_a_header_placeholder_takes_the_same_fallback(
        self, site: str, tmp_path: Path, config_dir: Path
    ) -> None:
        headers = f'    headers:\n      X-Token: "${{{ENV_NAME}:anonymous}}"\n'
        plugin = _yaml_plugin(tmp_path, site, "      - name: page\n", headers=headers)
        har, _ = _observed_run(tmp_path / "runs", plugin, [])

        request = json.loads(har)["log"]["entries"][0]["request"]
        assert {"name": "X-Token", "value": "anonymous"} in request["headers"]


class TestExpandEnvVars:
    def test_set_unset_and_fallbacks(
        self, config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from graftpunk.plugins.yaml_loader import expand_env_vars

        assert expand_env_vars(f"Bearer ${{{ENV_NAME}:anon}}") == "Bearer anon"
        assert expand_env_vars(f"${{{ENV_NAME}:}}") == ""
        assert expand_env_vars(f"${{{ENV_NAME}:a:b}}") == "a:b"
        with pytest.raises(PluginError, match=rf"\${ENV_NAME} is not set"):
            expand_env_vars(f"${{{ENV_NAME}}}")
        monkeypatch.setenv(ENV_NAME, "real")
        assert expand_env_vars(f"${{{ENV_NAME}:anon}}") == "real"
        assert expand_env_vars(f"${{{ENV_NAME}}}") == "real"


class TestRecorderLogs:
    def _capture(self, **kwargs: Any) -> Any:
        from graftpunk.observe.http_capture import HttpCaptureBackend

        session = requests.Session()
        return session, HttpCaptureBackend(session, redact=[API_KEY], **kwargs)

    def test_a_response_after_stop_logs_its_url_scrubbed(self, site: str) -> None:
        session, capture = self._capture()
        response = session.get(f"{site}/home", params={"key": API_KEY})
        capture.stop_capture()
        with capture_logs() as logs:
            capture.record(response, 0.0, 0.0)

        (dropped,) = [e for e in logs if e["event"] == "http_capture_response_after_stop"]
        assert dropped["url"].endswith(f"/home?key={REDACTED}")

    def test_a_response_that_cannot_be_recorded_logs_its_url_scrubbed(self, site: str) -> None:
        session, capture = self._capture()
        response = session.get(f"{site}/home", params={"key": API_KEY})
        with (
            patch(
                "graftpunk.observe.http_capture.har_entry_for_response",
                side_effect=RuntimeError("no"),
            ),
            capture_logs() as logs,
        ):
            capture.record(response, 0.0, 0.0)

        (failed,) = [e for e in logs if e["event"] == "http_capture_record_failed"]
        assert failed["url"].endswith(f"/home?key={REDACTED}")

    def test_a_failed_request_that_cannot_be_recorded_logs_its_url_scrubbed(
        self, site: str
    ) -> None:
        session, capture = self._capture()
        request = requests.Request("GET", f"{site}/home", params={"key": API_KEY}).prepare()
        with (
            patch(
                "graftpunk.observe.http_capture.har_entry_for_failed_request",
                side_effect=RuntimeError("no"),
            ),
            capture_logs() as logs,
        ):
            capture.record_failure(request, 0.0, 0.0, ConnectionError("refused"))

        (failed,) = [e for e in logs if e["event"] == "http_capture_record_failed"]
        assert failed["url"].endswith(f"/home?key={REDACTED}")

    def test_a_streamed_request_body_warning_logs_its_url_scrubbed(
        self, site: str, tmp_path: Path
    ) -> None:
        session, capture = self._capture(bodies_dir=tmp_path / "bodies")
        capture.start_capture()
        with capture_logs() as logs:
            session.post(
                f"{site}/api/search",
                params={"key": API_KEY},
                data=(chunk for chunk in [b'{"a": 1}']),
            )
        capture.stop_capture()

        (warning,) = [e for e in logs if e["event"] == "post_data_unavailable"]
        assert warning["url"].endswith(f"/api/search?key={REDACTED}")

    def test_a_failed_streamed_request_body_warning_logs_its_url_scrubbed(
        self, site: str, tmp_path: Path
    ) -> None:
        _, capture = self._capture(bodies_dir=tmp_path / "bodies")
        request = requests.Request(
            "POST",
            f"{site}/api/search",
            params={"key": API_KEY},
            data=(chunk for chunk in [b"{}"]),
        ).prepare()
        with capture_logs() as logs:
            capture.record_failure(request, 0.0, 0.0, ConnectionError("refused"))

        (warning,) = [e for e in logs if e["event"] == "post_data_unavailable"]
        assert warning["url"].endswith(f"/api/search?key={REDACTED}")
