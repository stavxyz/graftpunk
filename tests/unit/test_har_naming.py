"""The one capture/fixture filename rule (plugin tooling spec, 2026-09-11)."""

from __future__ import annotations

from graftpunk.har.naming import capture_filename, capture_slug


class TestCaptureSlug:
    def test_method_lowercased_and_path_templated(self) -> None:
        assert capture_slug("GET", "/orders/123") == "get_orders_{order_id}"

    def test_root_path_uses_root_placeholder(self) -> None:
        assert capture_slug("GET", "/") == "get_root"

    def test_two_calls_with_different_ids_produce_the_same_slug(self) -> None:
        assert capture_slug("GET", "/orders/1") == capture_slug("GET", "/orders/2")


class TestCaptureFilename:
    def test_json_content_type(self) -> None:
        assert capture_filename("GET", "/orders/123", "application/json") == (
            "get_orders_{order_id}.json"
        )

    def test_html_content_type(self) -> None:
        assert capture_filename("GET", "/login", "text/html; charset=utf-8") == ("get_login.html")

    def test_other_text_content_type_falls_back_to_txt(self) -> None:
        assert capture_filename("GET", "/robots", "text/plain") == "get_robots.txt"

    def test_unknown_content_type_falls_back_to_bin(self) -> None:
        assert capture_filename("GET", "/asset", "application/octet-stream") == ("get_asset.bin")

    def test_known_binary_content_type_uses_its_extension(self) -> None:
        assert capture_filename("GET", "/photo", "image/png") == "get_photo.png"

    def test_post_method_lowercased(self) -> None:
        assert capture_filename("POST", "/login", "application/json") == "post_login.json"
