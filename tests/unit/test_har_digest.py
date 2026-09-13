"""RunDigest over synthetic HARs built in tmp_path (plugin tooling spec, 2026-09-11)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from graftpunk.har.digest import (
    _BODY_SAMPLE_THRESHOLD,
    _DYNAMIC_MAJORITY,
    _FORM_CONTENT_TYPE,
    _HIGH_CARDINALITY_THRESHOLD,
    _LOGIN_WINDOW,
    _MAX_FIELD_NAME_LEN,
    _SHAPE_MAX_DEPTH,
    _SHAPE_MAX_KEYS,
    SHAPE_UNAVAILABLE,
    DigestSource,
    _parse_body,
    body_params,
    digest,
)
from graftpunk.har.parser import parse_har_file


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

    def test_a_multi_label_public_suffix_does_not_widen_the_scope(self, tmp_path: Path) -> None:
        """The last two labels of shop.example.co.uk are co.uk, which made every
        *.co.uk host a first party. The scope root is the primary host's parent
        domain instead."""
        entries = [
            _entry("GET", "https://shop.example.co.uk/orders"),
            _entry("GET", "https://shop.example.co.uk/orders/2"),
            _entry("GET", "https://api.example.co.uk/data"),
            _entry("GET", "https://other.co.uk/data"),
        ]
        result = digest(DigestSource.from_har(_write_har(tmp_path, entries)))
        assert result.primary_host == "shop.example.co.uk"
        hosts = {e.host for e in result.endpoints}
        assert hosts == {"shop.example.co.uk", "api.example.co.uk"}
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
        name = "f" * _MAX_FIELD_NAME_LEN
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
