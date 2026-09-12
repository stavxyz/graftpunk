"""Pure path templating: no HAR involved (plugin tooling spec, 2026-09-11)."""

from __future__ import annotations

import pytest

from graftpunk.har.paths import (
    _MIN_BASE64_LEN,
    _MIN_HEX_LEN,
    param_name_for_segment,
    template_path,
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
