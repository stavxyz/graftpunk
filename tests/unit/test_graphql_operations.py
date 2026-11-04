"""GraphQL operations as endpoint rows (#367).

A synthetic recording on myshop.example.com (tests/unit/graphql_har.py) holds three
named operations and an anonymous one, a batch, a persisted query, a GraphQL login,
and a JSON POST to the same path that is not GraphQL. The digest files each
operation as a row of its own; ``--command``, ``--match``, the sidecar,
``FixtureSession`` and the graft skill's ``har-facts.py`` take the same key.
"""

from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from graftpunk.cli.main import app as main_app
from graftpunk.cli.observe_commands import observe_app
from graftpunk.cli.scaffold_commands import plugin_app
from graftpunk.har.digest import (
    DigestSource,
    Endpoint,
    RunDigest,
    digest,
    endpoint_key,
)
from graftpunk.har.graphql import (
    ANONYMOUS_OPERATION,
    document_literals_to_fill,
    document_operation,
    filled_document,
    operations_in,
    request_operation,
)
from graftpunk.har.naming import (
    EndpointSpecError,
    capture_filename,
    capture_slug,
    parse_command_key,
    parse_command_spec,
    parse_endpoint,
    parse_endpoint_key,
)
from graftpunk.har.report import endpoints_projection, render_json, render_markdown
from graftpunk.testing import FixtureNotFoundError, FixtureSession
from graftpunk.testing.plugin import check_fixtures_tree
from graftpunk.testing.sidecar import Sidecar, SidecarError, load_sidecar, sidecar_text
from tests.unit import graphql_har
from tests.unit.cli_harness import strip_ansi
from tests.unit.test_har_digest import _entry, _write_har

_ORDERS = "POST /graphql (OrdersByCustomer)"


@pytest.fixture(autouse=True)
def _no_session_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GRAFTPUNK_SESSION", raising=False)


def _digest(tmp_path: Path, entries: list[dict] | None = None) -> RunDigest:
    run = tmp_path / "run"
    run.mkdir(parents=True, exist_ok=True)
    har = _write_har(run, graphql_har.entries() if entries is None else entries)
    return digest(DigestSource.from_har(har))


def digest_of(entries: list[dict]) -> RunDigest:
    with tempfile.TemporaryDirectory() as scratch:
        return digest(DigestSource.from_har(_write_har(Path(scratch), entries)))


def _row(d: RunDigest, key: str) -> Endpoint:
    (found,) = [e for e in d.endpoints if e.key == key]
    return found


class TestTheReader:
    def test_the_first_operation_after_fragments_names_the_document(self) -> None:
        assert document_operation(graphql_har.ORDERS_DOCUMENT) == ("OrdersByCustomer", "query")
        assert document_operation(graphql_har.ADD_TO_CART_DOCUMENT) == ("AddToCart", "mutation")

    @pytest.mark.parametrize(
        "document",
        ["{ viewer { name } }", "query { a }", "fragment F on T { a }\n{ b { ...F } }"],
    )
    def test_an_operation_without_a_name_is_anonymous(self, document: str) -> None:
        assert document_operation(document) == (ANONYMOUS_OPERATION, "query")

    @pytest.mark.parametrize(
        "text", ["red shoes", "query shoes", "{ unbalanced", "}{", "", "SELECT 1;", "{ a } }"]
    )
    def test_text_that_is_not_a_document_is_not_graphql(self, text: str) -> None:
        assert document_operation(text) is None
        assert operations_in({"query": text}) == ()

    def test_a_name_reading_as_an_id_is_filed_as_anonymous(self) -> None:
        (operation,) = operations_in({"operationName": "Q40912873", "query": "query Q { a }"})
        assert operation.name == ANONYMOUS_OPERATION

    def test_operation_name_wins_over_the_document(self) -> None:
        body = {"operationName": "Second", "query": "query First { a } query Second { b }"}
        assert request_operation(body).name == "Second"  # type: ignore[union-attr]

    def test_a_batch_is_each_element_and_a_mixed_array_is_nothing(self) -> None:
        batch = [{"query": "query A { a }"}, {"operationName": "B", "query": "query B { b }"}]
        assert [(o.name, o.batched) for o in operations_in(batch)] == [("A", True), ("B", True)]
        assert operations_in([*batch, {"page": 1}]) == ()
        assert request_operation(batch) is None

    def test_a_persisted_query_carries_its_hash(self) -> None:
        body = {
            "operationName": "ProductPage",
            "extensions": {"persistedQuery": {"version": 1, "sha256Hash": "ab" * 32}},
        }
        operation = request_operation(body)
        assert operation is not None
        assert (operation.name, operation.document) == ("ProductPage", None)
        assert (operation.persisted_hash, operation.persisted_version) == ("ab" * 32, 1)

    def test_a_document_without_inline_values_is_kept_byte_for_byte(self) -> None:
        document = "# a comment\nquery Q($id: ID!) {\n  node(id: $id, first: 10) { id }\n}\n"
        assert document_literals_to_fill(document) == 0
        assert filled_document(document) == document

    def test_inline_strings_and_id_shaped_numbers_are_filled(self) -> None:
        document = 'query Q { a(id: 40912873, email: "jane@myshop.example", n: 5) { b } }'
        assert document_literals_to_fill(document) == 2
        assert filled_document(document) == (
            'query Q { a(id: "GP-FILL", email: "GP-FILL", n: 5) { b } }'
        )


