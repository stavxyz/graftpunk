"""A synthetic GraphQL recording on myshop.example.com for #367's tests.

Three named operations and an anonymous one, a batch, a persisted query, a
GraphQL login, and a JSON POST to the same path that is not GraphQL. Every value
is invented.
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.unit.test_har_digest import _entry, _write_har

HOST = "https://myshop.example.com"
GRAPHQL_URL = f"{HOST}/graphql"
PERSISTED_HASH = "a" * 64
ORDERS_DOCUMENT = (
    "fragment OrderFields on Order {\n"
    "  id\n"
    "  total\n"
    "}\n"
    "\n"
    "query OrdersByCustomer($customerId: ID!, $first: Int = 10, $status: String) {\n"
    "  orders(customer: $customerId, first: $first, status: $status) {\n"
    "    ...OrderFields\n"
    "  }\n"
    "}\n"
)
ADD_TO_CART_DOCUMENT = (
    "mutation AddToCart($sku: String!, $quantity: Int!) {\n"
    "  addToCart(sku: $sku, quantity: $quantity) { ok }\n"
    "}"
)
LOGIN_DOCUMENT = (
    "mutation LoginMutation($email: String!, $password: String!) "
    "{ login(email: $email, password: $password) { id } }"
)
CART_DOCUMENT = "query Cart { cart { items { sku } } }"
ANONYMOUS_DOCUMENT = "{ viewer { name } }"
CLIENT_LIBRARY = {"clientLibrary": {"name": "web", "version": "3.8.1"}}
SESSION_VALUE = "s3ss10nvalue77"
SESSION_COOKIE = f"myshop_session={SESSION_VALUE}"
EMAIL = "jane@myshop.example"
PASSWORD = "hunter22-pw"  # noqa: S105 - an invented value the recording posts


def _graphql(body: object, response: object) -> dict:
    return _entry(
        "POST",
        GRAPHQL_URL,
        body=json.dumps(response),
        post_data=json.dumps(body),
        request_headers={
            "Content-Type": "application/json",
            "x-app-name": "web",
            "Cookie": SESSION_COOKIE,
        },
    )


def orders_body(customer_id: str = "4021", status: str = "OPEN", contact: str = "") -> dict:
    """An OrdersByCustomer request; *contact* adds a variable holding that value."""
    variables: dict[str, object] = {"customerId": customer_id, "first": 10, "status": status}
    if contact:
        variables["contact"] = contact
    return {
        "operationName": "OrdersByCustomer",
        "variables": variables,
        "extensions": CLIENT_LIBRARY,
        "query": ORDERS_DOCUMENT,
    }


ORDERS_RESPONSE = {"data": {"orders": [{"id": "o1", "total": 12}]}}
CART_RESPONSE = {"data": {"cart": {"items": [{"sku": "red-mug"}]}}}


def entries() -> list[dict]:
    """The recording, in order."""
    return [
        _entry(
            "GET",
            f"{HOST}/",
            content_type="text/html",
            body="<html><body><div id='app'></div></body></html>",
            set_cookies=[f"{SESSION_COOKIE}; Path=/; HttpOnly"],
        ),
        _graphql(
            {
                "operationName": "LoginMutation",
                "variables": {"email": EMAIL, "password": PASSWORD},
                "extensions": CLIENT_LIBRARY,
                "query": LOGIN_DOCUMENT,
            },
            {"data": {"login": {"id": "u1", "customer": {"customerId": "4021"}}}},
        ),
        _graphql(orders_body(contact=EMAIL), ORDERS_RESPONSE),
        _graphql(orders_body(customer_id="4022", status="CLOSED"), ORDERS_RESPONSE),
        _graphql(
            {
                "operationName": "AddToCart",
                "variables": {"sku": "red-mug", "quantity": 2},
                "extensions": CLIENT_LIBRARY,
                "query": ADD_TO_CART_DOCUMENT,
            },
            {"data": {"addToCart": {"ok": True}}},
        ),
        _graphql({"query": ANONYMOUS_DOCUMENT}, {"data": {"viewer": {"name": "Jane"}}}),
        _graphql(
            [
                orders_body(customer_id="4023"),
                {"operationName": "Cart", "variables": {}, "query": CART_DOCUMENT},
            ],
            [ORDERS_RESPONSE, CART_RESPONSE],
        ),
        _graphql(
            {
                "operationName": "ProductPage",
                "variables": {"slug": "red-mug", "session": SESSION_VALUE},
                "extensions": {"persistedQuery": {"version": 1, "sha256Hash": PERSISTED_HASH}},
            },
            {"data": {"product": {"name": "Mug"}}},
        ),
        _entry(
            "POST",
            GRAPHQL_URL,
            post_data=json.dumps({"query": "red shoes", "page": 1}),
            body=json.dumps({"results": [], "page": 1}),
        ),
    ]


def write(run_dir: Path) -> Path:
    """The recording as ``run_dir/network.har``."""
    run_dir.mkdir(parents=True, exist_ok=True)
    return _write_har(run_dir, entries())
