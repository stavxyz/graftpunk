"""The browser's cookie rules, applied to graftpunk's own sessions and nowhere else.

A graftpunk session should send exactly the cookies the browser that captured it
would send. :func:`cookie_from_browser` stores a captured cookie with the
attributes that decide where the browser sends it. The standard library's
:class:`http.cookiejar.DefaultCookiePolicy` never sends a ``Secure`` cookie
over plain HTTP, where Chrome sends one to a loopback origin, and sends a
host-only cookie to subdomains, where RFC 6265 (section 5.4) sends it to its
own host alone. Chrome 155 was measured on both on 2026-10-16; for the second,
a host-only cookie set by ``http://site.localhost`` was sent to
``site.localhost`` and not to ``sub.site.localhost``. ChromeDriver was not
measured.
:class:`BrowserCookiePolicy` changes those two rules, and
:class:`BrowserCookiesMixin` makes a ``requests.Session`` subclass use its
jar's policy for every request it sends, which stock ``requests`` does not
(``Session.prepare_request`` merges cookies into a fresh jar with the default
policy, psf/requests#5449).

graftpunk leaves process-global state as it found it. HTTPie works around the
same requests issue by replacing ``http.cookiejar.DefaultCookiePolicy`` for the
whole process when ``httpie.compat`` is imported (``httpie.context`` imports
it too), so graftpunk imports those only under :func:`importing_httpie`, which
puts the global back (#253). Loading a session cached by 1.21.0 or earlier
imports ``httpie.cookies``, whose class the pickle names; that module leaves
the global alone. Nothing here relies on that global either, since other code
may have replaced it.
"""

from __future__ import annotations

import copy
import copyreg
import importlib
import threading
from collections.abc import Iterator, Mapping
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

__all__ = [
    "BrowserCookiePolicy",
    "BrowserCookieSession",
    "BrowserCookiesMixin",
    "cookie_from_browser",
    "importing_httpie",
    "use_browser_cookie_policy",
]
# Hosts Chrome 155 (headless) was measured sending a Secure cookie to over plain
# HTTP on 2026-10-16: http://localhost, http://foo.localhost, http://127.0.0.1
# and http://[::1]. The rest of 127.0.0.0/8 was not measured and is not here.
_LOOPBACK_HOSTS: frozenset[str] = frozenset({"localhost", "127.0.0.1", "::1"})
_LOCALHOST_SUFFIX = ".localhost"


def _is_secure_loopback_host(host: str) -> bool:
    """Whether Chrome treats *host* (no port, IPv6 without brackets) as secure."""
    host = host.lower()
    return host in _LOOPBACK_HOSTS or host.endswith(_LOCALHOST_SUFFIX)


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
    _STDLIB_DEFAULT_POLICY = cookiejar.DefaultCookiePolicy
else:
    _STDLIB_DEFAULT_POLICY = _stdlib_default_policy()


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


def _request_host(request: Any) -> str:
    # request_host is in http.cookiejar but not in its stubs.
    return cookiejar.request_host(request)  # ty: ignore[unresolved-attribute]


