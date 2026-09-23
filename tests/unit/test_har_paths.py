"""Pure path templating: no HAR involved (plugin tooling spec, 2026-09-11)."""

from __future__ import annotations

import pytest

from graftpunk.har.paths import (
    _MIN_BASE64_LEN,
    _MIN_HEX_LEN,
    bare_host,
    bare_url,
    holds_an_id,
    is_placeholder,
    looks_dynamic,
    param_name_for_segment,
    template_path,
    templated_url,
    templates_a_segment,
)


class TestTemplatePath:
    def test_numeric_segment_collapses_with_singular_param_name(self) -> None:
        template, params = template_path("/orders/123")
        assert template == "/orders/{order_id}"
        assert params == {"order_id": "123"}

    def test_multiple_numeric_segments(self) -> None:
        template, params = template_path("/orders/123/items/456")
        assert template == "/orders/{order_id}/items/{item_id}"
        assert params == {"order_id": "123", "item_id": "456"}

    def test_uuid_segment_collapses(self) -> None:
        template, params = template_path("/sessions/8f14e45f-ceea-467e-bd3d-46f0e7d1f5a3")
        assert template == "/sessions/{session_id}"

    def test_hex_segment_at_min_length_collapses(self) -> None:
        segment = "a" * _MIN_HEX_LEN
        template, _ = template_path(f"/tokens/{segment}")
        assert template == "/tokens/{token_id}"

    def test_hex_segment_under_min_length_stays_literal(self) -> None:
        segment = "a" * (_MIN_HEX_LEN - 1)
        template, _ = template_path(f"/tokens/{segment}")
        assert template == f"/tokens/{segment}"

    def test_base64_like_segment_at_min_length_collapses(self) -> None:
        segment = ("z1" * ((_MIN_BASE64_LEN // 2) + 1))[:_MIN_BASE64_LEN]
        template, _ = template_path(f"/files/{segment}")
        assert template == "/files/{file_id}"

    def test_a_long_word_with_no_digit_stays_literal(self) -> None:
        segment = "z" * (_MIN_BASE64_LEN - 1)
        template, _ = template_path(f"/files/{segment}")
        assert template == f"/files/{segment}"

    def test_short_alphabetic_segment_stays_literal(self) -> None:
        template, params = template_path("/products/wireless-mouse")
        assert template == "/products/wireless-mouse"
        assert params == {}

    def test_leading_segment_with_no_predecessor_uses_bare_id(self) -> None:
        template, params = template_path("/123")
        assert template == "/{id}"
        assert params == {"id": "123"}

    def test_root_path(self) -> None:
        assert template_path("/") == ("/", {})

    def test_trailing_slash_preserved(self) -> None:
        template, _ = template_path("/orders/123/")
        assert template == "/orders/{order_id}/"

    def test_plural_previous_segment_singularizes(self) -> None:
        template, _ = template_path("/carts/999")
        assert template == "/carts/{cart_id}"

    def test_previous_segment_already_a_param_uses_bare_id(self) -> None:
        template, _ = template_path("/orders/123/456")
        assert template == "/orders/{order_id}/{id}"


class TestParamNameForSegment:
    @pytest.mark.parametrize(
        ("prev", "expected"),
        [
            ("orders", "order_id"),
            ("cart", "cart_id"),
            ("", "id"),
            ("{order_id}", "id"),
        ],
    )
    def test_singular_of_previous_segment(self, prev: str, expected: str) -> None:
        assert param_name_for_segment(prev) == expected

    @pytest.mark.parametrize(
        ("prev", "expected"),
        [
            ("status", "status_id"),
            ("address", "address_id"),
            ("analysis", "analysis_id"),
            ("bus", "bus_id"),
            ("orders", "order_id"),
        ],
    )
    def test_a_word_ending_in_s_is_not_treated_as_a_plural(self, prev: str, expected: str) -> None:
        """Naive stripping gave statu_id, addres_id, analysi_id and bu_id; the
        letter before the final s decides, and orders still gives order_id."""
        assert param_name_for_segment(prev) == expected


def test_path_params_are_dropped_from_every_segment() -> None:
    assert template_path("/auth;sid=MIDSEG999/session") == ("/auth/session", {})
    assert template_path("/orders;v=2/123;jsessionid=X") == (
        "/orders/{order_id}",
        {"order_id": "123"},
    )


def test_bare_host_drops_userinfo_and_keeps_the_port() -> None:
    assert bare_host("alice:secret@myshop.example.com:8443") == "myshop.example.com:8443"
    assert bare_host("myshop.example.com") == "myshop.example.com"


class TestTemplatedUrl:
    def test_the_path_is_templated_and_the_rest_kept(self) -> None:
        segment = "7f3a9c2e8b1d4f60a9e2c3b4d5f6a7b8"
        assert (
            templated_url(f"https://myshop.example.com/signin/{segment}")
            == "https://myshop.example.com/signin/{signin_id}"
        )

    def test_a_url_with_no_path_gets_the_root(self) -> None:
        assert templated_url("https://myshop.example.com") == "https://myshop.example.com/"

    def test_a_relative_url_stays_relative(self) -> None:
        assert templated_url("/orders/12345") == "/orders/{order_id}"

    def test_an_empty_url_stays_empty(self) -> None:
        """An empty form action submits to the page itself; "/" would be a claim."""
        assert templated_url("") == ""


class TestTemplatesASegment:
    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("https://myshop.example.com/accounts/12345/session", True),
            ("/signin/7f3a9c2e8b1d4f60a9e2c3b4d5f6a7b8", True),
            ("https://myshop.example.com", False),
            ("https://myshop.example.com/", False),
            ("/session", False),
            ("/orders/", False),
            ("", False),
        ],
    )
    def test_only_a_segment_template_path_collapses_counts(self, url: str, expected: bool) -> None:
        assert templates_a_segment(url) is expected


class TestAnEmailSegmentIsAnAccountValue:
    @pytest.mark.parametrize("segment", ["alice@example.com", "alice%40example.com"])
    def test_an_email_segment_templates_like_an_id(self, segment: str) -> None:
        assert looks_dynamic(segment)
        assert template_path(f"/users/{segment}/orders") == (
            "/users/{user_id}/orders",
            {"user_id": segment},
        )

    def test_a_word_with_an_at_sign_but_no_domain_is_not_an_email(self) -> None:
        assert not looks_dynamic("@home")
        assert not looks_dynamic("team@")

    @pytest.mark.parametrize("segment", ["alice@example.com", "alice%40example.com"])
    def test_every_url_the_digest_keeps_masks_an_email_segment(self, segment: str) -> None:
        assert (
            bare_url(f"https://myshop.example.com/users/{segment}/orders?page=1")
            == "https://myshop.example.com/users/{user_id}/orders"
        )

    def test_a_masked_segment_counts_as_templated(self) -> None:
        assert templates_a_segment("https://myshop.example.com/users/{user_id}/signin")


# The one id rule's two tables (graftpunk.har.paths.holds_an_id). Every position
# the digest reads a name from goes through it; tests/unit/test_id_property.py
# plants the first table in every position end to end.
MUST_BE_ID = (
    "40912873",
    "40912",
    "acct-40912873",
    "order12345x",
    "8f14e45f-ceea-467e-bd3d-46f0e7d1f5a3",
    "ab12cd34",
    "a3f9c2d1e0b4",
    "5f1a9c2e8b1d4f60a9e2c3b4",
    "7f3a9c2e8b1d4f60a9e2c3b4d5f6a7b8",
    "acct-ab12cd34ef",
    "a3f9c2d1e0b4.pdf",
    "cus_NffrFeUfNV2Hib",
    "usr_8fK2x9Qa",
    "usr_8fk2x9qa",
    "x7kq29lp",
    "a9b8c7d6e5",
    "kqzpwmab47",
    "XKQ29LPZ",
    "ABCDEFG123",
    "ZQ9XKLMP",
    "ab12-cd34-ef56-gh78",
    "AB12-CD34-EF56-GH78",
    "k3j4_h5g6_a1b2",
    "ORD-2024-0001",
    "cart_4091287",
    "user_40912873",
    "btn-5f1a9c2e8b1d",
    "otp_40912873",
    "fld_a8f3c9e2b1",
    "usr-Zq9XkLmPwR",
    "acct.Zq9XkLmPwR",
    "Zq9XkLmPwR",
    "x7Kq29Lp",
    "x7Kq29Lp.json",
    "order~40912873",
    "Zm9vYmFyYmF6cXV4MTIzNDU2",
    "alice@example.com",
    "alice%40example.com",
)
MUST_BE_KEPT = (
    "address2",
    "line1",
    "phone2",
    "billing_address2",
    "added2cart",
    "utm_source",
    "per_page",
    "sort_by",
    "userId",
    "orderId2",
    "html5",
    "mp3",
    "v2",
    "v1beta1",
    "v2alpha1",
    "en-US",
    "my-post-2024",
    "windows10",
    "Address2",
    "AddressLine1",
    "Windows10",
    "ipv4Address",
    "oauth2Token",
    "base64Data",
    "sha256Hash",
    "md5Checksum",
    "x509Certificate",
    "covid19Status",
    "line2Address",
    "PhoneNumber2",
    "billing_address_line2",
    "orders",
    "api",
    "red-widget-2024",
    "deadbeef",
    "abcdefgh",
    "facade",
    "user_profile",
    "password-reset",
    "oauth2",
    "2024",
)


class TestHoldsAnId:
    @pytest.mark.parametrize("text", MUST_BE_ID)
    def test_an_id_shape_holds_an_id(self, text: str) -> None:
        assert holds_an_id(text)

    @pytest.mark.parametrize("text", MUST_BE_KEPT)
    def test_an_ordinary_name_holds_none(self, text: str) -> None:
        assert not holds_an_id(text)

    @pytest.mark.parametrize("text", MUST_BE_ID)
    def test_a_path_segment_holding_an_id_templates(self, text: str) -> None:
        assert looks_dynamic(text)
        assert template_path(f"/accounts/{text}/orders")[0] == "/accounts/{account_id}/orders"

    def test_a_path_segment_of_digits_alone_templates_at_any_length(self) -> None:
        assert looks_dynamic("1")
        assert looks_dynamic("2024")

    def test_is_placeholder(self) -> None:
        assert is_placeholder("{user_id}")
        assert not is_placeholder("user_id")
        assert not is_placeholder("{x}y")
