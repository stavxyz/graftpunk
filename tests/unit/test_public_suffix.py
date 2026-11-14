"""Registrable domains and public suffixes from the fixed list."""

import pytest

from graftpunk.public_suffix import (
    TWO_LABEL_PUBLIC_SUFFIXES,
    is_ip_address,
    is_public_suffix,
    registrable_domain,
)


@pytest.mark.parametrize(
    ("host", "domain"),
    [
        ("myshop.example", "myshop.example"),
        ("www.myshop.example", "myshop.example"),
        ("a.b.myshop.example", "myshop.example"),
        ("WWW.MyShop.Example.", "myshop.example"),
        ("myshop.co.uk", "myshop.co.uk"),
        ("www.myshop.co.uk", "myshop.co.uk"),
        ("api.eu.myshop.com.au", "myshop.com.au"),
        ("co.uk", "co.uk"),
        ("localhost", "localhost"),
        ("192.168.1.10", "192.168.1.10"),
        ("::1", "::1"),
        ("[::1]", "[::1]"),
    ],
)
def test_registrable_domain(host: str, domain: str) -> None:
    assert registrable_domain(host) == domain


@pytest.mark.parametrize(
    ("name", "suffix"),
    [
        ("com", True),
        ("example", True),
        ("co.uk", True),
        ("CO.UK", True),
        ("com.au", True),
        ("myshop.example", False),
        ("myshop.co.uk", False),
        ("team.myshop.example", False),
    ],
)
def test_is_public_suffix(name: str, suffix: bool) -> None:
    assert is_public_suffix(name) is suffix


def test_is_ip_address() -> None:
    assert is_ip_address("10.0.0.1")
    assert is_ip_address("[2001:db8::1]")
    assert not is_ip_address("myshop.example")


def test_every_listed_suffix_is_two_lowercase_labels() -> None:
    assert all(
        s == s.lower() and len(s.split(".")) == 2 and all(s.split("."))
        for s in TWO_LABEL_PUBLIC_SUFFIXES
    )
