"""Pure path templating: no HAR involved (plugin tooling spec, 2026-09-11)."""

from __future__ import annotations

import pytest

from graftpunk.har.paths import (
    _MIN_BASE64_LEN,
    _MIN_HEX_LEN,
    bare_host,
    bare_url,
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

    def test_base64_like_segment_under_min_length_stays_literal(self) -> None:
        segment = ("z1" * ((_MIN_BASE64_LEN // 2) + 1))[: _MIN_BASE64_LEN - 1]
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
