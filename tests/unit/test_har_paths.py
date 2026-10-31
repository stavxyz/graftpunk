"""Pure path templating: no HAR involved (plugin tooling spec, 2026-09-11)."""

from __future__ import annotations

import importlib.util
import random
import string
import time
import uuid
from types import ModuleType

import pytest

import graftpunk.har.paths as paths_module
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
    printable_name,
    reads_as_cased_words,
    reads_as_words,
    template_path,
    templated_url,
    templates_a_segment,
)
from tests.unit.guide_harness import REPO_ROOT


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


TOKEN = "f3a9c2e1b7d4a6f0e2c8b1d9"  # noqa: S105 - a planted token, not a secret


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

    @pytest.mark.parametrize("url", ["javascript:void(0)", "javascript:doLogin()"])
    def test_a_url_of_another_scheme_holding_no_id_is_left_alone(self, url: str) -> None:
        """A javascript: action has no path, and this one holds no id."""
        assert templated_url(url) == url

    @pytest.mark.parametrize(
        "url",
        [
            f"javascript:login('{TOKEN}')",
            "javascript:go(12345678)",
            "javascript:go(12345)",
            "javascript:go('alice.secret@realuser-account.example', 'ref')",
            "javascript:go('alice.secret%40realuser-account.example', 'ref')",
            f"javascript:go('alice.secret@realuser-account.example', '{TOKEN}')",
        ],
    )
    def test_a_url_of_another_scheme_holding_an_id_is_templated(self, url: str) -> None:
        """A token or an id in a javascript: action is never printed."""
        assert templated_url(url) == "javascript:{id}"


class TestAnEmailInAnOpaqueUrlIsMasked:
    @pytest.mark.parametrize(
        "url",
        [
            "javascript:go('alice.secret@realuser-account.example', 'ref')",
            "javascript:go('alice.secret%40realuser-account.example', 'ref')",
        ],
    )
    def test_bare_url_masks_the_whole_action(self, url: str) -> None:
        """An email anywhere after the scheme, however many arguments surround it."""
        assert bare_url(url) == "javascript:{id}"

    def test_an_action_with_no_email_is_kept(self) -> None:
        assert bare_url("javascript:go('ref', 2)") == "javascript:go('ref', 2)"


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
            ("javascript:void(0)", False),
            ("javascript:doLogin()", False),
            (f"javascript:login('{TOKEN}')", True),
            ("javascript:go(12345678)", True),
            ("javascript:submitLogin(12345)", True),
            ("javascript:go('alice.secret@realuser-account.example', 'ref')", True),
            ("javascript:go('alice.secret%40realuser-account.example', 'ref')", True),
            ("HTTPS://myshop.example.com/accounts/12345/session", True),
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


class TestASegmentHoldingAnEmailAmongOtherTextIsMasked:
    """X2 (polish #212 round 21): _email and _is_email used to require the whole
    segment to be one email; both now search, so an email anywhere in the segment
    is caught."""

    def test_two_digit_free_emails_in_one_segment_are_masked(self) -> None:
        # A comma, not a ";": bare_url strips a ";..." tail as a matrix
        # parameter before any email check runs, which would leave only the
        # first email in the segment and defeat this case.
        segment = "alice@example.com,bob@example.net"
        assert holds_an_id(segment)
        assert (
            bare_url(f"https://myshop.example.com/contacts/{segment}")
            == "https://myshop.example.com/contacts/{contact_id}"
        )

    def test_a_display_name_form_is_masked(self) -> None:
        """A mailto-style "Name <email>" contact, percent-encoded as a URL segment."""
        segment = "Zq%20Planted%20%3Czq.planted@realmail.example%3E"
        assert holds_an_id(segment)
        masked = bare_url(f"https://myshop.example.com/contacts/{segment}")
        assert masked == "https://myshop.example.com/contacts/{contact_id}"
        assert "zq.planted" not in masked
        assert "realmail.example" not in masked


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