class TestTheDigest:
    def test_each_operation_is_a_row_and_the_plain_post_stays(self, tmp_path: Path) -> None:
        d = _digest(tmp_path)
        assert sorted(e.key for e in d.endpoints) == [
            "GET /",
            "POST /graphql",
            "POST /graphql (AddToCart)",
            "POST /graphql (Cart)",
            "POST /graphql (LoginMutation)",
            "POST /graphql (OrdersByCustomer)",
            "POST /graphql (ProductPage)",
            "POST /graphql (anonymous)",
        ]
        plain = _row(d, "POST /graphql")
        assert plain.operation == ""
        assert plain.body_params == {"page": "int", "query": "str"}
        assert plain.count == 1

    def test_a_row_holds_its_own_variables_count_and_shape(self, tmp_path: Path) -> None:
        orders = _row(_digest(tmp_path), _ORDERS)
        assert orders.count == 3
        assert orders.operation_batched == 1
        assert orders.body_params == {
            "contact": "str",
            "customerId": "str",
            "first": "int",
            "status": "str",
        }
        assert orders.shape is not None and set(orders.shape.children) == {"data"}
        assert orders.graphql_document == graphql_har.ORDERS_DOCUMENT

    def test_a_batch_is_never_the_fixture_recording(self, tmp_path: Path) -> None:
        d = _digest(tmp_path)
        cart = _row(d, "POST /graphql (Cart)")
        assert cart.operation_batched == cart.count == 1
        assert not cart.fixture_written
        # The batch is entry 6; OrdersByCustomer's fixture is its first single post.
        assert _row(d, _ORDERS).fixture_entry_index == 2

    def test_the_login_owns_only_its_own_operation(self, tmp_path: Path) -> None:
        d = _digest(tmp_path)
        assert [e.key for e in d.endpoints if e.login_flow] == ["POST /graphql (LoginMutation)"]

    def test_defaults_leave_out_account_data(self, tmp_path: Path) -> None:
        d = _digest(tmp_path)
        orders = _row(d, _ORDERS)
        # Only form state becomes a default: the page size and the enum value. The
        # id "4021" and the login's email are held back by name, never kept.
        assert orders.graphql_defaults == {"first": 10, "status": "OPEN"}
        assert orders.graphql_held_variables == ("customerId", "contact")
        cart = _row(d, "POST /graphql (AddToCart)")
        assert cart.graphql_defaults == {"quantity": 2}
        assert cart.graphql_held_variables == ("sku",)
        product = _row(d, "POST /graphql (ProductPage)")
        assert product.graphql_defaults == {}
        assert product.graphql_held_variables == ("slug", "session")

    @pytest.mark.parametrize(
        ("name", "value", "kept"),
        [
            ("first", 10, True),
            ("status", "OPEN", True),
            ("sortBy", "DESC", True),
            # form_state does not read an underscore-joined constant as words.
            ("sortBy", "CREATED_AT", False),
            ("includeArchived", False, True),
            ("customerId", "4021", False),
            ("customerId", 4021, False),
            ("orderId", 7, False),
            ("page", 4021, False),
            ("status", "Open", False),
            ("name", "Jane", False),
            ("slug", "red-mug", False),
            ("state", "CLOSED", True),
            ("contact", graphql_har.EMAIL, False),
        ],
    )
    def test_a_default_is_form_state_alone(self, name: str, value: object, kept: bool) -> None:
        entries = [
            _entry(
                "POST",
                graphql_har.GRAPHQL_URL,
                post_data=json.dumps({"variables": {name: value}, "query": "query Q { a }"}),
                body='{"data": {"a": 1}}',
            )
        ]
        row = _row(digest_of(entries), "POST /graphql (Q)")
        assert (name in row.graphql_defaults) is kept
        assert (name in row.graphql_held_variables) is not kept

    def test_extensions_that_are_not_form_state_are_held_by_name(self, tmp_path: Path) -> None:
        d = _digest(tmp_path)
        # clientLibrary's name and version are recorded values, not form state.
        assert _row(d, _ORDERS).graphql_extensions == {}
        assert _row(d, _ORDERS).graphql_held_extensions == ("clientLibrary",)
        product = _row(d, "POST /graphql (ProductPage)")
        assert product.graphql_document is None
        assert product.graphql_persisted_hash == graphql_har.PERSISTED_HASH
        assert product.graphql_extensions == {}

    def test_shared_values_pair_a_variable_with_the_field_that_produced_it(
        self, tmp_path: Path
    ) -> None:
        d = _digest(tmp_path)
        assert [
            (s.source_operation, s.source_path, s.target_operation, s.target_variable, s.count)
            for s in d.shared_values
        ] == [
            ("Cart", "data.cart.items[].sku", "ProductPage", "slug", 1),
            (
                "LoginMutation",
                "data.login.customer.customerId",
                "OrdersByCustomer",
                "customerId",
                1,
            ),
        ]
        text = render_markdown(d)
        section = text.split("## Shared values\n", 1)[1].split("\n## ", 1)[0]
        assert (
            "- LoginMutation `data.login.customer.customerId` -> OrdersByCustomer "
            "`variables.customerId` (1 request(s))"
        ) in section
        for value in ("4021", "red-mug", graphql_har.EMAIL):
            assert value not in section
        shared = json.loads(render_json(d))["shared_values"]
        assert shared[1]["source_path"] == "data.login.customer.customerId"
        assert "4021" not in json.dumps(shared)

    def test_a_value_an_earlier_response_did_not_hold_links_nothing(self, tmp_path: Path) -> None:
        later = graphql_har.entries()
        # The orders request moved ahead of the login that returns its id.
        later.insert(1, later.pop(2))
        d = _digest(tmp_path, later)
        assert not any(s.target_variable == "customerId" for s in d.shared_values)
        entries = [_entry("POST", "https://myshop.example.com/api/x", post_data='{"a": "4021"}')]
        assert digest_of(entries).shared_values == ()
        assert "## Shared values" not in render_markdown(digest_of(entries))

    def test_the_markdown_prints_one_block_per_operation(self, tmp_path: Path) -> None:
        text = render_markdown(_digest(tmp_path))
        block = text.split(f"### {_ORDERS}\n", 1)[1].split("\n### ", 1)[0]
        assert "- GraphQL operation: query" in block
        assert "- variables: contact: str, customerId: str, first: int, status: str" in block
        assert "- batched: 1 of 3 recording(s) sent it in a batch" in block
        assert "### POST /graphql (anonymous)\n" in text
        assert "- GraphQL operation: persisted query (no document recorded)" in text
        cart = text.split("### POST /graphql (Cart)\n", 1)[1].split("\n### ", 1)[0]
        assert "- shape: unknown (every recording was in a batch)" in cart
        # No value of the recording reaches the digest.
        for value in ("4021", graphql_har.EMAIL, graphql_har.SESSION_VALUE, "red-mug"):
            assert value not in text

    def test_json_carries_the_operation_and_leaves_out_the_generator_lookups(
        self, tmp_path: Path
    ) -> None:
        rendered = json.loads(render_json(_digest(tmp_path)))
        rows = {(e["template"], e.get("operation")): e for e in rendered["endpoints"]}
        orders = rows[("/graphql", "OrdersByCustomer")]
        assert orders["operation_batched"] == 1
        assert not any(key.startswith("graphql_") for key in orders)
        assert "operation" not in rows[("/graphql", None)]
        assert "4021" not in json.dumps(rendered)

    def test_the_projection_adds_operation_within_schema_1(self, tmp_path: Path) -> None:
        projected = endpoints_projection(_digest(tmp_path))
        assert projected["schema"] == 1
        rows = projected["endpoints"]
        assert sum(1 for row in rows if row["template"] == "/graphql") == 7
        orders = next(row for row in rows if row.get("operation") == "OrdersByCustomer")
        assert orders["body_params"] == {
            "contact": "str",
            "customerId": "str",
            "first": "int",
            "status": "str",
        }
        plain = next(
            row for row in rows if row["template"] == "/graphql" and "operation" not in row
        )
        assert plain["body_params"] == {"page": "int", "query": "str"}

    def test_a_run_without_graphql_has_no_operation_anywhere(self, tmp_path: Path) -> None:
        entries = [_entry("POST", "https://myshop.example.com/api/search", post_data='{"q": 1}')]
        d = _digest(tmp_path, entries)
        assert [e.operation for e in d.endpoints] == [""]
        assert "operation" not in render_json(d)
        assert "operation" not in json.dumps(endpoints_projection(d))