class BrowserCookiePolicy(_STDLIB_DEFAULT_POLICY):
    """The standard library's policy, plus Chrome's rule that a loopback origin is secure.

    A host-only cookie (one stored without ``domain_specified``, as
    :func:`cookie_from_browser` stores a browser's host-only cookie and the
    jar stores a ``Set-Cookie`` without ``Domain``) goes to its own host only.
    When the jar accepts a host-only ``Set-Cookie``, the policy records the
    host that set it as the cookie's ``graftpunk-host`` nonstandard
    attribute, as :func:`cookie_from_browser` does for a captured one, and a
    cookie that carries it is sent to that host alone. The jar files a cookie
    from a host without a dot under ``<host>.local``, so ``printer`` and
    ``printer.local`` share a domain, and the recorded host tells them apart.
    A host-only cookie without it (stored by an earlier release, or by other
    code) goes to the host its domain names, which for ``printer.local``
    means ``printer`` and ``printer.local`` both.

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
        return host is not None and _is_secure_loopback_host(host)

    def set_ok(self, cookie: cookiejar.Cookie, request: Any) -> bool:
        # The jar asks this of every cookie it extracts from a response, with
        # the request that got it, and stores the cookie only on True; this is
        # the one point where the host that set a host-only cookie is known.
        if not super().set_ok(cookie, request):
            return False
        if cookie.domain and not cookie.domain_specified:
            cookie.set_nonstandard_attr(_SET_BY_HOST_ATTR, _request_host(request))
        return True

    def return_ok_domain(self, cookie: cookiejar.Cookie, request: Any) -> bool:
        # A cookie set without a Domain attribute is host-only: sent to the
        # host that set it and no other. The standard library also sends it to
        # subdomains unless strict_ns_domain says otherwise. An empty domain is
        # requests' cookie for every host, and is left to the standard library.
        if cookie.domain and not cookie.domain_specified:
            if set_by := cookie.get_nonstandard_attr(_SET_BY_HOST_ATTR):
                return _request_host(request) == set_by
            # No recorded host. A dotless host's cookie is filed under
            # "<host>.local", the effective host, by the standard library; the
            # bare host is accepted too, for a jar other code filled.
            # eff_request_host is in http.cookiejar but not in its stubs.
            req_host, effective_host = cookiejar.eff_request_host(request)  # ty: ignore[unresolved-attribute]
            return cookie.domain in (req_host, effective_host)
        return super().return_ok_domain(cookie, request)

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


_DOTLESS_HOST_SUFFIX = ".local"


# The nonstandard attribute in which a host-only jar cookie records the host
# that set it, in http.cookiejar.request_host's form: lowercase, without a
# port, an IPv6 literal in brackets. A nonstandard attribute is never sent and
# pickles with the cookie; the HTTPie export does not write it.
_SET_BY_HOST_ATTR = "graftpunk-host"


def _browser_host(host: str) -> str:
    # A host as a browser reported it, in request_host's form: lowercase, and
    # an IPv6 literal in brackets (CDP reports "[::1]"; the brackets are added
    # in case a driver reports it without).
    host = host.lower()
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return host


def _jar_host(host: str) -> str:
    # The domain http.cookiejar files a Set-Cookie from *host* (in
    # _browser_host's form) under, so that a server's refresh of a captured
    # host-only cookie replaces it rather than sitting beside it:
    # "<host>.local" for a host without a dot (eff_request_host).
    return host if "." in host else host + _DOTLESS_HOST_SUFFIX


def _browser_cookie_domain(cookie: cookiejar.Cookie) -> str:
    """The domain to give a browser for *cookie*, a jar cookie.

    A host-only cookie that recorded the host that set it (see
    :class:`BrowserCookiePolicy`) gets that host. Otherwise the stored domain
    is used, except that ``http.cookiejar`` files a cookie from a host without
    a dot under ``<host>.local``, so ``localhost.local`` and ``[<ipv6>].local``
    come back without the suffix. Any other unrecorded ``<name>.local`` is
    returned as it is, since it may be a real ``.local`` (mDNS) host rather
    than a dotless name.
    """
    if not cookie.domain_specified and (set_by := cookie.get_nonstandard_attr(_SET_BY_HOST_ATTR)):
        return set_by
    domain = cookie.domain
    if domain.endswith(_DOTLESS_HOST_SUFFIX):
        host = domain.removesuffix(_DOTLESS_HOST_SUFFIX)
        if host == "localhost" or (host.startswith("[") and host.endswith("]")):
            return host
    return domain


def cookie_from_browser(record: Any) -> cookiejar.Cookie:
    """A jar cookie for one cookie a browser reported, sent where the browser sends it.

    *record* is a WebDriver cookie dict, as selenium's ``get_cookies()``
    returns (``name``, ``value``, ``domain``, ``path``, ``secure``,
    ``httpOnly``, and ``expiry`` only on a persistent cookie), or a CDP
    ``Network.Cookie``, as nodriver's ``browser.cookies.get_all()`` returns
    (``http_only``, ``session``, and ``expires``, which is -1 on a session
    cookie).

    The cookie keeps its name, value, domain, path, ``Secure`` flag and
    expiry; a session cookie stays one (no expiry, discarded with the
    session). HttpOnly is kept as the ``HttpOnly`` nonstandard attribute, as
    ``http.cookiejar`` records it from a ``Set-Cookie`` header. A domain with
    a leading dot is a domain cookie, sent to subdomains; one without is
    host-only, which :class:`BrowserCookiePolicy` sends to that host alone.
    A host-only cookie records that host, lowercase and an IPv6 literal in
    brackets, as the ``graftpunk-host`` nonstandard attribute, and is stored
    under the domain ``http.cookiejar`` files that host's ``Set-Cookie``
    cookies under, so a server's refresh replaces it: ``localhost`` as
    ``localhost.local``, ``[::1]`` as ``[::1].local``, and any other host
    without a dot likewise. ``printer`` and ``printer.local`` are then both
    stored as ``printer.local``, and the recorded host keeps each cookie to
    its own host.
    """
    if isinstance(record, Mapping):
        name, value, domain = record["name"], record["value"], record["domain"]
        path = record.get("path") or "/"
        secure = bool(record.get("secure"))
        http_only = bool(record.get("httpOnly"))
        expires = record.get("expiry")
    else:
        name, value, domain = record.name, record.value, record.domain
        path = record.path or "/"
        secure = bool(record.secure)
        http_only = bool(record.http_only)
        expires = None if record.session or record.expires is None else record.expires
    if expires is not None and expires < 0:
        expires = None
    domain_cookie = domain.startswith(".")
    # http.cookiejar records a valueless attribute such as HttpOnly as None.
    rest: dict[str, str | None] = {"HttpOnly": None} if http_only else {}
    if not domain_cookie:
        set_by = _browser_host(domain)
        rest[_SET_BY_HOST_ATTR] = set_by
        # cookiejar.Cookie lowercases the domain itself.
        domain = _jar_host(set_by)
    return cookiejar.Cookie(
        version=0,
        name=name,
        value=value,
        port=None,
        port_specified=False,
        domain=domain,
        domain_specified=domain_cookie,
        domain_initial_dot=domain_cookie,
        path=path,
        path_specified=True,
        secure=secure,
        expires=None if expires is None else int(expires),
        discard=expires is None,
        comment=None,
        comment_url=None,
        # The stubs type rest as str values.
        rest=rest,  # ty: ignore[invalid-argument-type]
    )


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
        cls is _STDLIB_DEFAULT_POLICY
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

    The jar the session creates gets a new :class:`BrowserCookiePolicy`, with
    the standard library's default settings even when other code replaced
    ``http.cookiejar.DefaultCookiePolicy`` with a class that sets others. A
    jar assigned or unpickled later is treated as
    :func:`use_browser_cookie_policy` says, which keeps a default policy's
    settings. Any ``http.cookiejar.CookieJar`` may be assigned, as with stock
    ``requests``. Each request's ``Cookie`` header is computed with the
    session jar's policy. Redirects keep it: requests follows them with a copy
    of the prepared request's jar, and ``RequestsCookieJar.copy()`` carries
    the policy.

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
        # global named; no caller chose it or its settings.
        # use_browser_cookie_policy would copy the settings that class's
        # __init__ sets, which a program's class may change.
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
        # _policy, not get_policy(): requests accepts any http.cookiejar.CookieJar
        # as Session.cookies, and only RequestsCookieJar has get_policy().
        policy = use_browser_cookie_policy(self.cookies)._policy  # ty: ignore[unresolved-attribute]
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


# Reentrant so that a caller already inside the block can call
# BrowserSession.save_httpie_session, which opens it again.
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
