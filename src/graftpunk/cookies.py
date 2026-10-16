"""The browser's cookie rules, applied to graftpunk's own sessions and nowhere else.

A graftpunk session should send exactly the cookies the browser that captured it
would send. The standard library's :class:`http.cookiejar.DefaultCookiePolicy`
never sends a ``Secure`` cookie over plain HTTP; Chrome sends one to a
loopback origin. :class:`BrowserCookiePolicy` adds that rule, and
:class:`BrowserCookiesMixin` makes a ``requests.Session`` subclass use its
jar's policy for every request it sends, which stock ``requests`` does not
(``Session.prepare_request`` merges cookies into a fresh jar with the default
policy, psf/requests#5449).

graftpunk leaves process-global state as it found it. HTTPie works around the
same requests issue by replacing ``http.cookiejar.DefaultCookiePolicy`` for the
whole process when ``httpie.compat`` is imported, so graftpunk imports HTTPie
only under :func:`importing_httpie`, which puts the global back (#253). Nothing
here relies on that global either, since other code may have replaced it.
"""

from __future__ import annotations

import copy
import copyreg
import importlib
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http import cookiejar
from typing import TYPE_CHECKING, Any, Self, TypeVar
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


def _stdlib_default_policy() -> type:
    # The module attribute may already name HTTPie's class or a program's own,
    # which normally subclass the original; the original is the class that
    # http.cookiejar defines.
    candidates = [*cookiejar.DefaultCookiePolicy.__mro__, *cookiejar.CookiePolicy.__subclasses__()]
    for cls in candidates:
        if cls.__module__ == "http.cookiejar" and cls.__qualname__ == "DefaultCookiePolicy":
            return cls
    raise ImportError("cannot find the class http.cookiejar defines as DefaultCookiePolicy")


if TYPE_CHECKING:
    STDLIB_DEFAULT_POLICY = cookiejar.DefaultCookiePolicy
else:
    STDLIB_DEFAULT_POLICY = _stdlib_default_policy()


class _PickledCall:
    """Pickles as the call ``func(*args)``; the pickle never names this class."""

    def __init__(self, func: Any, *args: Any) -> None:
        self._func = func
        self._args = args

    def __reduce__(self) -> tuple[Any, ...]:
        return (self._func, self._args)

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        # The pickler requires a reduce callable to be callable; this one is
        # only ever pickled.
        raise TypeError("_PickledCall is evaluated by unpickling, not called")


def _settings(policy: object) -> dict[str, Any]:
    # A policy's settings (blocked domains, strictness flags); _now is a
    # timestamp the jar refreshes on every use.
    return {k: v for k, v in vars(policy).items() if k != "_now"}


class BrowserCookiePolicy(STDLIB_DEFAULT_POLICY):
    """The standard library's policy, plus Chrome's rule that a loopback origin is secure.

    It pickles as a call of ``http.cookiejar.DefaultCookiePolicy``, looked up
    by name when loaded, with the same settings, so a session cached by this
    release loads in releases without :mod:`graftpunk.cookies`; graftpunk's
    load paths turn it back into a ``BrowserCookiePolicy``. A subclass, which
    a program chose, pickles as itself. ``copy.copy`` and ``copy.deepcopy``
    keep the class, a subclass's included.
    """

    def return_ok_secure(self, cookie: cookiejar.Cookie, request: Any) -> bool:
        if super().return_ok_secure(cookie, request):
            return True
        # hostname drops the port and an IPv6 literal's brackets.
        host = urlsplit(request.get_full_url()).hostname
        return host is not None and is_secure_loopback_host(host)

    def __reduce__(self) -> tuple[Any, ...]:
        if type(self) is not BrowserCookiePolicy:
            # A program's subclass carries rules of its own, which loading it
            # as the standard library's class would drop; it pickles by
            # reference as any object does.
            return (copyreg.__newobj__, (type(self),), _settings(self))  # ty: ignore[unresolved-attribute]
        # Loads as http.cookiejar.DefaultCookiePolicy() given these settings.
        # The class is looked up by name when loaded rather than saved by
        # reference, which fails while a program has replaced the global (the
        # name no longer resolves to the class; dill then copies it by value).
        default_policy = _PickledCall(
            getattr, _PickledCall(importlib.import_module, "http.cookiejar"), "DefaultCookiePolicy"
        )
        return (default_policy, (), _settings(self))

    # Without these, copy would go through __reduce__ and return the standard
    # library's class.
    def __copy__(self) -> Self:
        new = type(self).__new__(type(self))
        new.__dict__.update(_settings(self))
        return new

    def __deepcopy__(self, memo: dict[int, Any]) -> Self:
        new = type(self).__new__(type(self))
        new.__dict__.update(copy.deepcopy(_settings(self), memo))
        return new


