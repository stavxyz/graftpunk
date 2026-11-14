"""Registrable domains from a small fixed list of two-label public suffixes.

graftpunk carries no copy of the Public Suffix List. A host's registrable
domain (what a browser calls its site, minus the scheme) is taken as its last
two labels, or its last three when the last two are a listed two-label
suffix (``co.uk``, ``com.au``, ``co.jp``, and the like). That is right for
hosts under a generic or single-label country domain and under the listed
suffixes. It is wrong for a suffix the list lacks (a three-label one such as
``pvt.k12.ma.us``, or a private one such as ``github.io``, where two hosts
the list calls the same site are different sites to a browser).
"""

from __future__ import annotations

import ipaddress
from typing import Final

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
