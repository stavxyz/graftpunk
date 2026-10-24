"""A local ``http.server`` site for the HTTP observe tests (``gp http``, plugin commands).

Routes:
    GET  /home         HTML
    GET  /logo.png     PNG bytes
    GET  /no-ct        JSON with no Content-Type
    GET  /csv          ``application/csv``
    GET  /form         ``application/x-www-form-urlencoded``
    GET  /latin1       ``text/html; charset=iso-8859-1`` (``café`` in latin-1)
    GET  /latin1-bare  ``text/plain`` with no charset (``café`` in latin-1)
    GET  /truncated    ``Content-Length: 1000`` but only 10 bytes, then closes
    GET  /error        500 with a JSON body
    GET  /latin1-quoted ``text/html; charset="iso-8859-1"`` (the charset quoted)
    GET  /empty        200 with ``Content-Length: 0``
    GET  /slow         answers after 0.3 s; ``X-Received`` is when the request arrived
    GET  /whoami       echoes the request's ``Authorization`` header in a JSON body
    GET  /badgz-redirect   302 to /home, ``Content-Encoding: gzip``, a body that is not gzip
    GET  /trunc-redirect   302 to /home, ``Content-Length: 500`` but 10 bytes, then closes
    GET  /trunc-cookie     200 setting ``sid``, ``Content-Length: 1000`` but 10 bytes, then closes
    GET  /bad400       400 with a JSON error body
    GET  /forbidden    403 with a JSON error body, whatever the query string
    GET  /badgz-to-closed  302 with a body that is not gzip, to a port nothing listens on
    GET  /badgz-to-truncated  302 with a body that is not gzip, to /truncated
    GET  /needauth     401 unless ``Authorization: ok``, then 200
    GET  /fastredir    302 to /slow
    GET  /slowredir    302 to /home, sent after 0.3 s
    GET  /slow400      400 whose 12-byte body follows the headers by 1.5 s
    GET  /stall        200, ``Content-Length: 20``: 12 bytes, a 1.5 s pause, the rest
    HEAD (any path)    200 with no body
    POST /api/search   echoes the JSON body back
    POST /login        302 to /home, setting two cookies
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

LATIN1_TEXT = "café, déjà vu"


class _Site(BaseHTTPRequestHandler):
    def log_message(self, *_args: Any) -> None:
        pass

    def _send(self, status: int, body: bytes, content_type: str | None, **headers: Any) -> None:
        self.send_response(status)
        if content_type is not None:
            self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for name, value in headers.items():
            for one in value if isinstance(value, list) else [value]:
                self.send_header(name.replace("_", "-"), one)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        routes: dict[str, tuple[int, bytes, str | None]] = {
            "/home": (200, b"<h1>home</h1>", "text/html; charset=utf-8"),
            "/logo.png": (200, b"\x89PNG\r\n\x1a\n\x00\xff", "image/png"),
            "/no-ct": (200, b'{"a": 1}', None),
            "/csv": (200, b"a,b\n1,2\n", "application/csv"),
            "/form": (200, b"token=abc&x=1", "application/x-www-form-urlencoded"),
            "/latin1": (200, LATIN1_TEXT.encode("latin-1"), "text/html; charset=iso-8859-1"),
            "/latin1-bare": (200, LATIN1_TEXT.encode("latin-1"), "text/plain"),
            "/error": (500, b'{"error": "boom"}', "application/json"),
            "/latin1-quoted": (
                200,
                LATIN1_TEXT.encode("latin-1"),
                'text/html; charset="iso-8859-1"',
            ),
            "/empty": (200, b"", "application/json"),
        }
        if self.path in ("/truncated", "/trunc-cookie", "/trunc-redirect"):
            self._cut_short()
            return
        if self.path == "/bad400":
            self._send(400, b'{"error": "bad thing"}', "application/json")
            return
        if self.path.split("?")[0] == "/forbidden":
            self._send(403, b'{"error": "forbidden"}', "application/json")
            return
        if self.path == "/badgz-to-truncated":
            self._send(
                302, b"not gzip at all", "text/html", Location="/truncated", Content_Encoding="gzip"
            )
            return
        if self.path == "/badgz-to-closed":
            self._send(
                302,
                b"not gzip at all",
                "text/html",
                Location="http://127.0.0.1:9/gone",
                Content_Encoding="gzip",
            )
            return
        if self.path == "/needauth":
            if self.headers.get("Authorization") == "ok":
                self._send(200, b'{"ok": 1}', "application/json")
            else:
                self._send(401, b'{"error": "who are you"}', "application/json")
            return
        if self.path == "/slowredir":
            time.sleep(0.3)
            self._send(302, b"", "text/html", Location="/home")
            return
        if self.path in ("/slow400", "/stall"):
            self._dribble()
            return
        if self.path == "/fastredir":
            self._send(302, b"", "text/html", Location="/slow")
            return
        if self.path == "/badgz-redirect":
            self._send(
                302, b"not gzip at all", "text/html", Location="/home", Content_Encoding="gzip"
            )
            return
        if self.path == "/slow":
            received = time.time()
            time.sleep(0.3)
            self._send(200, b"{}", "application/json", X_Received=repr(received))
            return
        if self.path.split("?")[0] == "/whoami":
            body = json.dumps({"authorization": self.headers.get("Authorization")}).encode()
            self._send(200, body, "application/json")
            return
        status, body, content_type = routes.get(self.path, (404, b"not found", "text/plain"))
        self._send(status, body, content_type)

    def _cut_short(self) -> None:
        """Announce a longer body than is sent, then close the connection."""
        redirect = self.path == "/trunc-redirect"
        self.send_response(302 if redirect else 200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", "500" if redirect else "1000")
        if redirect:
            self.send_header("Location", "/home")
        if self.path == "/trunc-cookie":
            self.send_header("Set-Cookie", "sid=abc123; Path=/")
        self.end_headers()
        self.wfile.write(b'{"rows": [')
        self.wfile.flush()
        self.close_connection = True

    def _dribble(self) -> None:
        """Send the headers, then the body slowly: all of it late, or part, a pause, the rest."""
        stall = self.path == "/stall"
        body = b'{"rows": [1, 2, 3]}\n' if stall else b'{"e": "bad"}'
        self.send_response(200 if stall else 400)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.flush()
        try:
            if stall:
                self.wfile.write(body[:12])
                self.wfile.flush()
            time.sleep(1.5)
            self.wfile.write(body[12:] if stall else body)
        except OSError:
            pass  # the client gave up first

    def do_HEAD(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self) -> None:
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if self.path == "/api/search":
            body = json.dumps({"echo": json.loads(raw)}).encode()
            self._send(200, body, "application/json")
        elif self.path == "/login":
            self._send(
                302,
                b"",
                "text/html",
                Location="/home",
                Set_Cookie=["sid=abc123; Path=/; HttpOnly", "theme=dark; Path=/"],
            )
        else:
            self._send(404, b"not found", "text/plain")


@pytest.fixture
def site() -> Iterator[str]:
    """The site's base URL, served for the test."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Site)
    thread = threading.Thread(target=server.serve_forever, args=(0.02,), daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