def _replaceable(policy: object) -> bool:
    # A default, not a choice: the standard library's class; HTTPie's as
    # earlier releases pickled it (or the stub the browser-free unpickler
    # substitutes when HTTPie is absent), matched by name so that checking
    # never imports HTTPie; and the class the global names now, which a jar
    # created or unpickled under that global was given (unpickling resolves
    # http.cookiejar.DefaultCookiePolicy through the global). Any other class
    # is one a program chose for this jar, and is kept.
    cls = type(policy)
    return (
        cls is STDLIB_DEFAULT_POLICY
        or cls is cookiejar.DefaultCookiePolicy
        or cls.__name__ == "HTTPieCookiePolicy"
    )


def _browser_policy_like(policy: object) -> BrowserCookiePolicy:
    browser = BrowserCookiePolicy()
    browser.__dict__.update(_settings(policy))
    return browser


_Jar = TypeVar("_Jar", bound=cookiejar.CookieJar)


def use_browser_cookie_policy(jar: _Jar) -> _Jar:
    """Give *jar* a :class:`BrowserCookiePolicy` with its current policy's settings.

    For a jar graftpunk did not create: one loaded from a cache, or one a
    program assigns to a session. A default policy is replaced: the standard
    library's ``DefaultCookiePolicy``, HTTPie's ``HTTPieCookiePolicy``, or the
    class ``http.cookiejar.DefaultCookiePolicy`` names when this runs (a
    program may have installed its own, before or after importing graftpunk
    alike), since a jar created or unpickled under that global was given it
    by default. A policy of any other class was set on this jar by a program,
    and is kept. graftpunk cannot tell a policy set explicitly from a default
    when both are the global's class, and replaces it. Returns *jar*, so a
    load path can wrap the jar it hands back.
    """
    policy = getattr(jar, "_policy", None)
    if _replaceable(policy):
        jar.set_policy(_browser_policy_like(policy))
    return jar


class BrowserCookiesMixin:
    """Mix into a ``requests.Session`` subclass, ahead of ``requests.Session``.

    The jar the session creates gets a :class:`BrowserCookiePolicy` whatever
    ``http.cookiejar.DefaultCookiePolicy`` names at the time, so a graftpunk
    session sends what the browser would send even when other code replaced
    that global. A jar assigned or unpickled later is treated as
    :func:`use_browser_cookie_policy` says. Each request's ``Cookie`` header is
    computed with the session jar's policy. Redirects keep it: requests follows
    them with a copy of the prepared request's jar, and
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
        self._give_new_jar_the_browser_cookie_policy()

    def __setstate__(self, state: dict[str, Any]) -> None:
        super().__setstate__(state)  # ty: ignore[unresolved-attribute]
        self._use_browser_cookie_policy()

    def _give_new_jar_the_browser_cookie_policy(self) -> None:
        # requests.Session.__init__ just made this jar with whatever class the
        # global named; no caller chose it.
        jar = getattr(self, "cookies", None)
        if isinstance(jar, cookiejar.CookieJar):
            jar.set_policy(BrowserCookiePolicy())

    def _use_browser_cookie_policy(self) -> None:
        # A session without a jar yet (a pickle state without cookies) gets the
        # policy in prepare_request.
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


# Reentrant because gp session export opens the block and save_httpie_session
# opens it again inside.
_IMPORTING_HTTPIE = threading.RLock()


@contextmanager
def importing_httpie() -> Iterator[None]:
    """Import HTTPie modules inside this block without leaving a global change.

    ``httpie.compat`` replaces ``http.cookiejar.DefaultCookiePolicy`` at import;
    the block puts back whatever was there before. One thread at a time runs
    the block, so two overlapping blocks cannot save each other's replacement
    and restore it last. ``httpie.context`` sets up the terminal at import and
    needs a stdout with a file descriptor (#250), which
    :func:`graftpunk._stdout_fd.stdout_with_a_file_descriptor` provides.

    Python imports a module once per process. After this block has imported
    ``httpie.compat``, a later ``import httpie.compat`` elsewhere in the
    process does nothing, so a program that goes on to run HTTPie in the same
    process does not get HTTPie's global policy, and HTTPie's sessions there
    stop sending ``Secure`` cookies to ``http://localhost``. Such a program can
    import ``httpie.compat`` itself before graftpunk exports a session.
    """
    with _IMPORTING_HTTPIE:
        saved = cookiejar.DefaultCookiePolicy
        try:
            with stdout_with_a_file_descriptor():
                yield
        finally:
            cookiejar.DefaultCookiePolicy = saved
