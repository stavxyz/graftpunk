"""A requests.HTTPError a command lets escape is reported with request_json's advice (#203)."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import requests

from graftpunk.plugins import SitePlugin, command
from tests.unit.cli_harness import invoke_plugin_app, strip_ansi

_BASE = "https://myshop.example"


def _response(status: int, headers: dict[str, str] | None = None) -> requests.Response:
    response = requests.Response()
    response.status_code = status
    response.reason = "Reason"
    response.url = f"{_BASE}/api/orders?token=hunter2"
    response.headers.update({"Content-Type": "application/json", **(headers or {})})
    response._content = b'{"detail": "body text"}'
    response.request = requests.Request("GET", response.url).prepare()
    return response


class _PythonPlugin(SitePlugin):
    site_name = "myshop"
    session_name = "myshop"
    help_text = "test"
    base_url = _BASE
    requires_session = False

    @command(help="Fetch orders")
    def orders(self, ctx: Any) -> dict:
        ctx.session.get(f"{_BASE}/api/orders", params={"token": "hunter2"}).raise_for_status()
        return {}


def _yaml_plugin(tmp_path: Path) -> SitePlugin:
    from graftpunk.plugins.yaml_loader import parse_yaml_plugin
    from graftpunk.plugins.yaml_plugin import create_yaml_site_plugin

    path = tmp_path / "myshop.yaml"
    path.write_text(
        f"""
site_name: myshop
base_url: "{_BASE}"
requires_session: false
commands:
  orders:
    help: "Fetch orders"
    url: "/api/orders"
"""
    )
    bundle = parse_yaml_plugin(path)
    return create_yaml_site_plugin(bundle.config, bundle.commands, bundle.headers)


@pytest.fixture(params=["python", "yaml"])
def plugin(request: pytest.FixtureRequest, tmp_path: Path) -> SitePlugin:
    return _PythonPlugin() if request.param == "python" else _yaml_plugin(tmp_path)


def _run(plugin: SitePlugin, response: requests.Response) -> tuple[int, str]:
    with patch.object(requests.Session, "request", return_value=response):
        result = invoke_plugin_app(plugin, ["myshop", "orders"])
    return result.exit_code, " ".join(strip_ansi(result.output).split())


@pytest.mark.parametrize(
    ("status", "headers", "message"),
    [
        (401, {}, "The site rejected the cached session (401 on GET /api/orders)."),
        (403, {}, "Run: gp myshop login"),
        (
            429,
            {"Retry-After": "30"},
            "GET /api/orders returned 429: the site is rate limiting requests. "
            "Try again in 30 seconds.",
        ),
        (
            429,
            {},
            "GET /api/orders returned 429: the site is rate limiting requests. "
            "Wait a while and try again.",
        ),
        (
            429,
            {"Retry-After": "9" * 5000},
            "GET /api/orders returned 429: the site is rate limiting requests. "
            "Wait a while and try again.",
        ),
        (503, {}, "GET /api/orders returned 503: the site is having trouble. Try again later."),
        (500, {}, "GET /api/orders returned 500: the site is having trouble. Try again later."),
        (404, {}, "GET /api/orders returned 404"),
    ],
    ids=["401", "403", "429-retry-after", "429", "429-5000-digits", "503", "500", "404"],
)
def test_an_escaped_http_error_prints_the_request_json_advice(
    plugin: SitePlugin, status: int, headers: dict[str, str], message: str
) -> None:
    exit_code, output = _run(plugin, _response(status, headers))
    assert exit_code == 1
    assert message in output
    assert "Command failed" not in output
    assert "Traceback" not in output
    assert "hunter2" not in output
    assert "body text" not in output


def test_the_escaped_http_error_is_logged_at_debug_without_a_traceback(
    plugin: SitePlugin,
) -> None:
    from structlog.testing import capture_logs

    with capture_logs() as logs:
        exit_code, _output = _run(plugin, _response(503))
    assert exit_code == 1
    assert not [e for e in logs if e["event"] == "plugin_command_failed"]
    (event,) = [e for e in logs if e["event"] == "plugin_command_http_error"]
    assert event["log_level"] == "debug"
    assert event["status"] == 503
    assert "exc_info" not in event


def test_a_2xx_yaml_response_is_not_an_error(tmp_path: Path) -> None:
    exit_code, output = _run(_yaml_plugin(tmp_path), _response(200))
    assert exit_code == 0, output


def test_an_http_error_without_a_response_stays_a_crash() -> None:
    class _Bare(_PythonPlugin):
        @command(help="Fetch orders")
        def orders(self, ctx: Any) -> dict:
            raise requests.HTTPError("no response attached")

    result = invoke_plugin_app(_Bare(), ["myshop", "orders"])
    assert result.exit_code == 1
    assert "Command failed: no response attached" in strip_ansi(result.output)