class TestReadsAsWords:
    @pytest.mark.parametrize(
        "value",
        [
            "running%20shoes",
            "running+shoes",
            "12",
            "12345",
            "2026-10-01",
            "10%2F01%2F2026",
            "en-US",
            "USD",
            "Monday",
            "page2",
            "transportations",
            "café",
            "naïve",
            "iPhone 15",
            "4K+TV",
            "2x4%20lumber",
            "macbook pro m3",
            "iPad+mini",
            "USB-C cable",
            "crème brûlée",
            "salt & pepper",
            "Tom+%26+Jerry",
            "black - white",
            "a / b",
            "512gb ssd",
            "500ml bottle",
            "12pk soda",
            "55inch tv",
            "10x10 tent",
            "12 34",
            "10 19 2026",
            "2026 10 19",
            "1 2 3",
            "12 34 56",
            "12345 678",
            "2 + 2",
            "5 10 15",
            "9.5 11",
            "v2",
            '{"q":"garden hose","page":3}',
        ],
    )
    def test_form_state_reads_as_words(self, value: str) -> None:
        assert reads_as_words(value)

    @pytest.mark.parametrize(
        "value",
        [
            "",
            "%20",
            "Zq9XkLmPwR4t7Bv2Hn8Ks3Jd",
            "x7Kq29Lp",
            "kQzPwM",
            "kqzpwmxtrv",
            "lalalalalalalalala",
            "lot 123456",
            "Zq9X+kLmP+wR4t+Bv2H",
            "Ab3+xYz9+kLm+Qr",
            "aB3x+Y7qW2e/R5tY8+uI9kL0",
            "12+/+34",
            "12 / 34",
            "512zq ssd",
            "x7Kq29Lp ann@myshop.example",
            "a3f9c2d1e0b4",
            "40912873",
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJl",
            "s%3AZq9XkLmPwR4t7Bv2.Hn8Ks3Jd",
        ],
    )
    def test_a_credential_shaped_or_empty_value_does_not(self, value: str) -> None:
        assert not reads_as_words(value)

    @pytest.mark.parametrize("length", [22, 24, 32, 44])
    def test_standard_base64_ids_of_session_length_do_not(self, length: int) -> None:
        """Plus-decoding splits a standard-base64 id at each +, so the several-words
        reading must not accept the pieces; 0 of 100,000 per length was measured."""
        alphabet = string.ascii_letters + string.digits + "+/"
        rng = random.Random(273)  # noqa: S311 - a fixed sample, not a secret
        ids = ["".join(rng.choices(alphabet, k=length)) for _ in range(5_000)]
        assert [value for value in ids if reads_as_words(value)] == []


_BASE64 = string.ascii_letters + string.digits + "+/"
_BASE64URL = string.ascii_letters + string.digits + "-_"
_HEX = string.digits + "abcdef"


def _random_value(shape: str, rng: random.Random) -> str:
    kind, _, size = shape.rpartition(" ")
    if shape == "uuid":
        return str(uuid.UUID(int=rng.getrandbits(128), version=4))
    if shape == "jwt signature":
        return "".join(rng.choices(_BASE64URL, k=43))
    length = int(size)
    if kind == "numeric":
        return str(rng.randrange(10 ** (length - 1), 10**length))
    alphabet = {
        "base64": _BASE64,
        "hex": _HEX,
        "lowercase": string.ascii_lowercase,
        "mixed-case": string.ascii_letters,
        "capitals": string.ascii_uppercase,
        "capitals and digits": string.ascii_uppercase + string.digits,
    }[kind]
    return "".join(rng.choices(alphabet, k=length))


# shape: the share of random values of that shape reads_as_cased_words accepts
# and reads_as_words rejects, so the share a cookie value the 1.24.0 rule flagged
# is no longer flagged for (#296). Each ceiling is the share over 100,000 other
# values of the shape (2026-10-24) plus three standard errors of the test's
# 10,000-value sample, sqrt(p * (1 - p) / 10,000), rounded up to 4 places; a
# shape with no loss in 100,000 has a ceiling of 0. The capital shapes are the
# rule's known limit: a word in capitals is judged only by its letter pairs, so
# an id of capitals and digits is lost when a draw holds no digit.
_CASED_WORD_LOSS_CEILINGS = {
    "base64 8": 0.0025,
    "base64 12": 0.0007,
    "base64 16": 0.0002,
    "base64 22": 0.0,
    "base64 32": 0.0,
    "hex 8": 0.0,
    "hex 16": 0.0,
    "hex 32": 0.0,
    "lowercase 8": 0.0,
    "lowercase 12": 0.0,
    "lowercase 16": 0.0,
    "lowercase 32": 0.0,
    "mixed-case 8": 0.0089,
    "mixed-case 12": 0.0041,
    "mixed-case 16": 0.0005,
    "mixed-case 32": 0.0,
    "uuid": 0.0,
    "jwt signature": 0.0,
    "numeric 5": 0.0,
    "numeric 6": 0.0,
    "numeric 8": 0.0,
    "numeric 10": 0.0,
    "capitals 4": 0.4374,
    "capitals 7": 0.204,
    "capitals 9": 0.125,
    "capitals 12": 0.0602,
    "capitals and digits 8": 0.0138,
    "capitals and digits 12": 0.0019,
    "capitals and digits 16": 0.0,
}


