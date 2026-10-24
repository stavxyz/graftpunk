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
    POST /api/search   echoes the JSON body back
    POST /login        302 to /home, setting two cookies
"""

from __future__ import annotations

import json
import threading
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
        }
        if self.path == "/truncated":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "1000")
            self.end_headers()
            self.wfile.write(b'{"rows": [')
            self.wfile.flush()
            self.close_connection = True
            return
        status, body, content_type = routes.get(self.path, (404, b"not found", "text/plain"))
        self._send(status, body, content_type)

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
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
