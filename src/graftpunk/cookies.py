"""The browser's cookie rules, applied to graftpunk's own sessions and nowhere else.

A graftpunk session should send exactly the cookies the browser that captured it
would send. The standard library's :class:`http.cookiejar.DefaultCookiePolicy`
never sends a ``Secure`` cookie over plain HTTP; Chrome sends one to a
loopback origin. :class:`BrowserCookiePolicy` adds that rule, and
:class:`BrowserCookiesMixin` makes a ``requests.Session`` subclass use its
jar's policy for every request it sends, which stock ``requests`` does not
(``Session.prepare_request`` merges cookies into a fresh jar with the default
policy, psf/requests#5449).

Nothing here changes process-global state.
"""

from __future__ import annotations

from http import cookiejar
from typing import Any, TypeVar
from urllib.parse import urlsplit

import requests
from requests.cookies import RequestsCookieJar
from requests.structures import CaseInsensitiveDict

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
    """

    cookies: RequestsCookieJar
    headers: CaseInsensitiveDict

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

    def prepare_request(self, request: requests.Request, **kwargs: Any) -> requests.PreparedRequest:
        # self.cookies may have been reassigned since __init__ (a loaded jar).
        policy = use_browser_cookie_policy(self.cookies)._policy  # ty: ignore[unresolved-attribute]
        prepared = super().prepare_request(request, **kwargs)  # ty: ignore[unresolved-attribute]
        jar = prepared._cookies
        if not isinstance(jar, cookiejar.CookieJar):
            return prepared
        jar.set_policy(policy)
        # requests left a Cookie header the caller set (on the request or the
        # session) untouched; any other Cookie header it computed with the
        # default policy, so compute it again with this one.
        caller_headers = requests.sessions.merge_setting(
            request.headers, self.headers, dict_class=CaseInsensitiveDict
        )
        if "Cookie" not in caller_headers:
            prepared.headers.pop("Cookie", None)
            prepared.prepare_cookies(jar)
        return prepared


class BrowserCookieSession(BrowserCookiesMixin, requests.Session):
    """A plain ``requests.Session`` with the browser's cookie rules.

    graftpunk uses it wherever it would otherwise build a bare
    ``requests.Session`` (a command that needs no session, ``--no-session``).
    """