class TestTheKey:
    def test_the_key_reads_back(self) -> None:
        assert parse_endpoint_key(_ORDERS) == ("POST", "/graphql", "OrdersByCustomer")
        assert parse_endpoint_key("POST /graphql (anonymous)") == (
            "POST",
            "/graphql",
            ANONYMOUS_OPERATION,
        )
        assert parse_endpoint_key("POST /graphql (*)") == ("POST", "/graphql", "*")
        assert parse_endpoint_key("GET /api/orders") == ("GET", "/api/orders", "")
        assert parse_command_key(f"orders={_ORDERS}") == (
            "orders",
            "POST",
            "/graphql",
            "OrdersByCustomer",
        )
        assert endpoint_key("POST", "/graphql", ANONYMOUS_OPERATION) == (
            "POST /graphql (anonymous)"
        )

    def test_the_pair_readers_still_refuse_a_suffix(self) -> None:
        with pytest.raises(EndpointSpecError):
            parse_endpoint(_ORDERS)
        with pytest.raises(EndpointSpecError):
            parse_command_spec(f"orders={_ORDERS}")

    def test_a_fixture_is_named_by_its_operation(self) -> None:
        assert capture_slug("POST", "/graphql", "OrdersByCustomer") == (
            "post_graphql(OrdersByCustomer)"
        )
        assert capture_slug("POST", "/graphql", ANONYMOUS_OPERATION) == "post_graphql(anonymous)"
        assert capture_filename("POST", "/graphql", "application/json", "Cart") == (
            "post_graphql(Cart).json"
        )
        assert capture_filename("POST", "/graphql", "application/json") == "post_graphql.json"


