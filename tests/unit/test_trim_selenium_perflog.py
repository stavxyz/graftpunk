"""scripts/trim_selenium_perflog.py, which writes the Selenium performance-log
fixture tests/unit/test_observe_headers.py replays."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).parents[2] / "scripts" / "trim_selenium_perflog.py"


def _script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("trim_selenium_perflog", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SITE = "http://127.0.0.1:54011"
EVENTS = [
    {
        "method": "Network.requestWillBeSent",
        "params": {
            "requestId": "OTHER1",
            "type": "Document",
            "request": {"url": "https://tracker.other.example/p", "method": "GET", "headers": {}},
        },
    },
    {
        "method": "Network.requestWillBeSent",
        "params": {
            "requestId": "AAAAAA1",
            "wallTime": 1.0,
            "type": "Document",
            "documentURL": f"{SITE}/login",
            "request": {
                "url": f"{SITE}/login",
                "method": "POST",
                "hasPostData": True,
                "postData": "username=someone&password=hunter2",
                "postDataEntries": [{"bytes": "dXNlcm5hbWU9"}],
                "headers": {
                    "Content-Type": "application/x-www-form-urlencoded",
                    "User-Agent": "Mozilla/5.0 Real/1",
                    "X-Account": "acct-123",
                },
            },
        },
    },
    {"method": "Network.dataReceived", "params": {"requestId": "AAAAAA1", "dataLength": 3}},
    {
        "method": "Network.requestWillBeSentExtraInfo",
        "params": {
            "requestId": "AAAAAA1",
            "headers": {
                "Host": "127.0.0.1:54011",
                "Content-Length": "33",
                "Cookie": "sid=real-session-value",
                "Authorization": "Bearer real-token",
                "Referer": f"{SITE}/login",
            },
        },
    },
    {
        "method": "Network.responseReceivedExtraInfo",
        "params": {
            "requestId": "AAAAAA1",
            "statusCode": 302,
            "headers": {"Location": "/home", "Set-Cookie": "sid=real-session-value; Path=/"},
        },
    },
]


def _run(tmp_path: Path, *extra: str) -> tuple[int, Path]:
    log = tmp_path / "perflog.json"
    log.write_text(json.dumps({"events": EVENTS}))
    out = tmp_path / "out.json"
    code = _script().main([*extra, str(log), str(out)])
    return code, out


def test_it_keeps_the_sites_events_in_order_and_replaces_values(tmp_path: Path) -> None:
    code, out = _run(tmp_path)
    assert code == 0
    events = json.loads(out.read_text())["events"]

    assert [e["method"] for e in events] == [
        "Network.requestWillBeSent",
        "Network.requestWillBeSentExtraInfo",
        "Network.responseReceivedExtraInfo",
    ]
    sent, extra, response = (e["params"] for e in events)
    assert {e["params"]["requestId"] for e in events} == {"AAAAA1"}
    assert sent["type"] == "Document"
    assert sent["request"]["url"] == "http://127.0.0.1:8080/login"
    assert sent["request"]["method"] == "POST"
    assert sent["request"]["hasPostData"] is True
    assert sent["request"]["headers"] == {
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": "Mozilla/5.0 Chrome/158.0.0.0",
        "X-Account": "placeholder",
    }
    assert extra["headers"] == {
        "Host": "127.0.0.1:8080",
        "Content-Length": "33",
        "Cookie": "placeholder=1",
        "Authorization": "placeholder",
        "Referer": "placeholder",
    }
    assert response["statusCode"] == 302
    assert response["headers"] == {"Location": "/home", "Set-Cookie": "placeholder=1"}


def test_it_writes_no_request_body_and_no_value_it_replaced(tmp_path: Path) -> None:
    _code, out = _run(tmp_path)
    text = out.read_text()
    assert "postData" not in text
    for secret in ("hunter2", "someone", "real-session-value", "real-token", "acct-123", "Real/1"):
        assert secret not in text


def test_it_refuses_to_replace_an_existing_output_unless_forced(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "out.json"
    out.write_text("keep me")
    code, _out = _run(tmp_path)
    assert code == 1
    assert out.read_text() == "keep me"
    assert "--force" in capsys.readouterr().err

    code, _out = _run(tmp_path, "--force")
    assert code == 0
    assert json.loads(out.read_text())["events"]