class TestReadsAsCasedWords:
    @pytest.mark.parametrize(
        "value",
        [
            "LANDOWNER",
            "GRANTEE",
            "BRAL",
            "NASA",
            "LEGO",
            "currentTab",
            "panelGRIDview",
            "startDate",
            "tabLISTresults",
            "searchByNAME",
            "searchByName",
            "viewPDF",
            "getXQZ",
            "parseHTMLString",
            "LISTviewResults",
            "HTMLParserView",
            "GRIDviewPanels",
            "Smith, John",
            "SMITH, JOHN",
            "ST. JOHN",
            "O'Brien Mary",
            "Smith-Jones, Ann",
            "GRANTEE, LANDOWNER Q.",
        ],
    )
    def test_a_word_in_capitals_or_camel_case_reads_as_cased_words(self, value: str) -> None:
        assert reads_as_cased_words(value)

    @pytest.mark.parametrize(
        "value",
        [
            "",
            '""',
            "iPhone",
            "iPad",
            "XL",
            "searchBxName",
            "tabXQZresults",
            "getXQZZ",
            "kQzPwMxT",
            "HTMLParser",
            "LISTview",
            "GRIDview",
            "QZXKmdotRwpq",
            "Smith4 John",
            "SMITH, J0HN",
            "Smith,John",
            "smith, john",
            "Smith, Kqzpwm",
            "QZXKWPRM",
            "CurrentTab",
            "currentTab2",
            "current tab",
            "ABCDEFGHIJKLMNOPQRST",
            "Zq9XkLmPwR4t7Bv2Hn8Ks3Jd",
        ],
    )
    def test_other_values_do_not(self, value: str) -> None:
        assert not reads_as_cased_words(value)

    @pytest.mark.parametrize("shape", sorted(_CASED_WORD_LOSS_CEILINGS))
    def test_few_random_values_reads_as_words_rejects_read_as_cased_words(self, shape: str) -> None:
        rng = random.Random(f"296-{shape}")  # noqa: S311 - a fixed sample, not a secret
        values = [_random_value(shape, rng) for _ in range(10_000)]
        lost = [v for v in values if reads_as_cased_words(v) and not reads_as_words(v)]
        assert len(lost) / len(values) <= _CASED_WORD_LOSS_CEILINGS[shape]

    @pytest.mark.parametrize(
        "value",
        [
            "".join(random.Random(296).choices(string.ascii_letters, k=200)),  # noqa: S311
            "aB" * 64,
            "".join(random.Random(297).choices(string.ascii_letters, k=1000)),  # noqa: S311
            "aB" * 22,
            "".join(random.Random(298).choices(string.ascii_letters + " ,.'-", k=1000)),  # noqa: S311
        ],
        ids=["random-200", "aB-64", "random-1000", "aB-22", "name-alphabet-1000"],
    )
    def test_a_long_value_is_judged_in_bounded_time(self, value: str) -> None:
        """The camelCase search once tried every split of every run of capitals,
        2 to the power of their number; a long value of letters must still
        return at once."""
        started = time.perf_counter()
        assert not reads_as_cased_words(value)
        assert time.perf_counter() - started < 0.25


class TestTheReportedFormStateShapes:
    """#296's reporter sent case masks of their form-state values, never the
    values. These are invented values with the same masks; the quoted and
    cleared forms go through the digest's quote and punctuation handling, tested
    in test_har_digest.py."""

    @pytest.mark.parametrize("value", ["GRANTEE", "LANDOWNER"], ids=["AAAAAAA", "AAAAAAAAA"])
    def test_a_word_of_7_or_9_capitals(self, value: str) -> None:
        assert reads_as_cased_words(value)
        assert not reads_as_words(value)

    def test_a_tab_name_of_4_capitals_4_lower_and_a_capitalised_6(self) -> None:
        """AAAAaaaaAaaaaa: camelCase that starts with a capital, 14 letters."""
        assert reads_as_cased_words("GRIDviewPanels")

    @pytest.mark.parametrize("value", ["BRL Grantees", "XQZ Grantees"])
    def test_a_name_of_3_capitals_and_a_capitalised_8_inside_its_quotes(self, value: str) -> None:
        """AAA Aaaaaaaa, the inside of the quoted value the digest takes the
        quotes off: a word of 3 capitals passes as typed, spelled or not."""
        assert reads_as_words(value)


