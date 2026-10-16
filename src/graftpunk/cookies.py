"""The browser's cookie rules, applied to graftpunk's own sessions and nowhere else.

A graftpunk session should send exactly the cookies the browser that captured it
would send. The standard library's :class:`http.cookiejar.DefaultCookiePolicy`
never sends a ``Secure`` cookie over plain HTTP; Chrome sends one to a
loopback origin. :class:`BrowserCookiePolicy` adds that rule, and
:class:`BrowserCookiesMixin` makes a ``requests.Session`` subclass use its
jar's policy for every request it sends, which stock ``requests`` does not
(``Session.prepare_request`` merges cookies into a fresh jar with the default
policy, psf/requests#5449).

Nothing here changes process-global state. HTTPie works around the same
requests issue by replacing ``http.cookiejar.DefaultCookiePolicy`` for the
whole process when ``httpie.compat`` is imported, so graftpunk imports HTTPie
only under :func:`importing_httpie`, which puts the global back (#253).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from http import cookiejar
from typing import Any, TypeVar
from urllib.parse import urlsplit

import requests
from requests.cookies import RequestsCookieJar, cookiejar_from_dict, merge_cookies
from requests.sessions import merge_hooks, merge_setting
from requests.structures import CaseInsensitiveDict
from requests.utils import get_netrc_auth

from graftpunk._stdout_fd import stdout_with_a_file_descriptor

# Hosts Chrome 155 (headless) was measured sending a Secure cookie to over plain
# HTTP on 2026-10-16: http://localhost, http://foo.localhost, http://127.0.0.1
# and http://[::1]. The rest of 127.0.0.0/8 was not measured and is not here.
LOOPBACK_HOSTS: frozenset[str] = frozenset({"localhost", "127.0.0.1", "::1"})
LOCALHOST_SUFFIX = ".localhost"


def is_secure_loopback_host(host: str) -> bool:
    """Whether Chrome treats *host* (no port, IPv6 without brackets) as secure."""
    host = host.lower()
    return host in LOOPBACK_HOSTS or host.endswith(LOCALHOST_SUFFIX)


class BrowserCookiePolicy(cookiejar.DefaultCookiePolicy):
    """The standard policy, plus Chrome's rule that a loopback origin is secure."""

    def return_ok_secure(self, cookie: cookiejar.Cookie, request: Any) -> bool:
        if super().return_ok_secure(cookie, request):
            return True
        # hostname drops the port and an IPv6 literal's brackets.
        host = urlsplit(request.get_full_url()).hostname
        return host is not None and is_secure_loopback_host(host)


def _replaceable(policy: object) -> bool:
    # The stdlib default, and HTTPie's policy as earlier releases pickled it
    # (or the stub the browser-free unpickler substitutes when HTTPie is
    # absent). Any other class is one a caller chose, and is kept.
    return type(policy) is cookiejar.DefaultCookiePolicy or (
        type(policy).__name__ == "HTTPieCookiePolicy"
    )


def _browser_policy_like(policy: object) -> BrowserCookiePolicy:
    # Keeps the replaced policy's settings (blocked domains, strictness flags);
    # _now is a timestamp the jar refreshes on every use.
    browser = BrowserCookiePolicy()
    browser.__dict__.update({k: v for k, v in vars(policy).items() if k != "_now"})
    return browser


_Jar = TypeVar("_Jar", bound=cookiejar.CookieJar)


def use_browser_cookie_policy(jar: _Jar) -> _Jar:
    """Give *jar* a :class:`BrowserCookiePolicy` with its current policy's settings.

    Only a ``DefaultCookiePolicy`` or an HTTPie ``HTTPieCookiePolicy`` is
    replaced; a jar whose policy is any other class keeps it. Returns *jar*, so
    a load path can wrap the jar it hands back.
    """
    policy = getattr(jar, "_policy", None)
    if _replaceable(policy):
        jar.set_policy(_browser_policy_like(policy))
    return jar


class BrowserCookiesMixin:
    """Mix into a ``requests.Session`` subclass, ahead of ``requests.Session``.

    The session's jar gets a :class:`BrowserCookiePolicy`, and each request's
    ``Cookie`` header is computed with the session jar's policy. Redirects keep
    it: requests follows them with a copy of the prepared request's jar, and
    ``RequestsCookieJar.copy()`` carries the policy.

    ``prepare_request`` replaces ``requests.Session.prepare_request`` rather
    than extending it, so no class between this mixin and ``requests.Session``
    in the MRO may override ``prepare_request``.
    """

    cookies: RequestsCookieJar
    headers: CaseInsensitiveDict
    params: Any
    auth: Any
    hooks: Any
    trust_env: bool

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._use_browser_cookie_policy()

    def __setstate__(self, state: dict[str, Any]) -> None:
        super().__setstate__(state)  # ty: ignore[unresolved-attribute]
        self._use_browser_cookie_policy()

    def _use_browser_cookie_policy(self) -> None:
        # A session without a jar yet (requests.Session.__init__ not run, or a
        # pickle state without cookies) gets the policy in prepare_request.
        jar = getattr(self, "cookies", None)
        if isinstance(jar, cookiejar.CookieJar):
            use_browser_cookie_policy(jar)

    def prepare_request(self, request: requests.Request) -> requests.PreparedRequest:
        # requests.Session.prepare_request (2.32), with the merged jar given the
        # session jar's policy instead of the default one. The rest, including
        # the order of the steps, is stock: the Cookie header is computed before
        # an auth handler runs, and the handler may replace it.
        # self.cookies may have been reassigned since __init__ (a loaded jar).
        policy = use_browser_cookie_policy(self.cookies).get_policy()
        cookies = request.cookies or {}
        if not isinstance(cookies, cookiejar.CookieJar):
            cookies = cookiejar_from_dict(cookies)
        merged_cookies = merge_cookies(
            merge_cookies(RequestsCookieJar(policy=policy), self.cookies), cookies
        )

        auth = request.auth
        if self.trust_env and not auth and not self.auth:
            auth = get_netrc_auth(request.url)

        prepared = requests.PreparedRequest()
        prepared.prepare(
            method=request.method.upper(),  # ty: ignore[unresolved-attribute]
            url=request.url,
            files=request.files,
            data=request.data,
            json=request.json,
            headers=merge_setting(request.headers, self.headers, dict_class=CaseInsensitiveDict),
            params=merge_setting(request.params, self.params),
            auth=merge_setting(auth, self.auth),
            cookies=merged_cookies,
            hooks=merge_hooks(request.hooks, self.hooks),
        )
        return prepared


class BrowserCookieSession(BrowserCookiesMixin, requests.Session):
    """A plain ``requests.Session`` with the browser's cookie rules.

    graftpunk uses it wherever it would otherwise build a bare
    ``requests.Session`` (a command that needs no session, ``--no-session``).
    """


@contextmanager
def importing_httpie() -> Iterator[None]:
    """Import HTTPie modules inside this block without leaving a global change.

    ``httpie.compat`` replaces ``http.cookiejar.DefaultCookiePolicy`` at import;
    the block puts back whatever was there before. ``httpie.context`` sets up
    the terminal at import and needs a stdout with a file descriptor (#250),
    which :func:`graftpunk._stdout_fd.stdout_with_a_file_descriptor` provides.
    """
    saved = cookiejar.DefaultCookiePolicy
    try:
        with stdout_with_a_file_descriptor():
            yield
    finally:
        cookiejar.DefaultCookiePolicy = saved
