"""graftpunk changes no process-global cookie state (#253).

Each test runs one graftpunk code path in a fresh interpreter, then checks that
``http.cookiejar.DefaultCookiePolicy`` is still the standard library's class and
that a plain ``requests.Session`` holding a ``Secure`` cookie for localhost
sends no ``Cookie`` header to ``http://localhost/``. A fresh interpreter is the
only honest place to check: once this worker has imported ``httpie.compat`` and
graftpunk's modules, a later import does nothing, so a regression that imports
HTTPie at module scope would go unseen here.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.unit.test_lite_cli_hints import _cache_fixture_session

_REPO = Path(__file__).parent.parent.parent

_CHECK = """
import json
from http import cookiejar

import requests


def _report(**facts):
    plain = requests.Session()
    plain.cookies.set("tok", "abc", domain="localhost.local", secure=True)
    header = plain.prepare_request(requests.Request("GET", "http://localhost/")).headers.get(
        "Cookie"
    )
    policy = cookiejar.DefaultCookiePolicy
    print(json.dumps({
        "policy": f"{policy.__module__}.{policy.__qualname__}",
        "plain_cookie": header,
        **facts,
    }))
"""

_SERVER = """
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class _Echo(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b'{"ok": true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


_server = ThreadingHTTPServer(("127.0.0.1", 0), _Echo)
threading.Thread(target=_server.serve_forever, daemon=True).start()
URL = f"http://127.0.0.1:{_server.server_address[1]}/"
"""

_PATHS = {
    "import graftpunk": """
import graftpunk
_report()
""",
    "load_session": """
from graftpunk import load_session
session = load_session("myshop")
_report(loaded=type(session).__name__)
""",
    "load_session_for_api": """
from graftpunk import load_session_for_api
session = load_session_for_api("myshop")
_report(loaded=type(session).__name__)
""",
    "construct BrowserSession": """
from graftpunk import BrowserSession
session = BrowserSession(backend="nodriver", headless=True)
_report(loaded=type(session).__name__)
""",
    "plugin command": """
from tests.unit.cli_harness import invoke_plugin_app
from tests.unit.test_lite_cli_hints import _shop_plugin
result = invoke_plugin_app(_shop_plugin("nodriver"), ["myshop", "items"])
_report(exit_code=result.exit_code, output=result.output)
""",
    "gp session export": """
import os
from typer.testing import CliRunner
from graftpunk.cli.main import app
target = os.path.join(os.environ["HOME"], "export.json")
result = CliRunner().invoke(app, ["session", "export", "myshop", "-o", target])
_report(exit_code=result.exit_code, output=result.output, exported=os.path.exists(target))
""",
    "gp http": _SERVER
    + """
from typer.testing import CliRunner
from graftpunk.cli.main import app
result = CliRunner().invoke(app, ["http", "get", URL, "--session", "myshop"])
_report(exit_code=result.exit_code, output=result.output)
""",
    "gp http --no-session": _SERVER
    + """
from typer.testing import CliRunner
from graftpunk.cli.main import app
result = CliRunner().invoke(app, ["http", "get", URL, "--no-session"])
_report(exit_code=result.exit_code, output=result.output)
""",
}


def _run(script: str, tmp_path: Path) -> dict:
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", _CHECK + script],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(_REPO)},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout.splitlines()[-1])


@pytest.fixture
def _cached_session(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The committed fixture cached as ``myshop``; HOME, XDG, graftpunk's and
    HTTPie's config dirs all inside *tmp_path*."""
    _cache_fixture_session(tmp_path, monkeypatch)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    monkeypatch.delenv("GRAFTPUNK_SESSION", raising=False)
    monkeypatch.setenv("NO_COLOR", "1")
    (tmp_path / "home").mkdir()


@pytest.mark.usefixtures("_cached_session")
@pytest.mark.parametrize("path", sorted(_PATHS))
def test_a_graftpunk_path_leaves_the_global_cookie_policy_alone(path: str, tmp_path: Path) -> None:
    facts = _run(_PATHS[path], tmp_path)

    assert facts.get("exit_code", 0) == 0, facts
    assert facts.get("exported", True), facts
    assert facts["policy"] == "http.cookiejar.DefaultCookiePolicy", facts
    assert facts["plain_cookie"] is None, facts


@pytest.mark.usefixtures("_cached_session")
def test_the_check_sees_httpies_global_policy(tmp_path: Path) -> None:
    """The check above would catch the change: importing httpie.compat makes it."""
    facts = _run("import httpie.compat\n_report()\n", tmp_path)

    assert facts["policy"] == "httpie.cookies.HTTPieCookiePolicy", facts
    assert facts["plain_cookie"] == "tok=abc", facts


def test_importing_httpie_puts_back_whatever_policy_was_there(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from http import cookiejar

    from graftpunk.cookies import importing_httpie

    class Before(cookiejar.DefaultCookiePolicy):
        pass

    class During(cookiejar.DefaultCookiePolicy):
        pass

    monkeypatch.setattr(cookiejar, "DefaultCookiePolicy", Before)
    with pytest.raises(RuntimeError, match="inside"), importing_httpie():
        cookiejar.DefaultCookiePolicy = During
        raise RuntimeError("inside")

    assert cookiejar.DefaultCookiePolicy is Before


_REBIND = """
import json
import sys
from http import cookiejar

import dill
import requests


class Mine(cookiejar.DefaultCookiePolicy):
    def return_ok(self, cookie, request):
        return False


def rebind():
    if kind == "httpie":
        import httpie.compat  # noqa: F401
    else:
        cookiejar.DefaultCookiePolicy = Mine


# Created while the global is still the standard library's class.
early = requests.cookies.RequestsCookieJar()
order, kind = sys.argv[1], sys.argv[2]
if order == "before":
    rebind()
from graftpunk.cookies import BrowserCookiePolicy, BrowserCookieSession, use_browser_cookie_policy
from graftpunk.graftpunk_session import GraftpunkSession
if order == "after":
    rebind()


def cookie(session):
    return session.prepare_request(requests.Request("GET", "http://localhost/")).headers.get(
        "Cookie"
    )


class Chosen(cookiejar.DefaultCookiePolicy):
    pass


built = GraftpunkSession()
built.cookies.set("tok", "1", domain="localhost.local", secure=True)
# Unpickling resolves http.cookiejar.DefaultCookiePolicy through the global.
pickled = BrowserCookieSession()
pickled.cookies.set("tok", "1", domain="localhost.local", secure=True)
reloaded = dill.loads(dill.dumps(pickled))
handed = use_browser_cookie_policy(requests.cookies.RequestsCookieJar())
chosen = requests.cookies.RequestsCookieJar(policy=Chosen())
use_browser_cookie_policy(chosen)
use_browser_cookie_policy(early)
base = BrowserCookiePolicy.__mro__[1]
print(json.dumps({
    "base": f"{base.__module__}.{base.__qualname__}",
    "built": type(built.cookies.get_policy()).__qualname__,
    "built_cookie": cookie(built),
    "reloaded": type(reloaded.cookies.get_policy()).__qualname__,
    "reloaded_cookie": cookie(reloaded),
    "handed": type(handed.get_policy()).__qualname__,
    "chosen": type(chosen.get_policy()).__qualname__,
    "early": type(early.get_policy()).__qualname__,
}))
"""


@pytest.mark.parametrize("order", ["before", "after"])
@pytest.mark.parametrize("kind", ["httpie", "mine"])
def test_a_replaced_global_policy_class_is_treated_the_same_in_either_import_order(
    order: str, kind: str, tmp_path: Path
) -> None:
    """A session graftpunk builds or reloads, and a jar it is handed with the
    default policy, get the browser policy whatever class the global names,
    whether the global was replaced before or after graftpunk was imported. So
    does a jar created before the global was replaced. A policy class a
    program set on its jar is kept."""
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", _REBIND, order, kind],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(_REPO), "HOME": str(tmp_path)},
    )
    assert result.returncode == 0, result.stdout + result.stderr

    assert json.loads(result.stdout.splitlines()[-1]) == {
        "base": "http.cookiejar.DefaultCookiePolicy",
        "built": "BrowserCookiePolicy",
        "built_cookie": "tok=1",
        "reloaded": "BrowserCookiePolicy",
        "reloaded_cookie": "tok=1",
        "handed": "BrowserCookiePolicy",
        "chosen": "Chosen",
        "early": "BrowserCookiePolicy",
    }
