"""Origins, and registrable domains from a small fixed list of two-label public suffixes.

An origin is a URL's scheme, host, and port, as a browser compares them; a
site is a scheme and a registrable domain. graftpunk carries no copy of the
Public Suffix List. A host's registrable domain (what a browser calls its site,
minus the scheme) is taken as its last two labels, or its last three when the
last two are a listed two-label suffix (``co.uk``, ``com.au``, ``co.jp``, and
the like). That is right for
hosts under a generic or single-label country domain and under the listed
suffixes. It is wrong for a suffix the list lacks (a three-label one such as
``pvt.k12.ma.us``, or a private one such as ``github.io``, where two hosts
the list calls the same site are different sites to a browser).
"""

from __future__ import annotations

import ipaddress
import urllib.parse
from typing import Any, Final

# The port a scheme means when a URL names none.
DEFAULT_PORTS: Final[dict[str, int]] = {"http": 80, "https": 443}

Origin = tuple[str, str, int | None]

# Country-code second-level domains under which names are registered, the
# ones most often seen in front of sites. Not exhaustive: see the module
# docstring for what a missing one costs.
TWO_LABEL_PUBLIC_SUFFIXES: Final[frozenset[str]] = frozenset(
    {
        # United Kingdom
        "ac.uk",
        "co.uk",
        "gov.uk",
        "ltd.uk",
        "me.uk",
        "net.uk",
        "nhs.uk",
        "org.uk",
        "plc.uk",
        "sch.uk",
        # Australia and New Zealand
        "asn.au",
        "com.au",
        "edu.au",
        "gov.au",
        "id.au",
        "net.au",
        "org.au",
        "ac.nz",
        "co.nz",
        "geek.nz",
        "gen.nz",
        "govt.nz",
        "net.nz",
        "org.nz",
        # Japan, Korea, China, Hong Kong, Taiwan
        "ac.jp",
        "co.jp",
        "ed.jp",
        "go.jp",
        "gr.jp",
        "lg.jp",
        "ne.jp",
        "or.jp",
        "ac.kr",
        "co.kr",
        "go.kr",
        "ne.kr",
        "or.kr",
        "com.cn",
        "edu.cn",
        "gov.cn",
        "net.cn",
        "org.cn",
        "com.hk",
        "edu.hk",
        "gov.hk",
        "net.hk",
        "org.hk",
        "com.tw",
        "edu.tw",
        "gov.tw",
        "net.tw",
        "org.tw",
        # South and South-East Asia
        "ac.in",
        "co.in",
        "firm.in",
        "gen.in",
        "gov.in",
        "ind.in",
        "net.in",
        "org.in",
        "ac.id",
        "co.id",
        "or.id",
        "com.my",
        "com.ph",
        "com.pk",
        "com.sg",
        "edu.sg",
        "gov.sg",
        "net.sg",
        "org.sg",
        "ac.th",
        "co.th",
        "go.th",
        "in.th",
        "com.vn",
        # Middle East and Africa
        "ac.il",
        "co.il",
        "org.il",
        "com.eg",
        "com.sa",
        "co.ke",
        "com.gh",
        "com.ng",
        "ac.za",
        "co.za",
        "gov.za",
        "org.za",
        "com.tr",
        "gov.tr",
        "org.tr",
        # Americas
        "com.ar",
        "com.br",
        "gov.br",
        "net.br",
        "org.br",
        "com.co",
        "com.ec",
        "com.mx",
        "gob.mx",
        "org.mx",
        "com.pe",
        # Europe
        "com.es",
        "com.pl",
        "com.ua",
    }
)


def is_ip_address(host: str) -> bool:
    """True when *host* (no port) is an IPv4 address or an IPv6 one, bracketed or not."""
    name = host[1:-1] if host.startswith("[") and host.endswith("]") else host
    try:
        ipaddress.ip_address(name)
    except ValueError:
        return False
    return True


def is_public_suffix(name: str) -> bool:
    """True when *name* (a host name, no port) is a suffix nobody registers under
    as a site: a single label (``com``, ``uk``) or a listed two-label suffix."""
    name = name.lower().rstrip(".")
    return "." not in name or name in TWO_LABEL_PUBLIC_SUFFIXES


def registrable_domain(host: str) -> str:
    """The registrable domain of *host* (a host name, no port), lowercased.

    ``shop.myshop.example`` gives ``myshop.example`` and ``www.myshop.co.uk``
    gives ``myshop.co.uk``. An IP address is its own, and so is a host that is
    itself a public suffix.
    """
    name = host.lower().rstrip(".")
    if is_ip_address(name):
        return name
    labels = name.split(".")
    if len(labels) >= 3 and ".".join(labels[-2:]) in TWO_LABEL_PUBLIC_SUFFIXES:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def origin(url: Any) -> Origin | None:
    """*url*'s origin: (scheme, host, port), lowercased, with the default port
    filled in and a non-ASCII host in its IDNA form (``xn--...``, the form
    requests sends), or None when *url* is not a string naming a scheme and a
    host, or its host or port cannot be read.

    An IPv6 host comes back without its brackets, as ``urlsplit`` gives it. A
    trailing dot is kept: ``myshop.example.`` is another origin to a browser.
    """
    if not isinstance(url, str):
        return None
    try:
        parts = urllib.parse.urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    host = parts.hostname
    if not scheme or not host:
        return None
    if not host.isascii():
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError:
            return None
    return scheme, host, port if port is not None else DEFAULT_PORTS.get(scheme)


def serialized_origin(value: Origin) -> str:
    """The ``Origin`` header a browser sends for *value*: the port left out when it
    is the scheme's default, and an IPv6 host bracketed."""
    scheme, host, port = value
    if ":" in host:
        host = f"[{host}]"
    if port is None or port == DEFAULT_PORTS.get(scheme):
        return f"{scheme}://{host}"
    return f"{scheme}://{host}:{port}"


def same_site(a: Origin, b: Origin) -> bool:
    """Whether origins *a* and *b* are one site: the same scheme and the same
    registrable domain (:func:`registrable_domain`), as Chrome compares them."""
    return a[0] == b[0] and registrable_domain(a[1].rstrip(".")) == registrable_domain(
        b[1].rstrip(".")
    )
