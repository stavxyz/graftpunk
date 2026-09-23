"""RunDigest over synthetic HARs built in tmp_path (plugin tooling spec, 2026-09-11)."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

import pytest
from structlog.testing import capture_logs

from graftpunk.har.digest import (
    _BODY_SAMPLE_THRESHOLD,
    _DYNAMIC_MAJORITY,
    _FORM_CONTENT_TYPE,
    _HIGH_CARDINALITY_THRESHOLD,
    _LOGIN_WINDOW,
    _MAX_ENDPOINT_EXAMPLES,
    _MAX_FIELD_NAME_LEN,
    _SHAPE_MAX_DEPTH,
    _SHAPE_MAX_KEYS,
    SHAPE_UNAVAILABLE,
    DigestSource,
    Endpoint,
    LoginObservation,
    ObservationKind,
    _parse_body,
    _with_login_flow,
    body_params,
    digest,
    endpoint_template,
    flagged_names_of,
)
from graftpunk.har.parser import parse_har_file
from graftpunk.har.report import render_endpoints_json


def _entry(
    method: str,
    url: str,
    *,
    status: int = 200,
    content_type: str = "application/json",
    body: str = "{}",
    request_headers: dict[str, str] | None = None,
    response_headers: dict[str, str] | None = None,
    post_data: str | None = None,
    set_cookies: list[str] | None = None,
    started: str = "2026-09-10T10:00:00.000Z",
) -> dict:
    resp_headers = [{"name": "Content-Type", "value": content_type}]
    for name, value in (response_headers or {}).items():
        resp_headers.append({"name": name, "value": value})
    for cookie in set_cookies or []:
        resp_headers.append({"name": "Set-Cookie", "value": cookie})
    entry = {
        "startedDateTime": started,
        "time": 5,
        "request": {
            "method": method,
            "url": url,
            "headers": [{"name": k, "value": v} for k, v in (request_headers or {}).items()],
            "cookies": [],
            "queryString": [],
        },
        "response": {
            "status": status,
            "statusText": "OK",
            "headers": resp_headers,
            "cookies": [],
            "content": {"mimeType": content_type, "text": body, "size": len(body)},
        },
    }
    if post_data is not None:
        entry["request"]["postData"] = {"mimeType": "application/json", "text": post_data}
    return entry


def _write_har(tmp_path: Path, entries: list[dict], name: str = "network.har") -> Path:
    har_path = tmp_path / name
    har_path.write_text(json.dumps({"log": {"version": "1.2", "entries": entries}}))
    return har_path


def _nested_dict(levels: int) -> dict[str, Any]:
    """A dict nested `levels` deep: {"level0": {"level1": {...: "too deep"}}}."""
    value: Any = "too deep"
    for i in reversed(range(levels)):
        value = {f"level{i}": value}
    return value


class TestPrimaryHostAndHosts:
    def test_primary_host_is_most_common_non_static(self, tmp_path: Path) -> None:
        entries = [
            _entry("GET", "https://api.myshop.example.com/orders"),
            _entry("GET", "https://api.myshop.example.com/orders/1"),
            _entry(
                "GET",
                "https://ads.tracker.example.net/pixel.gif",
                content_type="image/gif",
                body="",
            ),
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert result.primary_host == "api.myshop.example.com"

    def test_a_document_host_outranks_a_busier_beacon_host(self, tmp_path: Path) -> None:
        """A page-driven site answers its own pages and serves the rest as
        assets, so a telemetry endpoint can out-count it on non-static requests
        alone (polish round 2, 2026-09-12)."""
        entries = [
            _entry(
                "GET",
                "https://myshop.example.com/records/search",
                content_type="text/html; charset=utf-8",
                body="<html><body>results</body></html>",
            ),
            _entry(
                "GET",
                "https://myshop.example.com/records/1",
                content_type="text/html; charset=utf-8",
                body="<html><body>one</body></html>",
            ),
        ]
        entries += [_entry("POST", f"https://sessions.telemetry.example.net/{i}") for i in range(5)]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert result.primary_host == "myshop.example.com"
        assert result.dropped["third_party"] == 5

    def test_an_html_error_page_does_not_make_a_host_a_document_host(self, tmp_path: Path) -> None:
        """Only a served page counts: a beacon host answering one HTML 500
        must not join the document group and then win on count."""
        entries = [
            _entry(
                "GET",
                "https://myshop.example.com/records/search",
                content_type="text/html; charset=utf-8",
                body="<html><body>results</body></html>",
            ),
            _entry(
                "POST",
                "https://sessions.telemetry.example.net/collect",
                status=500,
                content_type="text/html; charset=utf-8",
                body="<html><body>server error</body></html>",
            ),
        ]
        entries += [_entry("POST", f"https://sessions.telemetry.example.net/{i}") for i in range(4)]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert result.primary_host == "myshop.example.com"

    def test_an_api_only_run_still_elects_on_count_alone(self, tmp_path: Path) -> None:
        """Nothing served HTML, so the document half of the rule decides
        nothing and the busiest non-static host wins as it always did."""
        entries = [_entry("GET", "https://api.myshop.example.com/orders")]
        entries += [_entry("GET", f"https://api.other.example.org/t/{i}") for i in range(3)]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert result.primary_host == "api.other.example.org"

    def test_hosts_dict_counts_every_host(self, tmp_path: Path) -> None:
        entries = [
            _entry("GET", "https://api.myshop.example.com/orders"),
            _entry(
                "GET", "https://cdn.myshop.example.com/logo.png", content_type="image/png", body=""
            ),
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert result.hosts["api.myshop.example.com"] == 1
        assert result.hosts["cdn.myshop.example.com"] == 1


class TestStaticAndThirdPartyExclusion:
    def test_static_extension_is_dropped(self, tmp_path: Path) -> None:
        entries = [
            _entry("GET", "https://api.myshop.example.com/orders"),
            _entry(
                "GET",
                "https://api.myshop.example.com/app.js",
                content_type="application/javascript",
                body="",
            ),
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert result.dropped["static"] == 1
        assert not any(e.template == "/app.js" for e in result.endpoints)

    def test_image_content_type_is_static_regardless_of_url(self, tmp_path: Path) -> None:
        entries = [
            _entry("GET", "https://api.myshop.example.com/orders"),
            _entry(
                "GET",
                "https://api.myshop.example.com/generated-thumb",
                content_type="image/jpeg",
                body="",
            ),
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert result.dropped["static"] == 1

    def test_a_hashed_asset_with_an_unlisted_extension_is_static(self, tmp_path: Path) -> None:
        """The extension list caught .js and .css but not ._hs, so a hashed
        hyperscript asset became an endpoint, a command stub, and a generated
        test (polish round 2, 2026-09-12)."""
        entries = [
            _entry("GET", "https://api.myshop.example.com/orders"),
            _entry(
                "GET",
                "https://api.myshop.example.com/vendor/custom.4a0ba46ee0b6b964b44b2909b6._hs",
                content_type="text/hyperscript",
                body="on click log 'hi'",
            ),
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert result.dropped["static"] == 1
        assert all("vendor" not in e.template for e in result.endpoints)

    def test_a_content_type_parameter_does_not_hide_a_static_type(self, tmp_path: Path) -> None:
        entries = [
            _entry("GET", "https://api.myshop.example.com/orders"),
            _entry(
                "GET",
                "https://api.myshop.example.com/bundle",
                content_type="text/css; charset=utf-8",
                body="body{}",
            ),
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert result.dropped["static"] == 1

    def test_a_json_response_under_an_unusual_path_is_kept(self, tmp_path: Path) -> None:
        entries = [
            _entry("GET", "https://api.myshop.example.com/orders"),
            _entry(
                "GET",
                "https://api.myshop.example.com/vendor/custom.4a0ba46ee0b6b964b44b2909b6._hs",
                content_type="application/json",
                body='{"ok": true}',
            ),
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert result.dropped["static"] == 0
        assert any("vendor" in e.template for e in result.endpoints)

    def test_an_html_page_is_kept(self, tmp_path: Path) -> None:
        entries = [
            _entry("GET", "https://api.myshop.example.com/orders"),
            _entry(
                "GET",
                "https://api.myshop.example.com/records/search",
                content_type="text/html; charset=utf-8",
                body="<html><body>results</body></html>",
            ),
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert result.dropped["static"] == 0
        templates = {e.template for e in result.endpoints}
        assert "/records/search" in templates

    def test_a_first_party_analytics_path_is_kept(self, tmp_path: Path) -> None:
        """The word analytics names a tracker host, not a path: matched anywhere
        in the URL it dropped the primary host's own reporting endpoint."""
        entries = [
            _entry("GET", "https://api.myshop.example.com/orders"),
            _entry("GET", "https://api.myshop.example.com/api/analytics/summary"),
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        templates = {e.template for e in result.endpoints}
        assert "/api/analytics/summary" in templates
        assert result.dropped["static"] == 0

    def test_an_asset_host_is_still_dropped(self, tmp_path: Path) -> None:
        entries = [
            _entry("GET", "https://api.myshop.example.com/orders"),
            _entry("GET", "https://cdn.example.com/app.js", content_type="text/plain", body="x=1"),
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert result.dropped["static"] == 1
        assert all("app.js" not in e.template for e in result.endpoints)

    def test_a_tracker_host_is_still_dropped(self, tmp_path: Path) -> None:
        entries = [
            _entry("GET", "https://api.myshop.example.com/orders"),
            _entry("GET", "https://analytics.example.net/collect"),
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert result.dropped["static"] == 1

    def test_a_tracking_pixel_path_segment_is_dropped(self, tmp_path: Path) -> None:
        entries = [
            _entry("GET", "https://api.myshop.example.com/orders"),
            _entry("GET", "https://api.myshop.example.com/pixel"),
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert result.dropped["static"] == 1

    def test_a_segment_merely_containing_an_excluded_word_is_kept(self, tmp_path: Path) -> None:
        entries = [
            _entry("GET", "https://api.myshop.example.com/orders"),
            _entry("GET", "https://api.myshop.example.com/pixelate-image"),
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        templates = {e.template for e in result.endpoints}
        assert "/pixelate-image" in templates
        assert result.dropped["static"] == 0

    def test_third_party_host_dropped_without_all_hosts(self, tmp_path: Path) -> None:
        entries = [
            _entry("GET", "https://api.myshop.example.com/orders"),
            _entry("GET", "https://api.myshop.example.com/orders/2"),
            _entry("GET", "https://other.example.org/data"),
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert result.dropped["third_party"] == 1
        assert all(e.host == "api.myshop.example.com" for e in result.endpoints)

    def test_a_deep_primary_host_does_not_widen_the_scope(self, tmp_path: Path) -> None:
        """Taking the last two labels made every host sharing them a first
        party, which under a two-label public suffix pulls in the whole
        country-code domain. The scope root is the primary host's parent
        instead: team.example.com here, so other.example.com is third party."""
        entries = [
            _entry("GET", "https://shop.team.example.com/orders"),
            _entry("GET", "https://shop.team.example.com/orders/2"),
            _entry("GET", "https://api.team.example.com/data"),
            _entry("GET", "https://other.example.com/data"),
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert result.primary_host == "shop.team.example.com"
        hosts = {e.host for e in result.endpoints}
        assert hosts == {"shop.team.example.com", "api.team.example.com"}
        assert result.dropped["third_party"] == 1

    def test_a_two_label_primary_host_keeps_its_own_subtree(self, tmp_path: Path) -> None:
        # "other." rather than "cdn."/"assets.": those are static-exclusion
        # patterns, and this test is about the scope rule, not that one.
        entries = [
            _entry("GET", "https://www.example.com/orders"),
            _entry("GET", "https://www.example.com/orders/2"),
            _entry("GET", "https://api.example.com/data"),
            _entry("GET", "https://other.example.net/data"),
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert result.primary_host == "www.example.com"
        hosts = {e.host for e in result.endpoints}
        assert hosts == {"www.example.com", "api.example.com"}
        assert result.dropped["third_party"] == 1

    def test_all_hosts_models_every_host(self, tmp_path: Path) -> None:
        entries = [
            _entry("GET", "https://api.myshop.example.com/orders"),
            _entry("GET", "https://api.myshop.example.com/orders/2"),
            _entry("GET", "https://other.example.org/data"),
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)), all_hosts=True)
        assert any(e.host == "other.example.org" for e in result.endpoints)
        assert result.dropped["third_party"] == 0


class TestNonHttpSchemes:
    """A capture taken before the first navigation holds the browser's own
    new-tab page, whose entries are not HTTP at all (polish round 2,
    2026-09-12)."""

    def _mixed_har(self, tmp_path: Path) -> Path:
        entries = [
            _entry("GET", "https://api.myshop.example.com/orders"),
            _entry("GET", "chrome://new-tab-page/", content_type="text/html", body="<html>"),
            _entry("GET", "chrome-untrusted://theme/colors.css", content_type="text/css", body=""),
            _entry("GET", "data:image/png;base64,AAAA", content_type="image/png", body=""),
            _entry("GET", "blob:https://api.myshop.example.com/1234", body="{}"),
        ]
        return _write_har(tmp_path, entries)

    def test_non_http_entries_are_dropped_as_other_scheme(self, tmp_path: Path) -> None:
        result = digest(DigestSource.from_har(self._mixed_har(tmp_path)))
        assert result.dropped["other_scheme"] == 4

    def test_a_pseudo_host_never_reaches_the_host_counts(self, tmp_path: Path) -> None:
        result = digest(DigestSource.from_har(self._mixed_har(tmp_path)))
        assert set(result.hosts) == {"api.myshop.example.com"}
        assert result.primary_host == "api.myshop.example.com"

    def test_a_non_http_entry_is_never_an_endpoint(self, tmp_path: Path) -> None:
        result = digest(DigestSource.from_har(self._mixed_har(tmp_path)))
        assert [e.template for e in result.endpoints] == ["/orders"]

    def test_the_scheme_rule_runs_before_the_static_rule(self, tmp_path: Path) -> None:
        """A non-HTTP entry counts once, under its own reason, even when its
        content type would also have made it static."""
        result = digest(DigestSource.from_har(self._mixed_har(tmp_path)))
        assert result.dropped["static"] == 0
        assert result.dropped["third_party"] == 0


class TestTypeObservation:
    def test_query_param_int_type(self, tmp_path: Path) -> None:
        entries = [_entry("GET", "https://api.myshop.example.com/orders?page=1")]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        endpoint = result.endpoints[0]
        assert endpoint.query_params["page"] == "int"

    def test_query_param_bool_type(self, tmp_path: Path) -> None:
        entries = [_entry("GET", "https://api.myshop.example.com/orders?archived=true")]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert result.endpoints[0].query_params["archived"] == "bool"

    def test_body_param_types_on_post(self, tmp_path: Path) -> None:
        entries = [
            _entry(
                "POST",
                "https://api.myshop.example.com/orders",
                post_data=json.dumps({"quantity": 3, "gift": True, "note": "hi"}),
            )
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        endpoint = result.endpoints[0]
        assert endpoint.body_params["quantity"] == "int"
        assert endpoint.body_params["gift"] == "bool"
        assert endpoint.body_params["note"] == "str"

    def test_a_json_float_is_float_not_int(self, tmp_path: Path) -> None:
        """An int-typed option refuses --amount 12.5, so a float is its own type."""
        entries = [
            _entry(
                "POST",
                "https://api.myshop.example.com/orders",
                post_data=json.dumps({"amount": 12.5, "quantity": 3, "gift": True}),
            )
        ]
        endpoint = digest(DigestSource.from_har(_write_har(tmp_path, entries))).endpoints[0]
        assert endpoint.body_params == {"amount": "float", "quantity": "int", "gift": "bool"}

    def test_a_query_float_is_float(self, tmp_path: Path) -> None:
        entries = [_entry("GET", "https://api.myshop.example.com/orders?min_total=12.5")]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert result.endpoints[0].query_params["min_total"] == "float"

    @pytest.mark.parametrize(
        "value",
        ["07030", "007", "+5", " 5", "1_000", "12.50", "1e5", "inf", "nan", "True", "FALSE"],
    )
    def test_a_value_is_typed_only_when_the_typed_value_spells_it_the_same(
        self, tmp_path: Path, value: str
    ) -> None:
        """A postal code 07030 typed int would be sent as 7030; a bool is sent as
        lowercase true/false, so a recorded True stays text."""
        entries = [_entry("GET", f"https://api.myshop.example.com/orders?code={value}")]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert result.endpoints[0].query_params["code"] == "str"

    @pytest.mark.parametrize(
        ("value", "expected"), [("-5", "int"), ("0", "int"), ("-0.5", "float")]
    )
    def test_a_value_that_round_trips_keeps_its_type(
        self, tmp_path: Path, value: str, expected: str
    ) -> None:
        entries = [_entry("GET", f"https://api.myshop.example.com/orders?code={value}")]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert result.endpoints[0].query_params["code"] == expected

    def test_query_types_that_disagree_across_requests_fall_back_to_str(
        self, tmp_path: Path
    ) -> None:
        """Seen as abc then 1: the last request used to win, and the command then
        refused abc."""
        entries = [
            _entry("GET", "https://api.myshop.example.com/orders?q=abc&page=1"),
            _entry("GET", "https://api.myshop.example.com/orders?q=1&page=2"),
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert result.endpoints[0].query_params == {"q": "str", "page": "int"}

    def test_a_query_key_that_does_not_read_as_a_field_name_is_dropped(
        self, tmp_path: Path
    ) -> None:
        """The body-key rule applies to query keys too: an email-shaped or digit
        key is data, and it reached the projection and generated source."""
        entries = [
            _entry(
                "GET",
                "https://api.myshop.example.com/orders?alice@example.com&4111111111111111=1&page=2",
            )
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert result.endpoints[0].query_params == {"page": "int"}
        assert "alice@example.com" not in render_endpoints_json(result)

    @staticmethod
    def _json_posts(tmp_path: Path, *bodies: dict) -> Endpoint:
        entries = [
            _entry("POST", "https://api.myshop.example.com/orders", post_data=json.dumps(body))
            for body in bodies
        ]
        (endpoint,) = digest(DigestSource.from_har(_write_har(tmp_path, entries))).endpoints
        return endpoint

    @staticmethod
    def _form_posts(tmp_path: Path, *bodies: str) -> Endpoint:
        entries = []
        for body in bodies:
            entry = _entry("POST", "https://api.myshop.example.com/orders", post_data=body)
            entry["request"]["postData"]["mimeType"] = _FORM_CONTENT_TYPE
            entries.append(entry)
        (endpoint,) = digest(DigestSource.from_har(_write_har(tmp_path, entries))).endpoints
        return endpoint

    def test_a_query_key_holding_an_id_is_dropped(self, tmp_path: Path) -> None:
        """A key is a field name, not a place for an account's id to ride along."""
        entries = [
            _entry(
                "GET",
                "https://api.myshop.example.com/orders"
                "?u_40912873=1&k_ab12cd34ef=2&page=1&sha256=x&v2=y",
            )
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert result.endpoints[0].query_params == {"page": "int", "sha256": "str", "v2": "str"}

    def test_a_body_key_holding_an_id_is_dropped(self, tmp_path: Path) -> None:
        endpoint = self._json_posts(tmp_path, {"acct_40912873": 1, "ab12cd34": 2, "note": "x"})
        assert endpoint.body_params == {"note": "str"}

    def test_a_form_key_holding_an_id_is_dropped_and_the_body_is_still_a_form(
        self, tmp_path: Path
    ) -> None:
        endpoint = self._form_posts(tmp_path, "u_40912873=1&name=alice")
        assert endpoint.body_kind == "form"
        assert endpoint.body_params == {"name": "str"}

    def test_json_int_then_float_merges_to_float(self, tmp_path: Path) -> None:
        endpoint = self._json_posts(tmp_path, {"amount": 3}, {"amount": 3.5})
        assert endpoint.body_params == {"amount": "float"}

    def test_a_json_null_is_not_a_type_observation(self, tmp_path: Path) -> None:
        endpoint = self._json_posts(tmp_path, {"amount": None}, {"amount": 3})
        assert endpoint.body_params == {"amount": "int"}

    def test_a_field_only_ever_null_is_not_declared(self, tmp_path: Path) -> None:
        endpoint = self._json_posts(tmp_path, {"amount": None, "note": "x"})
        assert endpoint.body_params == {"note": "str"}

    def test_json_text_then_number_is_mixed(self, tmp_path: Path) -> None:
        """No one type sends both, so the generator must not declare it."""
        endpoint = self._json_posts(tmp_path, {"ref": "a"}, {"ref": 3})
        assert endpoint.body_params == {"ref": "mixed"}

    def test_json_mixed_is_final(self, tmp_path: Path) -> None:
        endpoint = self._json_posts(tmp_path, {"ref": "a"}, {"ref": 3}, {"ref": "b"})
        assert endpoint.body_params == {"ref": "mixed"}

    def test_form_int_then_text_is_str(self, tmp_path: Path) -> None:
        endpoint = self._form_posts(tmp_path, "qty=3", "qty=abc")
        assert endpoint.body_params == {"qty": "str"}

    def test_a_json_object_is_object(self, tmp_path: Path) -> None:
        endpoint = self._json_posts(tmp_path, {"address": {"city": "x"}})
        assert endpoint.body_params == {"address": "object"}

    def test_a_json_array_records_its_element_type(self, tmp_path: Path) -> None:
        endpoint = self._json_posts(
            tmp_path,
            {"ids": [1, 2], "tags": ["a"], "prices": [1, 2.5], "mix": [1, "a"], "none": []},
        )
        assert endpoint.body_params == {
            "ids": "list[int]",
            "tags": "list[str]",
            "prices": "list[float]",
            "mix": "list[mixed]",
            "none": "list[unknown]",
        }

    def test_json_array_element_types_merge_across_requests(self, tmp_path: Path) -> None:
        endpoint = self._json_posts(
            tmp_path, {"ids": [], "codes": [1]}, {"ids": [4], "codes": ["a"]}
        )
        assert endpoint.body_params == {"ids": "list[int]", "codes": "list[mixed]"}

    def test_a_json_array_and_a_scalar_are_mixed(self, tmp_path: Path) -> None:
        endpoint = self._json_posts(tmp_path, {"ids": [1]}, {"ids": 1})
        assert endpoint.body_params == {"ids": "mixed"}

    def test_a_repeated_query_key_records_its_element_type(self, tmp_path: Path) -> None:
        entries = [
            _entry("GET", "https://api.myshop.example.com/orders?id=1&id=2&tag=a&tag=1"),
            _entry("GET", "https://api.myshop.example.com/orders?id=3"),
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert result.endpoints[0].query_params == {"id": "list[int]", "tag": "list[str]"}

    def test_a_repeated_form_key_records_its_element_type(self, tmp_path: Path) -> None:
        endpoint = self._form_posts(tmp_path, "id=1&id=2")
        assert endpoint.body_params == {"id": "list[int]"}


class TestBodyParams:
    """The one owner of body-param parsing: digest()'s endpoint accumulation
    and the fixtures sidecar (Task 7) both call this directly
    (validation net-negative, addressed 2026-09-12)."""

    def test_json_body_field_types(self, tmp_path: Path) -> None:
        entries = [
            _entry(
                "POST",
                "https://api.myshop.example.com/orders",
                post_data=json.dumps({"quantity": 3, "gift": True}),
            )
        ]
        (entry,) = parse_har_file(_write_har(tmp_path, entries)).entries
        assert body_params(entry) == {"quantity": "int", "gift": "bool"}

    def test_form_encoded_body_field_types(self, tmp_path: Path) -> None:
        entry_dict = _entry("POST", "https://api.myshop.example.com/login")
        entry_dict["request"]["postData"] = {
            "mimeType": "application/x-www-form-urlencoded",
            "text": "username=alice&remember=true",
        }
        (entry,) = parse_har_file(_write_har(tmp_path, [entry_dict])).entries
        assert body_params(entry) == {"username": "str", "remember": "bool"}

    def test_no_body_is_empty(self, tmp_path: Path) -> None:
        entries = [_entry("GET", "https://api.myshop.example.com/orders")]
        (entry,) = parse_har_file(_write_har(tmp_path, entries)).entries
        assert body_params(entry) == {}

    def test_a_json_key_that_does_not_read_as_a_field_name_is_dropped(self, tmp_path: Path) -> None:
        """A JSON object keyed by data (an email address used as a map key, a
        session id) is not a field name, held to the same rule
        (_plausible_field_name) a form body's keys already were: reporting the
        key itself put the data into the digest report and the fixtures sidecar."""
        long_hex_key = "a" * 128
        entries = [
            _entry(
                "POST",
                "https://api.myshop.example.com/orders",
                post_data=json.dumps(
                    {
                        "quantity": 3,
                        "alice@example.com": {"role": "owner"},
                        long_hex_key: "planted",
                    }
                ),
            )
        ]
        (entry,) = parse_har_file(_write_har(tmp_path, entries)).entries
        assert body_params(entry) == {"quantity": "int"}


class TestNonFormBodiesHaveNoFieldNames:
    """parse_qs returns the whole text as one key for anything that is not a
    form, which put an XML credential post's entire body (values included) into
    Endpoint.body_params and everything downstream of it."""

    @staticmethod
    def _parsed(tmp_path: Path, *, mime_type: str, text: str) -> tuple[dict[str, str], str]:
        entry_dict = _entry("POST", "https://api.myshop.example.com/submit")
        entry_dict["request"]["postData"] = {"mimeType": mime_type, "text": text}
        (entry,) = parse_har_file(_write_har(tmp_path, [entry_dict])).entries
        return _parse_body(entry)

    def test_json_object_is_a_json_body_with_field_names(self, tmp_path: Path) -> None:
        types, kind = self._parsed(
            tmp_path, mime_type="application/json", text=json.dumps({"quantity": 3})
        )
        assert (types, kind) == ({"quantity": "int"}, "json")

    def test_json_array_is_a_json_body_with_no_field_names(self, tmp_path: Path) -> None:
        types, kind = self._parsed(
            tmp_path, mime_type="application/json", text=json.dumps([{"sku": "a"}, {"sku": "b"}])
        )
        assert (types, kind) == ({}, "json")

    def test_json_scalar_is_a_json_body_with_no_field_names(self, tmp_path: Path) -> None:
        types, kind = self._parsed(tmp_path, mime_type="application/json", text='"just-a-string"')
        assert (types, kind) == ({}, "json")

    def test_an_xml_credential_post_yields_nothing(self, tmp_path: Path) -> None:
        xml = "<login><account>SECRET-ACCT-99</account><password>hunter2</password></login>"
        types, kind = self._parsed(tmp_path, mime_type="application/xml", text=xml)
        assert (types, kind) == ({}, "none")

    def test_plain_text_yields_nothing(self, tmp_path: Path) -> None:
        types, kind = self._parsed(
            tmp_path, mime_type="text/plain", text="a sentence with no equals sign"
        )
        assert (types, kind) == ({}, "none")

    def test_a_form_body_declared_as_something_else_is_not_read_as_a_form(
        self, tmp_path: Path
    ) -> None:
        types, kind = self._parsed(tmp_path, mime_type="text/plain", text="username=alice")
        assert (types, kind) == ({}, "none")

    def test_a_field_name_past_the_cap_disqualifies_the_body(self, tmp_path: Path) -> None:
        long_name = "f" * (_MAX_FIELD_NAME_LEN + 1)
        types, kind = self._parsed(
            tmp_path, mime_type=_FORM_CONTENT_TYPE, text=f"{long_name}=value"
        )
        assert (types, kind) == ({}, "none")

    def test_a_field_name_at_the_cap_is_accepted(self, tmp_path: Path) -> None:
        # Not hex: 64 f's is a hex token, which the id rule drops.
        name = "z" * _MAX_FIELD_NAME_LEN
        types, kind = self._parsed(tmp_path, mime_type=_FORM_CONTENT_TYPE, text=f"{name}=value")
        assert (types, kind) == ({name: "str"}, "form")

    def test_an_xml_credential_post_is_not_a_login_observation(self, tmp_path: Path) -> None:
        """No field names means no password hint: the digest reports no
        credential post rather than one whose 'field name' is the body."""
        entry_dict = _entry("POST", "https://api.myshop.example.com/submit")
        entry_dict["request"]["postData"] = {
            "mimeType": "application/xml",
            "text": "<login><account>SECRET-ACCT-99</account><password>hunter2</password></login>",
        }
        result = digest(DigestSource.from_har(_write_har(tmp_path, [entry_dict])))
        assert not any(o.kind == "credential_post" for o in result.login)
        assert "SECRET-ACCT-99" not in repr(result)


class TestShapeNode:
    def test_object_shape_has_children(self, tmp_path: Path) -> None:
        entries = [
            _entry(
                "GET",
                "https://api.myshop.example.com/orders/1",
                body=json.dumps({"id": 1, "total": 9.5, "paid": True}),
            )
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        shape = result.endpoints[0].shape
        assert shape is not None
        assert shape.kind == "object"
        assert shape.children is not None
        assert shape.children["id"].kind == "number"
        assert shape.children["paid"].kind == "boolean"

    def test_depth_beyond_the_max_is_truncated(self, tmp_path: Path) -> None:
        nested = _nested_dict(_SHAPE_MAX_DEPTH + 1)
        entries = [_entry("GET", "https://api.myshop.example.com/nested", body=json.dumps(nested))]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        shape = result.endpoints[0].shape
        assert shape is not None
        node = shape
        for i in range(_SHAPE_MAX_DEPTH):
            assert node is not None
            assert node.children is not None
            node = node.children[f"level{i}"]
        assert node.truncated is True

    def test_more_than_12_keys_is_truncated(self, tmp_path: Path) -> None:
        wide = {f"key{i}": i for i in range(_SHAPE_MAX_KEYS + 3)}
        entries = [_entry("GET", "https://api.myshop.example.com/wide", body=json.dumps(wide))]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        shape = result.endpoints[0].shape
        assert shape is not None
        assert shape.truncated is True
        assert len(shape.children) == _SHAPE_MAX_KEYS

    def test_a_json_body_over_the_threshold_still_reports_its_shape(self, tmp_path: Path) -> None:
        """The body is parsed whole. Parsing a fixed-size prefix of it could
        never succeed, so every large body reported non-JSON."""
        padding = "x" * _BODY_SAMPLE_THRESHOLD
        body = json.dumps({"id": 1, "padding": padding})
        assert len(body.encode("utf-8")) > _BODY_SAMPLE_THRESHOLD
        entries = [_entry("GET", "https://api.myshop.example.com/big", body=body)]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        shape = result.endpoints[0].shape
        assert shape is not None
        assert shape.kind == "object"
        assert shape.children is not None
        assert shape.children["id"].kind == "number"

    def test_an_unparseable_body_over_the_threshold_is_unavailable_not_non_json(
        self, tmp_path: Path
    ) -> None:
        truncated = '{"orders": [' + '{"id": 1},' * 40000
        assert len(truncated.encode("utf-8")) > _BODY_SAMPLE_THRESHOLD
        entries = [_entry("GET", "https://api.myshop.example.com/big", body=truncated)]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert result.endpoints[0].shape == SHAPE_UNAVAILABLE

    def test_a_small_unparseable_json_body_is_still_shapeless(self, tmp_path: Path) -> None:
        entries = [_entry("GET", "https://api.myshop.example.com/broken", body="not json at all")]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert result.endpoints[0].shape is None

    def test_non_json_response_has_no_shape(self, tmp_path: Path) -> None:
        entries = [
            _entry(
                "GET",
                "https://api.myshop.example.com/page",
                content_type="text/html",
                body="<html></html>",
            )
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert result.endpoints[0].shape is None


class TestCustomHeaders:
    def test_non_standard_header_name_reported(self, tmp_path: Path) -> None:
        entries = [
            _entry(
                "GET",
                "https://api.myshop.example.com/orders",
                request_headers={"X-Shop-Client": "web", "Accept": "application/json"},
            )
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert "X-Shop-Client" in result.endpoints[0].custom_headers
        assert "Accept" not in result.endpoints[0].custom_headers

    def test_header_values_never_appear_anywhere_in_the_model(self, tmp_path: Path) -> None:
        entries = [
            _entry(
                "GET",
                "https://api.myshop.example.com/orders",
                request_headers={"X-Shop-Client": "super-secret-build-id-42"},
            )
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert "super-secret-build-id-42" not in repr(result)


class TestLoginObservations:
    def test_form_page_observation(self, tmp_path: Path) -> None:
        entries = [
            _entry(
                "GET",
                "https://api.myshop.example.com/login",
                content_type="text/html",
                body='<form><input type="password" name="pw"></form>',
            )
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert result.login[0].kind == "form_page"

    def test_credential_post_lists_field_names_never_values(self, tmp_path: Path) -> None:
        entries = [
            _entry(
                "POST",
                "https://api.myshop.example.com/login",
                post_data=json.dumps({"email": "alice@example.com", "password": "hunter2"}),
            )
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        (observation,) = [o for o in result.login if o.kind == "credential_post"]
        assert set(observation.fields) == {"email", "password"}
        assert "hunter2" not in repr(result)

    def test_redirect_within_window_after_credential_post_is_an_observation(
        self, tmp_path: Path
    ) -> None:
        entries = [
            _entry(
                "POST",
                "https://api.myshop.example.com/login",
                post_data=json.dumps({"password": "x"}),
            ),
            _entry("GET", "https://api.myshop.example.com/dashboard", status=302, body=""),
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert any(o.kind == "redirect" for o in result.login)

    def test_a_redirecting_credential_post_carries_its_target(self, tmp_path: Path) -> None:
        """The common shape: the POST itself answers 302, so it is the only record."""
        entries = [
            _entry(
                "POST",
                "https://api.myshop.example.com/login",
                status=302,
                body="",
                post_data=json.dumps({"password": "x"}),
                response_headers={"Location": "https://api.myshop.example.com/dashboard?welcome=1"},
            )
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        (observation,) = [o for o in result.login if o.kind == "credential_post"]
        assert observation.redirect_to == "/dashboard"

    def test_a_following_redirect_carries_its_own_target(self, tmp_path: Path) -> None:
        """Each hop of the chain records where it sent the client next."""
        entries = [
            _entry(
                "POST",
                "https://api.myshop.example.com/login",
                status=302,
                body="",
                post_data=json.dumps({"password": "x"}),
                response_headers={"Location": "/auth/callback"},
            ),
            _entry(
                "GET",
                "https://api.myshop.example.com/auth/callback",
                status=302,
                body="",
                response_headers={"Location": "/dashboard"},
            ),
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        targets = [o.redirect_to for o in result.login]
        assert targets == ["/auth/callback", "/dashboard"]

    def test_a_non_redirect_observation_carries_no_target(self, tmp_path: Path) -> None:
        """A 200 has nowhere it sent the client, whatever headers it carries."""
        entries = [
            _entry(
                "POST",
                "https://api.myshop.example.com/login",
                post_data=json.dumps({"password": "x"}),
                response_headers={"Location": "/dashboard"},
            )
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        (observation,) = [o for o in result.login if o.kind == "credential_post"]
        assert observation.redirect_to == ""

    def test_redirect_outside_window_is_not_an_observation(self, tmp_path: Path) -> None:
        entries = [
            _entry(
                "POST",
                "https://api.myshop.example.com/login",
                post_data=json.dumps({"password": "x"}),
            ),
            *[
                _entry("GET", f"https://api.myshop.example.com/item/{i}")
                for i in range(_LOGIN_WINDOW + 1)
            ],
            _entry("GET", "https://api.myshop.example.com/elsewhere", status=302, body=""),
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert not any(o.kind == "redirect" for o in result.login)

    def test_set_cookie_within_window_is_an_observation(self, tmp_path: Path) -> None:
        entries = [
            _entry(
                "POST",
                "https://api.myshop.example.com/login",
                post_data=json.dumps({"password": "x"}),
            ),
            _entry(
                "GET",
                "https://api.myshop.example.com/dashboard",
                set_cookies=["session_id=abc123; Path=/; HttpOnly"],
            ),
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        (observation,) = [o for o in result.login if o.kind == "set_cookie"]
        assert observation.fields == ("session_id",)

    def test_auth_api_path_is_an_observation(self, tmp_path: Path) -> None:
        entries = [_entry("POST", "https://api.myshop.example.com/api/auth", post_data="{}")]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert any(o.kind == "auth_api" for o in result.login)

    def test_an_auth_word_inside_a_longer_segment_is_not_an_observation(
        self, tmp_path: Path
    ) -> None:
        """/token matched as a bare substring, so /api/tokens/list was labelled
        auth_api."""
        entries = [_entry("GET", "https://api.myshop.example.com/api/tokens/list")]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert result.login == ()

    def test_an_auth_word_ending_a_segment_is_an_observation(self, tmp_path: Path) -> None:
        entries = [_entry("POST", "https://api.myshop.example.com/oauth/token", post_data="{}")]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert any(o.kind == "auth_api" for o in result.login)

    def test_unrelated_asset_load_is_not_a_login_observation(self, tmp_path: Path) -> None:
        entries = [_entry("GET", "https://api.myshop.example.com/products")]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert result.login == ()


class TestTokenCandidatePairing:
    def test_header_and_meta_both_counted(self, tmp_path: Path) -> None:
        entries = [
            _entry(
                "GET",
                "https://api.myshop.example.com/dashboard",
                content_type="text/html",
                body='<meta name="csrf-token" content="abc">',
                request_headers={"X-CSRF-Token": "abc"},
            )
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        kinds = {c.kind for c in result.tokens}
        assert "header" in kinds
        assert "meta" in kinds


class TestRunLevelCookies:
    def test_multiple_set_cookie_headers_both_names_appear(self, tmp_path: Path) -> None:
        entries = [
            _entry(
                "GET",
                "https://api.myshop.example.com/dashboard",
                set_cookies=[
                    "session_id=abc123; Path=/; HttpOnly",
                    "csrf_token=xyz; Path=/",
                ],
            )
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert "session_id" in result.cookies
        assert "csrf_token" in result.cookies


class TestDigestSourceFromHar:
    def test_bare_har_has_no_run_metadata(self, tmp_path: Path) -> None:
        entries = [_entry("GET", "https://api.myshop.example.com/x")]
        source = DigestSource.from_har(_write_har(tmp_path, entries))
        assert source.session is None
        assert source.bodies_dir is None
        assert source.page_source is None

    def test_run_dir_picks_up_bodies_and_page_source(self, tmp_path: Path) -> None:
        run_dir = tmp_path / "myshop" / "20260910-100000-1"
        (run_dir / "bodies").mkdir(parents=True)
        (run_dir / "page-source.html").write_text("<html></html>")
        _write_har(run_dir, [_entry("GET", "https://api.myshop.example.com/x")])
        source = DigestSource.from_run_dir(run_dir, session="myshop", run_id=run_dir.name)
        assert source.bodies_dir == run_dir / "bodies"
        assert source.page_source == run_dir / "page-source.html"
        assert source.session == "myshop"
        assert source.run_id == run_dir.name


class TestEndpointExamples:
    def test_examples_stop_at_the_cap(self, tmp_path: Path) -> None:
        entries = [
            _entry("GET", f"https://api.myshop.example.com/orders/{i}")
            for i in range(_MAX_ENDPOINT_EXAMPLES + 4)
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        (endpoint,) = [e for e in result.endpoints if e.template == "/orders/{order_id}"]
        assert len(endpoint.examples) == _MAX_ENDPOINT_EXAMPLES


class TestEveryEndpointPresent:
    def test_low_and_high_count_endpoints_both_appear(self, tmp_path: Path) -> None:
        entries = [_entry("GET", "https://api.myshop.example.com/rare")]
        entries += [_entry("GET", "https://api.myshop.example.com/common") for _ in range(50)]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        templates = {e.template for e in result.endpoints}
        assert "/rare" in templates
        assert "/common" in templates


class TestHighCardinalityCollapse:
    def test_segment_with_many_distinct_literal_values_collapses(self, tmp_path: Path) -> None:
        entries = [
            _entry("GET", f"https://api.myshop.example.com/products/item-{i}")
            for i in range(_HIGH_CARDINALITY_THRESHOLD + 2)
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        templates = {e.template for e in result.endpoints}
        assert "/products/{product_id}" in templates

    def test_endpoint_template_answers_with_the_collapsed_template(self, tmp_path: Path) -> None:
        entries = [
            _entry("GET", f"https://api.myshop.example.com/products/item-{i}")
            for i in range(_HIGH_CARDINALITY_THRESHOLD + 2)
        ]
        entries.append(_entry("GET", "https://api.myshop.example.com/orders/7"))
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert endpoint_template(result, "/products/item-3") == "/products/{product_id}"
        assert endpoint_template(result, "/orders/7") == "/orders/{order_id}"
        assert endpoint_template(result, "/never/seen") == "/never/seen"

    def test_segment_at_or_below_threshold_stays_literal(self, tmp_path: Path) -> None:
        entries = [
            _entry("GET", f"https://api.myshop.example.com/products/item-{i}")
            for i in range(_HIGH_CARDINALITY_THRESHOLD)
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        templates = {e.template for e in result.endpoints}
        assert all(t.startswith("/products/item-") for t in templates)

    def test_single_segment_routes_never_collapse(self, tmp_path: Path) -> None:
        """A single segment has no other position to compare, so the family
        check would otherwise treat any run of distinct root routes as one
        high-cardinality family and collapse them all into `/{id}`."""
        entries = [
            _entry("GET", f"https://api.myshop.example.com/route-{i}")
            for i in range(_HIGH_CARDINALITY_THRESHOLD + 2)
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        templates = {e.template for e in result.endpoints}
        assert templates == {f"/route-{i}" for i in range(_HIGH_CARDINALITY_THRESHOLD + 2)}


class TestHighCardinalityEligibility:
    """A position collapses on count *and* on its values looking like identifiers."""

    def test_word_like_sibling_routes_stay_separate_endpoints(self, tmp_path: Path) -> None:
        names = [
            "orders",
            "products",
            "users",
            "carts",
            "invoices",
            "shipments",
            "returns",
            "coupons",
            "reviews",
        ]
        assert len(names) > _HIGH_CARDINALITY_THRESHOLD
        entries = [_entry("GET", f"https://api.myshop.example.com/api/{name}") for name in names]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        templates = {e.template for e in result.endpoints}
        assert templates == {f"/api/{name}" for name in names}

    def test_digit_bearing_slug_family_still_collapses(self, tmp_path: Path) -> None:
        count = _HIGH_CARDINALITY_THRESHOLD + 1
        entries = [
            _entry("GET", f"https://api.myshop.example.com/products/red-widget-{2000 + i}")
            for i in range(count)
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        templates = {e.template for e in result.endpoints}
        assert templates == {"/products/{product_id}"}

    def test_minority_of_eligible_values_does_not_collapse(self, tmp_path: Path) -> None:
        total = _HIGH_CARDINALITY_THRESHOLD + 3
        eligible = int(total * _DYNAMIC_MAJORITY)  # a minority of *total*, by definition
        slugs = [f"red-widget-{2000 + i}" for i in range(eligible)]
        slugs += [f"red-widget-{chr(ord('a') + i)}" for i in range(total - eligible)]
        entries = [
            _entry("GET", f"https://api.myshop.example.com/products/{slug}") for slug in slugs
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        templates = {e.template for e in result.endpoints}
        assert templates == {f"/products/{slug}" for slug in slugs}


class TestCollapseIsScopedToTheFamilyThatQualified:
    def test_siblings_of_the_same_depth_outside_the_family_stay_literal(
        self, tmp_path: Path
    ) -> None:
        """One slug family used to collapse every template of its segment count:
        /account/profile and /account/settings became /account/{account_id} and
        merged, and /api/health became /api/{api_id}."""
        slug_count = 80
        assert slug_count > _HIGH_CARDINALITY_THRESHOLD
        entries = [
            _entry("GET", f"https://api.myshop.example.com/products/red-widget-{2000 + i}")
            for i in range(slug_count)
        ]
        entries += [
            _entry("GET", "https://api.myshop.example.com/api/health"),
            _entry("GET", "https://api.myshop.example.com/account/profile"),
            _entry("GET", "https://api.myshop.example.com/account/settings"),
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        templates = {e.template for e in result.endpoints}
        assert templates == {
            "/products/{product_id}",
            "/api/health",
            "/account/profile",
            "/account/settings",
        }

    def test_an_ineligible_value_inside_the_family_stays_literal(self, tmp_path: Path) -> None:
        """The family qualifies on its values as a whole; a word-like member of
        it is still not an identifier and keeps its own template."""
        slug_count = _HIGH_CARDINALITY_THRESHOLD + 2
        entries = [
            _entry("GET", f"https://api.myshop.example.com/products/red-widget-{2000 + i}")
            for i in range(slug_count)
        ]
        entries.append(_entry("GET", "https://api.myshop.example.com/products/featured"))
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        templates = {e.template for e in result.endpoints}
        assert templates == {"/products/{product_id}", "/products/featured"}


class TestCollapsePreservesATrailingSlash:
    def test_a_family_recorded_with_a_trailing_slash_keeps_it(self, tmp_path: Path) -> None:
        """paths.template_path preserves a trailing slash exactly as given, so
        the collapse must too: dropping it renamed the route."""
        count = _HIGH_CARDINALITY_THRESHOLD + 2
        entries = [
            _entry("GET", f"https://api.myshop.example.com/products/red-widget-{2000 + i}/")
            for i in range(count)
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        templates = {e.template for e in result.endpoints}
        assert templates == {"/products/{product_id}/"}


class TestCollapseMergeCarriesShapeAndBodyKind:
    def test_a_later_members_shape_and_body_kind_survive_the_merge(self, tmp_path: Path) -> None:
        """The first member of a collapsed family answers for the family, and it may
        be the one that returned HTML and posted nothing."""
        count = _HIGH_CARDINALITY_THRESHOLD + 1
        entries = [
            _entry(
                "POST",
                "https://api.myshop.example.com/products/red-widget-2000",
                content_type="text/html",
                body="<html></html>",
            )
        ]
        entries += [
            _entry(
                "POST",
                f"https://api.myshop.example.com/products/red-widget-{2000 + i}",
                body='{"id": 1}',
                post_data='{"sku": "widget"}',
            )
            for i in range(1, count)
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        merged = [e for e in result.endpoints if e.template == "/products/{product_id}"]
        assert len(merged) == 1, [e.template for e in result.endpoints]
        assert merged[0].shape is not None
        assert merged[0].body_kind == "json"
        assert "sku" in merged[0].body_params

    def test_members_that_type_a_parameter_differently_merge_to_str(self, tmp_path: Path) -> None:
        """The family merge applies the same rule a single endpoint's requests do."""
        count = _HIGH_CARDINALITY_THRESHOLD + 1
        entries = [
            _entry(
                "GET",
                f"https://api.myshop.example.com/products/red-widget-{2000 + i}"
                f"?ref={'abc' if i == 0 else i}&page={i}",
            )
            for i in range(count)
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        (merged,) = [e for e in result.endpoints if e.template == "/products/{product_id}"]
        assert merged.query_params == {"ref": "str", "page": "int"}


class TestParseErrorsAndMissingBodies:
    def test_malformed_entry_counts_as_dropped_error(self, tmp_path: Path) -> None:
        good = _entry("GET", "https://api.myshop.example.com/orders")
        bad = {"request": None, "response": {}}
        har_path = _write_har(tmp_path, [good, bad])
        result = digest(DigestSource.from_har(har_path))
        assert result.dropped["error"] == 1

    def test_missing_body_file_is_dropped_not_raised(self, tmp_path: Path) -> None:
        entry = _entry("GET", "https://api.myshop.example.com/big")
        entry["response"]["content"] = {
            "mimeType": "application/json",
            "size": 0,
            "_bodyFile": "bodies/missing.json",
        }
        har_path = _write_har(tmp_path, [entry])
        result = digest(DigestSource.from_har(har_path))  # must not raise
        assert result.dropped["error"] == 1

    def test_the_missing_body_warning_logs_a_bare_url(self, tmp_path: Path) -> None:
        entry = _entry("GET", "https://api.myshop.example.com/big;s=PARAMVALUE?q=QUERYVALUE#FRAG")
        entry["response"]["content"] = {
            "mimeType": "application/json",
            "size": 0,
            "_bodyFile": "bodies/missing.json",
        }
        har_path = _write_har(tmp_path, [entry])
        with capture_logs() as events:
            digest(DigestSource.from_har(har_path))
        (event,) = [e for e in events if e["event"] == "digest_body_file_missing"]
        assert event["url"] == "https://api.myshop.example.com/big"

    def test_a_malformed_form_action_is_recorded_as_empty(self, tmp_path: Path) -> None:
        """urlsplit raises on an unclosed IPv6 bracket; the digest must still complete,
        and the warning names the page, never the action text."""
        page = _entry(
            "GET",
            "https://myshop.example.com/signin?next=QUERYVALUE",
            content_type="text/html",
            body=(
                '<form action="http://[bad/login"><input name="username">'
                '<input type="password" name="password"></form>'
            ),
        )
        with capture_logs() as events:
            result = digest(DigestSource.from_har(_write_har(tmp_path, [page])))
        (form,) = result.login_forms
        assert form.action == ""
        # Unscoped: an empty-action scope would never match the live form.
        assert form.fields["username"] == 'input[name="username"]'
        (event,) = [e for e in events if e["event"] == "login_form_action_unparseable"]
        assert event["source"] == "https://myshop.example.com/signin"
        assert "[bad" not in str(event)

    def test_a_malformed_request_url_is_dropped_as_an_error(self, tmp_path: Path) -> None:
        entries = [
            _entry("GET", "https://api.myshop.example.com/orders"),
            _entry("GET", "http://[bad/orders"),
        ]
        with capture_logs() as events:
            result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert result.dropped["error"] == 1
        assert [e.template for e in result.endpoints] == ["/orders"]
        assert any(e["event"] == "digest_url_unparseable" for e in events)

    def test_a_malformed_redirect_target_records_no_landing_path(self, tmp_path: Path) -> None:
        post = _entry(
            "POST",
            "https://myshop.example.com/session",
            status=302,
            content_type="text/html",
            body="",
            response_headers={"Location": "http://[bad/dashboard"},
            post_data="username=alice&password=x",
        )
        post["request"]["postData"]["mimeType"] = _FORM_CONTENT_TYPE
        result = digest(DigestSource.from_har(_write_har(tmp_path, [post])))
        (observation,) = [o for o in result.login if o.kind == "credential_post"]
        assert observation.redirect_to == ""


def _flow_endpoint(template: str, method: str = "GET", examples: tuple[str, ...] = ()) -> Endpoint:
    return Endpoint(
        host="api.myshop.example.com",
        template=template,
        methods=(method,),
        count=1,
        statuses=(200,),
        content_type="text/html",
        query_params={},
        body_params={},
        body_kind="none",
        shape=None,
        custom_headers=(),
        examples=examples,
    )


def _flow_observation(kind: ObservationKind, method: str, path: str) -> LoginObservation:
    return LoginObservation(
        order=1,
        method=method,
        url=f"https://api.myshop.example.com{path}",
        status=200,
        kind=kind,
        fields=(),
    )


class TestLoginFlowFlag:
    """login_flow marks the endpoints login_config drives: the login form's GET and
    the credential POST. It moved here from the generator so the proposal table and
    the scaffold read one flag (graft skill spec, 2026-09-21)."""

    def test_the_form_page_and_the_credential_post_are_flagged(self, tmp_path: Path) -> None:
        entries = [
            _entry(
                "GET",
                "https://api.myshop.example.com/login",
                content_type="text/html",
                body=(
                    '<form action="/login" method="post">'
                    '<input type="email" name="email"><input type="password" name="password">'
                    "</form>"
                ),
            ),
            _entry(
                "POST",
                "https://api.myshop.example.com/login",
                post_data=json.dumps({"email": "alice@example.com", "password": "x"}),
            ),
            _entry("GET", "https://api.myshop.example.com/api/orders", body='{"orders": []}'),
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        flags = {(e.methods[0], e.template): e.login_flow for e in result.endpoints}
        assert flags == {
            ("GET", "/login"): True,
            ("POST", "/login"): True,
            ("GET", "/api/orders"): False,
        }

    def test_the_same_path_under_another_method_is_not_flagged(self) -> None:
        (endpoint,) = _with_login_flow(
            (_flow_endpoint("/login", "DELETE"),),
            (_flow_observation("form_page", "GET", "/login"),),
        )
        assert endpoint.login_flow is False

    def test_a_login_path_inside_a_collapsed_family_is_flagged(self) -> None:
        """The high-cardinality collapse can re-template the endpoint the observation
        belongs to, so templating the observation's path alone would miss it."""
        family = _flow_endpoint("/account/{account_id}", examples=("/account/login",))
        (endpoint,) = _with_login_flow(
            (family,), (_flow_observation("form_page", "GET", "/account/login"),)
        )
        assert endpoint.login_flow is True

    def test_an_endpoint_the_login_flow_owns_under_only_one_of_its_methods_is_not_flagged(
        self,
    ) -> None:
        """digest() gives each endpoint one method; a hand-built endpoint with two is
        flagged only when the flow owns both."""
        two_methods = dataclasses.replace(_flow_endpoint("/login"), methods=("GET", "DELETE"))
        (endpoint,) = _with_login_flow(
            (two_methods,), (_flow_observation("form_page", "GET", "/login"),)
        )
        assert endpoint.login_flow is False

    def test_an_endpoint_with_no_methods_is_not_flagged(self) -> None:
        no_methods = dataclasses.replace(_flow_endpoint("/login"), methods=())
        (endpoint,) = _with_login_flow(
            (no_methods,), (_flow_observation("form_page", "GET", "/login"),)
        )
        assert endpoint.login_flow is False

    def test_a_redirect_or_auth_api_observation_flags_nothing(self) -> None:
        endpoints = (_flow_endpoint("/auth/callback"), _flow_endpoint("/api/session"))
        observations = (
            _flow_observation("redirect", "GET", "/auth/callback"),
            _flow_observation("auth_api", "GET", "/api/session"),
        )
        assert [e.login_flow for e in _with_login_flow(endpoints, observations)] == [False, False]

    def test_the_flag_defaults_to_false(self) -> None:
        assert _flow_endpoint("/anything").login_flow is False


class TestFlaggedNamesOf:
    def test_flagged_names_are_exactly_the_digest_cookie_and_token_names(
        self, tmp_path: Path
    ) -> None:
        entry = {
            "startedDateTime": "2026-09-10T10:00:00.000Z",
            "time": 1,
            "request": {
                "method": "GET",
                "url": "https://myshop.example.com/account",
                "headers": [],
                "cookies": [],
                "queryString": [],
            },
            "response": {
                "status": 200,
                "statusText": "OK",
                "headers": [{"name": "Content-Type", "value": "text/html"}],
                "cookies": [{"name": "sid", "value": "planted"}],
                "content": {
                    "mimeType": "text/html",
                    "text": '<meta name="csrf-token" content="planted">',
                    "size": 42,
                },
            },
        }
        har = tmp_path / "network.har"
        har.write_text(json.dumps({"log": {"version": "1.2", "entries": [entry]}}))
        result = digest(DigestSource.from_har(har))
        assert flagged_names_of(result) == tuple(
            sorted(set(result.cookies) | {t.name for t in result.tokens})
        )
        assert flagged_names_of(result) == ("csrf-token", "sid")

    def test_entries_add_cookies_the_digests_own_list_leaves_out(self, tmp_path: Path) -> None:
        """A static response and an out-of-scope host are outside d.cookies, which
        stays scoped; passing the entries widens only the flagged list."""
        entries = [
            _entry("GET", "https://api.myshop.example.com/orders"),
            _entry("GET", "https://api.myshop.example.com/orders/1"),
            _entry(
                "GET",
                "https://api.myshop.example.com/assets/app.js",
                content_type="application/javascript",
                body="var a = 1;",
                set_cookies=["asset_cookie=xyz; Path=/"],
            ),
            _entry(
                "GET",
                "https://tracker.example.net/pixel",
                set_cookies=["other_host_cookie=xyz; Path=/"],
            ),
        ]
        har = _write_har(tmp_path, entries)
        result = digest(DigestSource.from_har(har))
        assert result.cookies == ()
        assert flagged_names_of(result) == ()
        assert flagged_names_of(result, parse_har_file(har).entries) == (
            "asset_cookie",
            "other_host_cookie",
        )

    def test_a_cookie_set_by_a_second_in_scope_host_is_flagged_too(self, tmp_path: Path) -> None:
        """flagged_names must not miss a cookie a first-party subdomain sets: an
        auth or widget host distinct from the primary one."""
        entries = [
            _entry("GET", "https://api.myshop.example.com/orders"),
            _entry("GET", "https://api.myshop.example.com/orders/1"),
            _entry(
                "GET",
                "https://www.myshop.example.com/widget",
                content_type="text/plain",
                body="ok",
                set_cookies=["shop_session=xyz; Path=/; HttpOnly"],
            ),
        ]
        har = _write_har(tmp_path, entries)
        result = digest(DigestSource.from_har(har))
        assert result.primary_host == "api.myshop.example.com"
        assert "www.myshop.example.com" in result.hosts
        assert "shop_session" in result.cookies
        assert "shop_session" in flagged_names_of(result)


class TestEveryNamePositionGoesThroughTheIdRule:
    """S2 and S3: response keys, header names, and cookie names go through
    holds_an_id like path segments and parameter keys, and a dropped key is
    counted, never lost silently."""

    def test_an_id_response_key_becomes_one_placeholder_key(self, tmp_path: Path) -> None:
        body = json.dumps({"40912873": {"total": 1}, "ab12cd34ef": {"total": 2}, "name": "x"})
        entries = [_entry("GET", "https://api.myshop.example.com/balances", body=body)]
        (endpoint,) = digest(DigestSource.from_har(_write_har(tmp_path, entries))).endpoints
        assert endpoint.shape is not None
        assert set(endpoint.shape.children) == {"{key}", "name"}

    def test_an_id_header_name_is_dropped_and_counted(self, tmp_path: Path) -> None:
        entries = [
            _entry(
                "GET",
                "https://api.myshop.example.com/orders",
                request_headers={"X-Acct-40912873": "1", "X-Shop-Client": "web"},
            )
        ]
        (endpoint,) = digest(DigestSource.from_har(_write_har(tmp_path, entries))).endpoints
        assert endpoint.custom_headers == ("X-Shop-Client",)
        assert endpoint.dropped_id_header_names == 1

    def test_dropped_query_and_body_keys_are_counted(self, tmp_path: Path) -> None:
        entries = [
            _entry(
                "POST",
                "https://api.myshop.example.com/orders?u_40912873=1&cus_NffrFeUfNV2Hib=2&page=1",
                post_data=json.dumps({"acct_40912873": 1, "note": "x"}),
            ),
            _entry(
                "POST",
                "https://api.myshop.example.com/orders?u_40912873=3",
                post_data=json.dumps({"usr-Zq9XkLmPwR": 1}),
            ),
        ]
        (endpoint,) = digest(DigestSource.from_har(_write_har(tmp_path, entries))).endpoints
        assert endpoint.query_params == {"page": "int"}
        assert endpoint.body_params == {"note": "str"}
        assert (endpoint.dropped_id_query_keys, endpoint.dropped_id_body_keys) == (2, 2)

    def test_an_id_cookie_name_is_kept_as_its_hash(self, tmp_path: Path) -> None:
        import hashlib

        entries = [
            _entry(
                "GET",
                "https://api.myshop.example.com/orders",
                set_cookies=["sess_40912873=v; Path=/", "shop_session=v; Path=/"],
            )
        ]
        har = _write_har(tmp_path, entries)
        result = digest(DigestSource.from_har(har))
        hashed = "sha256:" + hashlib.sha256(b"sess_40912873").hexdigest()
        assert set(result.cookies) == {hashed, "shop_session"}
        flagged = flagged_names_of(result, parse_har_file(har).entries)
        assert hashed in flagged
        assert "sess_40912873" not in flagged

    def test_an_id_token_candidate_name_is_kept_as_its_hash(self, tmp_path: Path) -> None:
        entries = [
            _entry(
                "GET",
                "https://api.myshop.example.com/orders",
                request_headers={"X-Csrf-Token-40912873": "t"},
            )
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        names = {token.name for token in result.tokens}
        assert names and all(name.startswith("sha256:") for name in names)