def _app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> typer.Typer:
    observe_base = tmp_path / "observe"
    monkeypatch.setattr("graftpunk.cli.observe_commands.OBSERVE_BASE_DIR", observe_base)
    graphql_har.write(observe_base / "myshop" / "run-1")
    app = typer.Typer()
    app.add_typer(plugin_app)
    app.add_typer(observe_app)
    return app


def _fixtures(app: typer.Typer, out: Path, *matches: str) -> str:
    argv = ["observe", "fixtures", "myshop", "--out", str(out)]
    for match in matches:
        argv += ["--match", match]
    result = CliRunner().invoke(app, argv)
    assert result.exit_code == 0, result.output
    return " ".join(strip_ansi(result.output).split())


def _written(out: Path) -> list[str]:
    return sorted(p.name for p in out.iterdir() if not p.name.endswith(".meta.json"))


class TestFixtures:
    def test_match_by_operation_writes_that_operation_s_fixtures(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app = _app(tmp_path, monkeypatch)
        out = tmp_path / "captures"
        output = _fixtures(app, out, _ORDERS)
        assert _written(out) == [
            "post_graphql(OrdersByCustomer)#1.json",
            "post_graphql(OrdersByCustomer).json",
        ]
        assert "Skipped a batch of OrdersByCustomer, Cart on POST /graphql" in output
        sidecar = load_sidecar(out / "post_graphql(OrdersByCustomer).json.meta.json")
        assert sidecar.graphql_operation == "OrdersByCustomer"
        raw = json.loads((out / "post_graphql(OrdersByCustomer).json.meta.json").read_text())
        assert raw["schema"] == 5

    def test_a_glob_writes_one_name_per_operation_and_the_plain_row_apart(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app = _app(tmp_path, monkeypatch)
        out = tmp_path / "captures"
        _fixtures(app, out, "POST /graphql (*)", "POST /graphql")
        assert _written(out) == [
            "post_graphql(AddToCart).json",
            "post_graphql(LoginMutation).json",
            "post_graphql(OrdersByCustomer)#1.json",
            "post_graphql(OrdersByCustomer).json",
            "post_graphql(ProductPage).json",
            "post_graphql(anonymous).json",
            "post_graphql.json",
        ]
        plain = load_sidecar(out / "post_graphql.json.meta.json")
        assert plain.graphql_operation is None
        anonymous = load_sidecar(out / "post_graphql(anonymous).json.meta.json")
        assert anonymous.graphql_operation == ANONYMOUS_OPERATION
        assert json.loads((out / "post_graphql.json").read_text()) == {"results": [], "page": 1}

    def test_the_plain_key_on_a_pure_graphql_path_says_to_name_an_operation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app = _app(tmp_path, monkeypatch)
        output = _fixtures(app, tmp_path / "captures", "POST /graphql (Nope)", "POST /graph*")
        assert "No entries matched --match POST /graphql (Nope)." in output
        assert "No entries matched --match POST /graph*" not in output

    def test_the_fixtures_check_reads_operation_fixtures_like_any_other(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app = _app(tmp_path, monkeypatch)
        tree = tmp_path / "fixtures"
        _fixtures(app, tree, "POST /graphql (*)")
        for meta in tree.glob("*.meta.json"):
            sidecar = dataclasses.replace(load_sidecar(meta), capture_sha256=None)
            meta.write_text(sidecar_text(sidecar))
        report = check_fixtures_tree(tree, observe_dir=tmp_path / "nowhere")
        assert report.problems == ()
        assert report.declared == len(_written(tree)) == 6


class TestTheSidecar:
    def test_schema_5_round_trips_the_operation(self, tmp_path: Path) -> None:
        sidecar = Sidecar(status=200, content_type="application/json", graphql_operation="Cart")
        path = tmp_path / "x.json.meta.json"
        path.write_text(sidecar_text(sidecar))
        assert load_sidecar(path).graphql_operation == "Cart"

    def test_schema_4_holds_no_operation(self, tmp_path: Path) -> None:
        payload = json.loads(sidecar_text(Sidecar(status=200, content_type="text/html")))
        payload["schema"] = 4
        path = tmp_path / "x.html.meta.json"
        path.write_text(json.dumps(payload))
        with pytest.raises(SidecarError, match="keys outside sidecar schema 4: graphql_operation"):
            load_sidecar(path)
        del payload["graphql_operation"]
        path.write_text(json.dumps(payload))
        assert load_sidecar(path).graphql_operation is None

    def test_an_empty_operation_is_refused(self) -> None:
        with pytest.raises(SidecarError, match="graphql_operation"):
            Sidecar(status=200, content_type="application/json", graphql_operation=" ")


def _fixture_dir(tmp_path: Path, **files: str) -> Path:
    tree = tmp_path / "fixtures"
    tree.mkdir()
    for name, text in files.items():
        (tree / name).write_text(text)
    return tree


class TestFixtureSession:
    def _orders_body(self, customer_id: str = "4021") -> dict:
        return graphql_har.orders_body(customer_id=customer_id)

    def test_a_request_is_answered_by_its_operation_s_fixture(self, tmp_path: Path) -> None:
        tree = _fixture_dir(
            tmp_path,
            **{
                "post_graphql(OrdersByCustomer).json": '{"data": {"orders": []}}',
                "post_graphql(AddToCart).json": '{"data": {"addToCart": {"ok": true}}}',
                "post_graphql.json": '{"results": []}',
            },
        )
        session = FixtureSession(tree)
        url = graphql_har.GRAPHQL_URL
        assert session.post(url, json=self._orders_body()).json() == {"data": {"orders": []}}
        cart = {"operationName": "AddToCart", "query": graphql_har.ADD_TO_CART_DOCUMENT}
        assert session.post(url, data=json.dumps(cart)).json()["data"]["addToCart"]["ok"]
        assert session.post(url, json={"query": "red shoes"}).json() == {"results": []}

    def test_an_operation_without_a_fixture_fails_loudly(self, tmp_path: Path) -> None:
        tree = _fixture_dir(tmp_path, **{"post_graphql.json": '{"results": []}'})
        with pytest.raises(FixtureNotFoundError, match=r"post_graphql\(OrdersByCustomer\)"):
            FixtureSession(tree).post(graphql_har.GRAPHQL_URL, json=self._orders_body())

    def test_every_request_answered_is_recorded(self, tmp_path: Path) -> None:
        tree = _fixture_dir(tmp_path, **{"post_graphql(OrdersByCustomer).json": "{}"})
        session = FixtureSession(tree)
        session.post(graphql_har.GRAPHQL_URL, json=self._orders_body("4021"))
        session.post(graphql_har.GRAPHQL_URL, json=self._orders_body("4022"))
        session.get(f"{graphql_har.HOST}/api/missing")
        assert [(r.method, r.path, r.operation) for r in session.requests] == [
            ("POST", "/graphql", "OrdersByCustomer"),
            ("POST", "/graphql", "OrdersByCustomer"),
            ("GET", "/api/missing", None),
        ]
        assert session.requests[1].variables == {
            "customerId": "4022",
            "first": 10,
            "status": "OPEN",
        }
        assert session.requests[2].body is None

    def test_an_answer_overrides_the_directory_for_one_row(self, tmp_path: Path) -> None:
        tree = _fixture_dir(tmp_path, **{"post_graphql(OrdersByCustomer).json": "{}"})
        session = FixtureSession(tree)
        session.answer(_ORDERS, status=400, body='{"errors": [{"message": "Bad"}]}')
        session.answer(
            "POST /graphql (AddToCart)", status=200, content_type="text/html", body="<p>"
        )
        response = session.post(graphql_har.GRAPHQL_URL, json=self._orders_body())
        assert response.status_code == 400
        assert response.json() == {"errors": [{"message": "Bad"}]}
        cart = {"operationName": "AddToCart", "query": graphql_har.ADD_TO_CART_DOCUMENT}
        page = session.post(graphql_har.GRAPHQL_URL, json=cart)
        assert (page.status_code, page.headers["Content-Type"], page.text) == (
            200,
            "text/html",
            "<p>",
        )

    def test_a_batch_is_looked_up_by_the_plain_stem(self, tmp_path: Path) -> None:
        tree = _fixture_dir(tmp_path, **{"post_graphql.json": "[]"})
        batch = [self._orders_body(), {"operationName": "Cart", "query": "query Cart { c }"}]
        session = FixtureSession(tree)
        assert session.post(graphql_har.GRAPHQL_URL, json=batch).json() == []
        assert session.requests[0].operation is None


def _new_project(app: typer.Typer, target: Path, *commands: str) -> tuple[str, dict[str, str]]:
    argv = ["plugin", "new", "myshop", "--from-run", "myshop", "--dir", str(target)]
    for value in commands:
        argv += ["--command", value]
    result = CliRunner().invoke(app, argv)
    assert result.exit_code == 0, result.output
    package = target / "src" / "graftpunk_myshop"
    documents = {p.name: p.read_text() for p in (package / "graphql").glob("*.graphql")}
    return (package / "plugin.py").read_text(), documents


def _project_pytest(target: Path) -> str:
    for meta in (target / "tests" / "fixtures").glob("*.meta.json"):
        meta.write_text(sidecar_text(dataclasses.replace(load_sidecar(meta), capture_sha256=None)))
    env = {**os.environ, "PYTHONPATH": str(target / "src")}
    tested = subprocess.run(  # noqa: S603 - argv is fixed, not untrusted input
        [sys.executable, "-m", "pytest", "tests", "-q", "-p", "no:cacheprovider"],
        cwd=target,
        capture_output=True,
        text=True,
        env=env,
    )
    assert tested.returncode == 0, tested.stdout + tested.stderr
    return tested.stdout


def _assert_ruff_clean(project: Path) -> None:
    for argv in (["check", "."], ["format", "--check", "."]):
        linted = subprocess.run(  # noqa: S603 - argv is fixed, not untrusted input
            [sys.executable, "-m", "ruff", *argv], cwd=project, capture_output=True, text=True
        )
        assert linted.returncode == 0, linted.stdout + linted.stderr


class TestTheGeneratedPlugin:
    def test_two_operation_keys_give_two_stubs_sending_their_own_operation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app = _app(tmp_path, monkeypatch)
        target = tmp_path / "out"
        plugin, documents = _new_project(
            app, target, f"orders={_ORDERS}", "add-to-cart=POST /graphql (AddToCart)"
        )
        assert plugin.count("def _graphql(") == 1
        assert 'endpoint="POST /graphql (OrdersByCustomer)"' in plugin
        assert 'endpoint="POST /graphql (AddToCart)"' in plugin
        orders = plugin.split("def orders(", 1)[1].split("@command(", 1)[0]
        assert '"OrdersByCustomer",' in orders
        assert "customer_id: str | None = None," in orders
        assert "first: int | None = 10," in orders
        assert 'status: str | None = "OPEN",' in orders
        assert "contact: str | None = None," in orders
        assert '"customerId": customer_id,' in orders
        assert 'variable "customerId" (str) has a recorded value, which is not' in " ".join(
            line.strip().lstrip("#").strip() for line in orders.splitlines()
        )
        cart = plugin.split("def add_to_cart(", 1)[1]
        assert '"AddToCart",' in cart
        assert "sku: str | None = None," in cart
        assert "quantity: int | None = 2," in cart
        # The documents are files, written verbatim, never inline.
        assert documents == {
            "OrdersByCustomer.graphql": graphql_har.ORDERS_DOCUMENT,
            "AddToCart.graphql": graphql_har.ADD_TO_CART_DOCUMENT,
        }
        assert "fragment OrderFields" not in plugin
        # The helper names the held-back extension and sends the header names.
        helper = plugin.split("def _graphql(", 1)[1].split("@command(", 1)[0]
        assert "extension(s) clientLibrary with recorded values" in helper
        assert "extensions: dict = {}" in helper
        assert '"x-app-name": "GP-FILL",' in helper
        recorded = ("4021", "red-mug", "3.8.1", graphql_har.EMAIL, graphql_har.PASSWORD)
        for value in (*recorded, graphql_har.SESSION_VALUE):
            assert value not in plugin
        _assert_ruff_clean(target)

    def test_the_generated_tests_pass_on_the_operation_fixtures(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app = _app(tmp_path, monkeypatch)
        target = tmp_path / "out"
        plugin, documents = _new_project(app, target)
        # Default names come from the operation; the login's own row is skipped.
        for name in ("orders_by_customer", "add_to_cart", "product_page", "cart"):
            assert f"def {name}(" in plugin
        assert "def login_mutation(" not in plugin
        assert "persisted_query={" in plugin
        assert set(documents) == {
            "OrdersByCustomer.graphql",
            "AddToCart.graphql",
            "Cart.graphql",
            "graphql_2.graphql",
        }
        matches = ("POST /graphql (*)", "POST /graphql", "GET /")
        _fixtures(app, target / "tests" / "fixtures", *matches)
        tests = (target / "tests" / "test_plugin.py").read_text()
        assert 'assert ctx.session.requests[-1].operation == "OrdersByCustomer"' in tests
        assert "no test for cart (POST /graphql (Cart))" in tests
        # plugin_instantiates, root, the plain POST, and four operations.
        assert "7 passed" in _project_pytest(target)
        _assert_ruff_clean(target)

    def test_an_anonymous_operation_is_sent_without_a_name(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app = _app(tmp_path, monkeypatch)
        plugin, documents = _new_project(app, tmp_path / "out", "viewer=POST /graphql (anonymous)")
        stub = plugin.split("def viewer(", 1)[1]
        assert "            None,\n" in stub
        assert 'document="viewer",' in stub
        assert documents == {"viewer.graphql": graphql_har.ANONYMOUS_DOCUMENT}

    def test_a_template_with_operations_refuses_the_plain_key_with_a_hint(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        observe_base = tmp_path / "observe"
        monkeypatch.setattr("graftpunk.cli.observe_commands.OBSERVE_BASE_DIR", observe_base)
        entries = [e for e in graphql_har.entries() if "red shoes" not in json.dumps(e)]
        run = observe_base / "myshop" / "run-1"
        run.mkdir(parents=True)
        _write_har(run, entries)
        app = typer.Typer()
        app.add_typer(plugin_app)
        app.add_typer(observe_app)
        target = tmp_path / "out"
        argv = ["plugin", "new", "myshop", "--from-run", "myshop", "--dir", str(target)]
        result = CliRunner().invoke(app, [*argv, "--command", "g=POST /graphql"])
        assert result.exit_code == 1
        output = " ".join(strip_ansi(result.output).split())
        assert "Its GraphQL operations are rows of their own" in output
        assert not target.exists()

    def test_add_command_writes_the_document_and_the_helper_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _app(tmp_path, monkeypatch)
        target = tmp_path / "out"
        new = ["plugin", "new", "myshop", "--from-run", "myshop", "--dir", str(target)]
        result = CliRunner().invoke(main_app, [*new, "--command", "home=GET /"])
        assert result.exit_code == 0, result.output
        for value in (f"orders={_ORDERS}", "cart=POST /graphql (AddToCart)"):
            added = CliRunner().invoke(
                main_app,
                [
                    "plugin",
                    "add-command",
                    "myshop",
                    "--from-run",
                    "myshop",
                    "--command",
                    value,
                    "--dir",
                    str(target),
                ],
            )
            assert added.exit_code == 0, added.output
        package = target / "src" / "graftpunk_myshop"
        plugin = (package / "plugin.py").read_text()
        assert plugin.count("def _graphql(") == 1
        assert "from pathlib import Path" in plugin
        # The second stub passes its path: the helper it found may default to another.
        cart = plugin.split("def cart(", 1)[1]
        assert 'path="/graphql",' in cart
        orders = plugin.split("def orders(", 1)[1].split("def cart(", 1)[0]
        assert "path=" not in orders
        assert (package / "graphql" / "OrdersByCustomer.graphql").read_text() == (
            graphql_har.ORDERS_DOCUMENT
        )
        assert (package / "graphql" / "AddToCart.graphql").exists()
        _assert_ruff_clean(target)


class TestALongDocument:
    def test_a_long_document_with_fragments_round_trips(self, tmp_path: Path) -> None:
        """A real client's documents run to hundreds of lines with fragments: the
        file is the recording's text, and the fixture and its sidecar take it."""
        fields = "\n".join(f"  field{i} {{ id name }}" for i in range(300))
        document = (
            f"fragment Big on Thing {{\n{fields}\n}}\n\nquery LongOne {{ thing {{ ...Big }} }}\n"
        )
        assert document.count("\n") > 300
        body = {"operationName": "LongOne", "variables": {}, "query": document}
        entries = [
            _entry(
                "POST",
                graphql_har.GRAPHQL_URL,
                post_data=json.dumps(body),
                body=json.dumps({"data": {"thing": {"field1": {"id": "1"}}}}),
            )
        ]
        row = _row(_digest(tmp_path, entries), "POST /graphql (LongOne)")
        assert row.graphql_document == document
        assert filled_document(document) == document
