"""The one capture/fixture filename rule (plugin tooling spec, 2026-09-11)."""

from __future__ import annotations

import re

import pytest

from graftpunk.har.naming import (
    UNNAMED_CONTENT_TYPE,
    EndpointSpecError,
    capture_filename,
    capture_slug,
    capture_text,
    fixture_order,
    fixture_rank,
    normalize_media_type,
    parse_command_spec,
    parse_endpoint,
)


class TestCaptureSlug:
    def test_method_lowercased_and_path_templated(self) -> None:
        assert capture_slug("GET", "/orders/123") == "get_orders_{order_id}"

    def test_root_path_uses_root_placeholder(self) -> None:
        assert capture_slug("GET", "/") == "get_root"

    def test_two_calls_with_different_ids_produce_the_same_slug(self) -> None:
        assert capture_slug("GET", "/orders/1001") == capture_slug("GET", "/orders/1002")


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


class TestNormalizeMediaType:
    def test_parameters_are_stripped(self) -> None:
        assert normalize_media_type("application/json; charset=utf-8") == "application/json"

    def test_lowercased(self) -> None:
        assert normalize_media_type("Application/JSON") == "application/json"

    def test_whitespace_around_the_parameter_is_not_kept(self) -> None:
        assert normalize_media_type("text/html ;charset=UTF-8") == "text/html"

    def test_an_empty_type_is_unnamed(self) -> None:
        assert normalize_media_type("") == UNNAMED_CONTENT_TYPE


class TestFixtureOrder:
    def test_a_recording_of_the_fixture_type_sorts_ahead_of_another_type(self) -> None:
        winner = fixture_order("{}", "application/json", "application/json")
        loser = fixture_order("<p/>", "text/html", "application/json")
        assert winner < loser

    def test_the_comparison_is_normalised(self) -> None:
        """A charset-suffixed content type still counts as the plain fixture type."""
        assert fixture_order("{}", "application/json; charset=utf-8", "application/json") == (
            fixture_order("{}", "application/json", "application/json")
        )

    def test_an_empty_fixture_type_matches_nothing(self) -> None:
        """No endpoint to read a fixture type from: every recording ranks by
        fixture_rank alone, never as though it matched an unnamed type."""
        assert fixture_order("", "", "") == (1, fixture_rank(""))

    def test_within_a_type_a_body_sorts_first(self) -> None:
        with_body = fixture_order("{}", "application/json", "application/json")
        without_body = fixture_order("", "application/json", "application/json")
        assert with_body < without_body


class TestParseEndpoint:
    def test_the_digest_printed_form_parses_to_the_pair(self) -> None:
        assert parse_endpoint("GET /api/orders/{order_id}") == ("GET", "/api/orders/{order_id}")

    def test_a_glob_template_is_kept_as_written(self) -> None:
        assert parse_endpoint("GET /api/orders/*") == ("GET", "/api/orders/*")

    @pytest.mark.parametrize(
        "value", ["/orders", "get /orders", "Get /orders", "GET", "GET   ", "ORDERS /orders"]
    )
    def test_a_value_that_is_not_method_space_template_is_refused(self, value: str) -> None:
        with pytest.raises(EndpointSpecError, match="METHOD template"):
            parse_endpoint(value)

    @pytest.mark.parametrize(
        ("value", "problem"),
        [
            ("GET /orders extra", "has whitespace inside it"),
            ("GET /orders\t/items", "has whitespace inside it"),
            ("GET orders", "starts with neither '/' nor '*'"),
            ("GET https://myshop.example.com/orders", "starts with neither '/' nor '*'"),
        ],
    )
    def test_a_template_that_would_match_nothing_is_refused_naming_the_problem(
        self, value: str, problem: str
    ) -> None:
        with pytest.raises(EndpointSpecError, match=re.escape(problem)):
            parse_endpoint(value)

    def test_a_template_may_start_with_a_glob(self) -> None:
        assert parse_endpoint("GET */orders") == ("GET", "*/orders")

    def test_surrounding_whitespace_is_accepted_and_a_tab_is_refused(self) -> None:
        assert parse_endpoint("  GET   /orders  ") == ("GET", "/orders")
        with pytest.raises(EndpointSpecError):
            parse_endpoint("GET\t/orders")


class TestParseCommandSpec:
    def test_the_triple(self) -> None:
        assert parse_command_spec("order=GET /api/orders/{order_id}") == (
            "order",
            "GET",
            "/api/orders/{order_id}",
        )

    def test_the_split_is_on_the_first_equals_sign(self) -> None:
        assert parse_command_spec("search=GET /api/search?q=a") == (
            "search",
            "GET",
            "/api/search?q=a",
        )

    def test_a_missing_equals_sign_is_refused(self) -> None:
        with pytest.raises(EndpointSpecError, match="no '='"):
            parse_command_spec("orders GET /api/orders")

    def test_an_empty_name_is_refused(self) -> None:
        with pytest.raises(EndpointSpecError, match="no command name"):
            parse_command_spec("=GET /api/orders")

    def test_a_trailing_equals_sign_is_refused(self) -> None:
        with pytest.raises(EndpointSpecError, match="nothing after '='"):
            parse_command_spec("orders=")

    def test_a_malformed_endpoint_half_is_refused_by_parse_endpoint(self) -> None:
        with pytest.raises(EndpointSpecError, match="METHOD template"):
            parse_command_spec("orders=get /api/orders")


class TestCaptureText:
    """What gp observe fixtures writes for a recording, and the generator's own
    reading of whether a fixture is written."""

    @pytest.mark.parametrize(
        ("body", "status", "text"),
        [
            ("x", 200, "x"),
            ("", 200, ""),
            (None, 302, ""),
            (None, 204, ""),
            (None, 200, None),
            (None, 500, None),
        ],
        ids=["body", "empty-text", "null-302", "null-204", "null-200", "null-500"],
    )
    def test_a_recording_s_capture_text(
        self, body: str | None, status: int, text: str | None
    ) -> None:
        assert capture_text(body, status) == text