# The graft skill's HAR reader runs on the standard library alone, so it carries
# its own copies of holds_an_id and the spelling pairs; these hold them equal.
_HAR_FACTS = REPO_ROOT / "skills" / "graft" / "scripts" / "har-facts.py"


def _load_har_facts() -> ModuleType:
    spec = importlib.util.spec_from_file_location("har_facts", _HAR_FACTS)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _id_corpus() -> list[str]:
    rng = random.Random(277)  # noqa: S311 - a fixed sample, not a secret
    alphabets = [
        string.ascii_letters + string.digits,
        string.ascii_letters + string.digits + "-_",
        string.ascii_letters + string.digits + "+/=",
        "0123456789abcdef",
        string.digits + "-.() ",
        string.ascii_lowercase + "_.-~$@",
    ]
    corpus = [
        "".join(rng.choices(rng.choice(alphabets), k=rng.randint(1, 40))) for _ in range(5_000)
    ]
    corpus += [str(uuid.UUID(int=rng.getrandbits(128), version=4)) for _ in range(200)]
    corpus += [
        "cus_NffrFeUfNV2Hib",
        "otp_cus_NffrFeUfNV2Hib",
        "jane.victim@example.com",
        "0xdeadbeefcafe12",
        "4111-1111-1111-1111",
        "2024-01-15",
        "(555)123-4567",
        "user_40912873",
        "ctl00_ContentPlaceHolder1_txtUserName",
        "csrfmiddlewaretoken",
        "a%40b.co",
        # Inputs that exercise each piece of the rule a one-line drift would break.
        "a~4111-1111-1111",
        "(555) 123-4567",
        "cus_nffrfeufnv2hib12",
        "cus_x~1",
        "otp~cus_NffrFeUfNV2Hib",
        "cus_NFFRFEUFNV2HIB12",
        "",
    ]
    return corpus


def test_the_har_readers_id_rule_is_graftpunks() -> None:
    script = _load_har_facts()
    differ = [text for text in _id_corpus() if script.holds_an_id(text) != holds_an_id(text)]
    assert differ == []


_COPIED = [
    "_MIN_HEX_LEN",
    "_MIN_PREFIXED_TAIL",
    "_MIN_BASE64_LEN",
    "_MIN_BASE64_SWITCHES",
    "_MAX_CAPITALS_RUN",
    "_VOWELS",
    "_WORD_CONSONANT_PAIRS",
]
_COPIED_PATTERNS = [
    "_EMAIL_SEARCH_RE",
    "_PART_SPLIT_RE",
    "_DIGIT_RUN_RE",
    "_MIXED_HEX_RE",
    "_PREFIXED_ID_RE",
    "_BASE64_RE",
    "_DATE_RE",
    "_PARENTHESISED_PHONE_RE",
    "_PREFIXED_HEX_RE",
]


@pytest.mark.parametrize("name", _COPIED)
def test_the_har_readers_copied_constants_are_graftpunks(name: str) -> None:
    assert getattr(_load_har_facts(), name) == getattr(paths_module, name)


@pytest.mark.parametrize("name", _COPIED_PATTERNS)
def test_the_har_readers_copied_patterns_are_graftpunks(name: str) -> None:
    ours, theirs = getattr(_load_har_facts(), name), getattr(paths_module, name)
    assert (ours.pattern, ours.flags) == (theirs.pattern, theirs.flags)


def test_the_har_readers_spelling_rule_is_graftpunks() -> None:
    script = _load_har_facts()
    rng = random.Random(277)  # noqa: S311 - a fixed sample, not a secret
    words = ["".join(rng.choices(string.ascii_letters, k=rng.randint(1, 12))) for _ in range(5_000)]
    words += ["csrf", "CSRF", "ABC", "ABCD", "upgrade", "jsessionid", "JSESSIONID", ""]
    assert [w for w in words if script._spelled(w) != paths_module._spelled(w)] == []


# The abbreviations cookie and token names use, several spelled with no vowel.
_TOKEN_WORDS = (  # noqa: SIM905 - one compact string on purpose: the list is data
    "csrf xsrf jwt sid sess auth oidc saml nonce sso mfa otp totp hotp tfa jti pkce hmac "
    "tkn idp jsid ssid"
).split()
# Names the review rounds of #315 found the gate must hold back: a nameless
# cookie's value recorded as its name, capitals dropped into a name, and token
# words with a random tail.
_HELD_BACK_PROBES = (
    "SECRETVALUEq7",
    "uid_SAMCOOKIE_X",
    "xsrfQz9XvKw",
    "authTokenQx7vK",
    "sessionZq9vLm2x",
    "XSRF_ZQVLKMXT",
)


