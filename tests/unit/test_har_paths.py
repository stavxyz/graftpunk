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
    keys_are_ids,
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

    def test_mixed_hex_at_min_length_is_an_id_and_one_shorter_is_not(self) -> None:
        segment = ("a1" * _MIN_HEX_LEN)[:_MIN_HEX_LEN]
        assert holds_an_id(segment)
        assert not holds_an_id(segment[:-1] + "x")
        assert template_path(f"/tokens/{segment}")[0] == "/tokens/{token_id}"

    def test_base64_like_token_at_min_length_is_an_id(self) -> None:
        segment = ("z1a" * _MIN_BASE64_LEN)[:_MIN_BASE64_LEN]
        assert holds_an_id(segment)
        assert not holds_an_id(segment[:-1])
        assert template_path(f"/files/{segment}")[0] == "/files/{file_id}"

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


# The name rule's key-position id table (graftpunk.har.paths.holds_an_id): the
# strong-evidence shapes an account value takes as a key, header, input, cookie, or
# token name. Every entry must be caught; tests/unit/test_id_property.py plants
# each in every name and path position end to end, and test_id_miss_rates.py
# requires every sub-rule of the name rule to be the only catch of one entry.
KEY_POSITION_IDS = (
    "alice@example.com",
    "alice%40example.com",
    "8f14e45f-ceea-467e-bd3d-46f0e7d1f5a3",
    "40912873",
    "409128",
    "user_40912873",
    "acct-409128",
    "order~40912873",
    "otp_40912873",
    "a3f9c2d1e0b4",
    "a3f9c2d1e0b4.pdf",
    "5f1a9c2e8b1d4f60a9e2c3b4",
    "btn-5f1a9c2e8b1d",
    "ctl00$5f1a9c2e8b1d",
    "4111-1111-1111-1111",
    "123-45-6789",
    "1-800-555-0199",
    "order-2024-1187-7731",
    "2024-01-15-0412",
    "cus_NffrFeUfNV2Hib",
    "pi_3NkQ7xLkdIwHu7ix",
    "usr-8fK2x9QaZ1mN",
    "Zm9vYmFyYmF6cXV4MTIzNDU2",
    "sess-Zm9vYmFyYmF6cXV4MTIzNDU2",
    "(555)123-4567",
    "tel(512)555-0100",
    "0xdeadbeefcafe12",
    "wallet_0x5f1aBcDeF09aAbBc",
    "otp_cus_NffrFeUfNV2Hib",
    "x-cus_NffrFeUfNV2Hib",
)
# Shapes an account value takes in a path, beyond the table above. A path segment
# fails closed, so each is dynamic there; as a name most are kept (short random
# tokens are a known limit of the name rule).
PATH_ID_SHAPES = (
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
    "usr_8fK2x9Qa",
    "cus_4fK2x9QaZ1",
    "ctl00$a8f3k2x9q1z7",
    "4111-1111-1111-1111",
    "123-45-6789",
    "1-800-555-0199",
    "order-2024-1187-7731",
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
    "img-001-thumb",
    "abfkuro7",
    "4address",
    "MAPLETON7",
    "zPde0Igx",
    "window7us",
    "abc4order",
    "Lmv8Lifonp-Rq-X-bivNx",
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
    "k8sNamespace",
    "sha256Key",
    "ed25519",
    "argon2id",
    "retina2x",
    "pbkdf2Iterations",
    "Md5OfMessageAttributes",
    "x-amz-content-sha256",
    "X-Hub-Signature-256",
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
    "shippingAddressLine2",
    "shipping_address_line2_city",
    "line_item_2_unit_price",
    "X-Goog-Upload-Protocol-v2-Status",
    "ctl00_MainContent_LoginUser_Password",
    "ctl00$ContentPlaceHolder1$txtUserName",
    "ctl00$ContentPlaceHolder1$btnLogin",
    "step_01_done",
    "2024-01-15",
    "webhook2",
    "backpack2",
    "thumbnail2",
    "PDFExport2",
    "address4",
    "$filter",
    "$top",
    "ctl00$Main$txtSearch",
    "__VIEWSTATE",
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
    @pytest.mark.parametrize("text", KEY_POSITION_IDS)
    def test_a_key_position_id_holds_an_id(self, text: str) -> None:
        assert holds_an_id(text)

    @pytest.mark.parametrize("text", ["kqzpwmab47", "x7Kq29Lp", "usr_8fk2x9qa", "ab12-cd34-ef56"])
    def test_a_short_random_token_as_a_name_is_kept(self, text: str) -> None:
        """The name rule's known limit: names are dropped on strong evidence only,
        so a short random token used as a field name is kept. As a path segment it
        is dynamic, since paths fail closed."""
        assert not holds_an_id(text)
        assert looks_dynamic(text)

    @pytest.mark.parametrize("text", MUST_BE_KEPT)
    def test_an_ordinary_name_holds_none(self, text: str) -> None:
        assert not holds_an_id(text)

    def test_a_date_as_a_name_is_a_name(self) -> None:
        assert not holds_an_id("2024-01-15")
        assert holds_an_id("2024-01-15-0412")

    def test_is_placeholder(self) -> None:
        assert is_placeholder("{user_id}")
        assert not is_placeholder("user_id")
        assert not is_placeholder("{x}y")


# Path segments fail closed (graftpunk.har.paths.looks_dynamic): a segment holding a
# digit stays literal only when every part is letters, a word with a trailing digit
# run of at most 2, a version, or a lone digit run of at most 2.
# Short digit groups: dates, sort codes, addresses, and phone fragments whose
# every digit part is 1 or 2 digits long. A lone short digit part keeps a segment
# literal only when it is the segment's only part holding a digit.
SHORT_DIGIT_GROUPS = (
    "12-34-56",
    "03-14-87",
    "14.03.87",
    "15-01-24",
    "01.15.24",
    "4-11-11",
    "12$34$56",
    "acct-12-34",
    "10.0.0.1",
    "10.0.0.12",
    "v40912",
    "v1beta12345",
)
MUST_BE_DYNAMIC_SEGMENT = (
    "4111-1111-1111-1111",
    "123-45-6789",
    "555-867-5309",
    "512-555-0100",
    "020-7946-0958",
    "1-800-555-0199",
    "6035-3210-9876",
    "order-2024-1187-7731",
    "2026-09-23",
    "1987-03-14",
    "ab12cd34",
    "img-001-thumb",
    "123",
    "2024",
    "my-post-2024",
    "base64Data",
    "sha256Hash",
    "cus_NffrFeUfNV2Hib",
    *KEY_POSITION_IDS,
    *PATH_ID_SHAPES,
    *SHORT_DIGIT_GROUPS,
)
MUST_STAY_LITERAL_SEGMENT = (
    "v2",
    "v10",
    "v2beta1",
    "step-2",
    "page-12",
    "smith42",
    "v1beta1",
    "v2alpha1",
    "api",
    "en-US",
    "my-post",
    "address2",
    "windows10",
    "page",
    "2",
    "12",
    "ec2",
    "oauth2",
    "orders",
    "user_profile",
    "password-reset",
    "deadbeef",
    "report.pdf",
    "image2.png",
    "{order_id}",
)


class TestPathSegmentsFailClosed:
    @pytest.mark.parametrize("segment", MUST_BE_DYNAMIC_SEGMENT)
    def test_a_segment_holding_an_account_value_templates(self, segment: str) -> None:
        assert looks_dynamic(segment)
        assert template_path(f"/accounts/{segment}/orders")[0] == "/accounts/{account_id}/orders"

    @pytest.mark.parametrize("segment", MUST_STAY_LITERAL_SEGMENT)
    def test_a_route_segment_stays_literal(self, segment: str) -> None:
        assert not looks_dynamic(segment)
        assert template_path(f"/api/{segment}/items")[0] == f"/api/{segment}/items"


class TestKeysAreIds:
    @pytest.mark.parametrize(
        "keys",
        [
            ["-NqF7xYz3abcDEFghiJK", "-NqF7xZ01bcdEFGhijKL", "-NqF7y0Q2cdeFGHijkLM"],
            ["recA1b2C3d4E5f6G7", "recH8i9J0k1L2m3N4", "recO5p6Q7r8S9t0U1"],
        ],
        ids=["push-ids", "record-ids"],
    )
    def test_random_keys_of_one_length_are_ids(self, keys: list[str]) -> None:
        assert keys_are_ids(keys)

    @pytest.mark.parametrize(
        "keys",
        [
            ["addressLine1", "addressLine2", "addressLine3"],
            ["customField1", "customField2", "customField3", "customField4"],
            ["streetLine01", "streetLine02", "streetLine03"],
        ],
        ids=["address-lines", "custom-fields", "street-lines"],
    )
    def test_a_numbered_field_group_is_not_ids(self, keys: list[str]) -> None:
        """Keys that differ only in a trailing digit run are one field, numbered."""
        assert not keys_are_ids(keys)
