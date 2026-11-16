"""The plain-HTTP form login example in docs/HOW_IT_WORKS.md, run as written (#173).

The example is read out of the document and executed, then its ``login()`` posts to
a fake site mounted in place of requests' transport: no network. The fake answers
the three ways a form login commonly answers: an accepted login redirects to the
app, and a rejected one either re-renders the form with a 200 or redirects back to
the login page.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
import requests
from requests.adapters import BaseAdapter
from requests.structures import CaseInsensitiveDict

from graftpunk.cache import load_session_for_api
from graftpunk.exceptions import SessionNotFoundError
from graftpunk.graftpunk_session import GraftpunkSession
from tests.unit.guide_harness import REPO_ROOT, blocks, section

HOW_IT_WORKS = (REPO_ROOT / "docs" / "HOW_IT_WORKS.md").read_text(encoding="utf-8")
HEADING = "#### A form login over plain HTTP"


def _example() -> dict[str, Any]:
    """The section's one Python block, executed into a fresh namespace."""
    (_line, code), *rest = blocks(section(HOW_IT_WORKS, HEADING), "python")
    assert not rest
    namespace: dict[str, Any] = {"__name__": "how_it_works_http_login"}
    exec(compile(code, "HOW_IT_WORKS.md", "exec"), namespace)  # noqa: S102
    return namespace


class _FakeSite(BaseAdapter):
    """myshop.example's login form. *rejects_with* is how a wrong password is
    answered: ``"form"`` (200, the form again) or ``"redirect"`` (302 to /login)."""

    def __init__(self, rejects_with: str) -> None:
        super().__init__()
        self.rejects_with = rejects_with
        self.sent: list[tuple[str, str]] = []

    def send(self, request: requests.PreparedRequest, **_kwargs: Any) -> requests.Response:
        path = urlsplit(request.url or "").path
        self.sent.append((request.method or "", path))
        if request.method == "POST" and path == "/login":
            body = request.body if isinstance(request.body, str) else ""
            form = parse_qs(body)
            if form.get("password") == ["right"] and form.get("email") == ["ann@myshop.example"]:
                return self._answer(request, 302, location="/account")
            if self.rejects_with == "redirect":
                return self._answer(request, 302, location="/login")
            return self._answer(request, 200, body=b"<form action='/login'>")
        return self._answer(request, 200, body=b"<html>")

    def _answer(
        self,
        request: requests.PreparedRequest,
        status: int,
        *,
        location: str | None = None,
        body: bytes = b"",
    ) -> requests.Response:
        response = requests.Response()
        response.status_code = status
        response.headers = CaseInsensitiveDict({"Location": location} if location else {})
        response._content = body
        response.url = request.url or ""
        response.request = request
        return response

    def close(self) -> None:
        pass


@pytest.fixture()
def site(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> _FakeSite:
    """The fake site, as the transport of every requests.Session made in the test."""
    fake = _FakeSite(getattr(request, "param", "form"))
    monkeypatch.setattr(requests.sessions, "HTTPAdapter", lambda: fake)
    return fake


def _plugin(namespace: dict[str, Any]) -> Any:
    return namespace["MyShopPlugin"]()


def test_an_accepted_login_is_cached_and_loads_for_commands(
    site: _FakeSite,
    fresh_backend: None,
) -> None:
    namespace = _example()
    plugin = _plugin(namespace)
    assert plugin.login({"username": "ann@myshop.example", "password": "right"}) is True
    assert site.sent == [("POST", "/login"), ("GET", "/account")]
    # The CLI's and the client's own load path reads the cached requests.Session.
    assert isinstance(load_session_for_api("myshop"), GraftpunkSession)


@pytest.mark.parametrize("site", ["form", "redirect"], indirect=True)
def test_a_rejected_login_is_not_cached(site: _FakeSite, fresh_backend: None) -> None:
    """A 200 re-rendering the form and a 302 back to /login are both failures,
    though neither status is an error."""
    plugin = _plugin(_example())
    assert plugin.login({"username": "ann@myshop.example", "password": "wrong"}) is False
    with pytest.raises(SessionNotFoundError):
        load_session_for_api("myshop")


@pytest.mark.parametrize(
    ("status", "location", "url", "accepted"),
    [
        (302, "/account", "https://myshop.example/login", True),
        (302, "https://myshop.example/login/", "https://myshop.example/login", False),
        (303, "/login?error=1", "https://myshop.example/login", False),
        (200, None, "https://myshop.example/account", True),
        (200, None, "https://myshop.example/login", False),
        (500, None, "https://myshop.example/oops", False),
    ],
)
def test_the_decision_reads_where_the_response_ended(
    status: int, location: str | None, url: str, accepted: bool
) -> None:
    """With redirects followed the final URL decides; with ``allow_redirects=False``
    the ``Location`` does."""
    response = requests.Response()
    response.status_code = status
    response.headers = CaseInsensitiveDict({"Location": location} if location else {})
    response.url = url
    assert _example()["signed_in"](response) is accepted