@pytest.mark.parametrize("word", _TOKEN_WORDS)
def test_a_token_word_reads_as_a_name_in_any_case(word: str) -> None:
    for spelling in (word, word.capitalize(), word.upper()):
        assert printable_name(spelling), spelling


@pytest.mark.parametrize(
    "name",
    ["X-Csrf", "XCsrf", "csrf_token", "Csrf-Token", "xsrfToken", "JwtToken", "OidcState"],
)
def test_a_token_name_built_of_token_words_reads_as_a_name(name: str) -> None:
    assert printable_name(name)


@pytest.mark.parametrize("name", _HELD_BACK_PROBES)
def test_a_probe_the_reviews_held_back_is_still_held_back(name: str) -> None:
    assert not printable_name(name)


@pytest.mark.parametrize("name", ["QCsrf", "Os", "Js", "XQz", "Jsx"])
def test_a_lone_capital_or_a_short_acronym_in_lower_case_is_not_a_name_part(name: str) -> None:
    """Only ``X`` stands alone before a word, and an acronym of two letters
    counts in camel case only in capitals."""
    assert not printable_name(name)


def _name_corpus() -> list[str]:
    rng = random.Random(277)  # noqa: S311 - a fixed sample, not a secret
    name_alphabet = string.ascii_letters + string.digits + "_.-$[]"
    words = ["user", "Name", "ID", "session", "csrf", "Token", "txt", "ctl00", "form", "0", "12"]
    corpus = _id_corpus()
    corpus += ["".join(rng.choices(name_alphabet, k=rng.randint(1, 50))) for _ in range(5_000)]
    corpus += [
        rng.choice(["", "_", "__", "."]).join(rng.choices(words, k=rng.randint(1, 4)))
        + rng.choice(["", "1", "22", "333"])
        for _ in range(2_000)
    ]
    corpus += [
        "PHPSESSID",
        "__Host-sid",
        "XSRF-TOKEN",
        "SECRETVALUEq7",
        "uid_SAMCOOKIE_X",
        "ctl00$MainContent$txtUserName",
        "form-TOTAL_FORMS",
        "bvp-0-book",
        "xqzkvbtrwamplhab-0-name",
        "user[password]",
        "a" * 48,
        "a" * 49,
        *_HELD_BACK_PROBES,
    ]
    corpus += [
        spelling
        for word in _TOKEN_WORDS
        for spelling in (word, word.capitalize(), word.upper(), f"X-{word.capitalize()}")
    ]
    return corpus


def test_the_har_readers_name_gate_is_graftpunks() -> None:
    """The digest prints a cookie name only when har-facts.py would."""
    script = _load_har_facts()
    differ = [
        name for name in _name_corpus() if script.printable_name(name) != printable_name(name)
    ]
    assert differ == []


_COPIED_GATE = [
    "_MAX_NAME_LEN",
    "_ACRONYMS",
    "_SHORT_WORDS",
    "_MAX_PARTS",
    "_MAX_CAPITALS",
    "_LONG_LOWER",
    "_FRAMEWORK_NAMES",
    "_MIN_CASELESS_ACRONYM_LEN",
]
_COPIED_GATE_PATTERNS = [
    "_NAME_CHARS_RE",
    "_NAME_SPLIT_RE",
    "_CAMEL_WORD_RE",
    "_WEBFORMS_CONTAINER_RE",
    "_HUNGARIAN_RE",
    "_FORMSET_FIELD_RE",
    "_FORMSET_MANAGEMENT_RE",
    "_SHORT_PREFIX_RE",
]


@pytest.mark.parametrize("name", _COPIED_GATE)
def test_the_har_readers_name_gate_constants_are_graftpunks(name: str) -> None:
    assert getattr(_load_har_facts(), name) == getattr(paths_module, name)


@pytest.mark.parametrize("name", _COPIED_GATE_PATTERNS)
def test_the_har_readers_name_gate_patterns_are_graftpunks(name: str) -> None:
    ours, theirs = getattr(_load_har_facts(), name), getattr(paths_module, name)
    assert (ours.pattern, ours.flags) == (theirs.pattern, theirs.flags)
