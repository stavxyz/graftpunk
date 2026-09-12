"""ScaffoldSpec -> file content, from a synthetic RunDigest (plugin tooling spec, 2026-09-11)."""

from __future__ import annotations

from graftpunk.devtools.scaffold.render import (
    _MAX_SCAFFOLD_ENDPOINTS,
    ScaffoldSpec,
    class_name_for,
    render,
)
from graftpunk.har.digest import (
    DigestSource,
    Endpoint,
    LoginForm,
    LoginObservation,
    RunDigest,
    ShapeNode,
    TokenCandidate,
)


def _digest(
    *,
    endpoints: tuple[Endpoint, ...] = (),
    login_forms: tuple[LoginForm, ...] = (),
    login: tuple[LoginObservation, ...] = (),
    tokens: tuple[TokenCandidate, ...] = (),
) -> RunDigest:
    return RunDigest(
        source=DigestSource(
            har_path=__import__("pathlib").Path("network.har"), session="myshop", run_id="run-1"
        ),
        primary_host="api.myshop.example.com",
        hosts={"api.myshop.example.com": 3},
        endpoints=endpoints,
        login=login,
        login_forms=login_forms,
        tokens=tokens,
        cookies=(),
        dropped={"static": 0, "third_party": 0, "error": 0},
    )


_ORDERS_ENDPOINT = Endpoint(
    host="api.myshop.example.com",
    template="/orders/{order_id}",
    methods=("GET",),
    count=5,
    statuses=(200,),
    content_type="application/json",
    query_params={"page": "int"},
    body_params={},
    body_kind="none",
    shape=ShapeNode(kind="object", children={"id": ShapeNode(kind="number")}),
    custom_headers=("X-Shop-Client",),
    examples=("/orders/1",),
)


class TestClassNameFor:
    def test_simple_name(self) -> None:
        assert class_name_for("myshop") == "MyshopPlugin"

    def test_hyphenated_name(self) -> None:
        assert class_name_for("my-shop") == "MyShopPlugin"


class TestRenderNewProject:
    def test_produces_the_expected_file_set(self) -> None:
        spec = ScaffoldSpec(
            name="myshop",
            mode="new_project",
            backend="nodriver",
            base_url="https://myshop.example.com",
        )
        files = render(spec)
        assert set(files) == {
            "pyproject.toml",
            "src/graftpunk_myshop/__init__.py",
            "src/graftpunk_myshop/plugin.py",
            "tests/conftest.py",
            "tests/test_plugin.py",
            "tests/fixtures/.gitkeep",
            ".gitignore",
            "README.md",
        }

    def test_pyproject_declares_entry_point_and_dependency_floor(self) -> None:
        spec = ScaffoldSpec(
            name="myshop",
            mode="new_project",
            backend="nodriver",
            base_url="https://myshop.example.com",
            graftpunk_version="1.17.0",
        )
        pyproject = render(spec)["pyproject.toml"]
        assert '[project.entry-points."graftpunk.plugins"]' in pyproject
        assert 'myshop = "graftpunk_myshop.plugin:MyshopPlugin"' in pyproject
        assert "graftpunk[browser]>=1.17.0" in pyproject
        assert 'select = ["E", "F", "I", "UP", "B"]' in pyproject

    def test_gitignore_contains_captures_dir(self) -> None:
        spec = ScaffoldSpec(
            name="myshop",
            mode="new_project",
            backend="nodriver",
            base_url="https://myshop.example.com",
        )
        assert "tests/captures/" in render(spec)[".gitignore"]

    def test_conftest_is_two_declarations(self) -> None:
        spec = ScaffoldSpec(
            name="myshop",
            mode="new_project",
            backend="nodriver",
            base_url="https://myshop.example.com",
        )
        conftest = render(spec)["tests/conftest.py"]
        assert 'pytest_plugins = ["graftpunk.testing.plugin"]' in conftest
        assert 'site_env_scrubber("MYSHOP_")' in conftest


class TestRenderAddToSuite:
    def test_produces_only_the_incremental_files(self) -> None:
        spec = ScaffoldSpec(
            name="widgets",
            mode="add_to_suite",
            backend="nodriver",
            base_url="https://myshop.example.com",
        )
        files = render(spec)
        assert set(files) == {
            "src/graftpunk_widgets/__init__.py",
            "src/graftpunk_widgets/plugin.py",
            "tests/test_widgets.py",
            "tests/fixtures/.gitkeep",
        }


class TestPluginModuleWithLoginForm:
    def test_login_config_uses_the_first_password_form(self) -> None:
        form = LoginForm(
            action="/login",
            method="POST",
            fields={"username": "#email", "password": "#pw"},
            submit="#login-btn",
            hidden=("_token",),
            source="page-source.html",
        )
        spec = ScaffoldSpec(
            name="myshop",
            mode="new_project",
            backend="nodriver",
            base_url="https://myshop.example.com",
            digest=_digest(login_forms=(form,)),
        )
        plugin_code = render(spec)["src/graftpunk_myshop/plugin.py"]
        assert "login_config = LoginConfig(" in plugin_code
        assert '"username": "#email"' in plugin_code
        assert '"password": "#pw"' in plugin_code
        assert 'submit="#login-btn"' in plugin_code
        assert 'url="/login"' in plugin_code
        assert "GP-FILL" in plugin_code  # failure/success still need a human

    def test_no_form_found_emits_commented_block_with_observations(self) -> None:
        observation = LoginObservation(
            order=1,
            method="POST",
            url="https://api.myshop.example.com/api/auth",
            status=200,
            kind="auth_api",
            fields=(),
        )
        spec = ScaffoldSpec(
            name="myshop",
            mode="new_project",
            backend="nodriver",
            base_url="https://myshop.example.com",
            digest=_digest(login=(observation,)),
        )
        plugin_code = render(spec)["src/graftpunk_myshop/plugin.py"]
        assert "# login_config = LoginConfig" in plugin_code
        assert "auth_api" in plugin_code


class TestPluginModuleWithTokens:
    def test_paired_header_and_meta_candidates_emit_token_config(self) -> None:
        header = TokenCandidate(kind="header", name="X-CSRF-Token", seen_on=("GET /dashboard",))
        meta = TokenCandidate(kind="meta", name="X-CSRF-Token", seen_on=("page-source.html",))
        spec = ScaffoldSpec(
            name="myshop",
            mode="new_project",
            backend="nodriver",
            base_url="https://myshop.example.com",
            digest=_digest(tokens=(header, meta)),
        )
        plugin_code = render(spec)["src/graftpunk_myshop/plugin.py"]
        assert "from graftpunk.tokens import Token, TokenConfig" in plugin_code
        assert 'Token.from_meta_tag(name="X-CSRF-Token", header="X-CSRF-Token")' in plugin_code

    def test_paired_header_and_cookie_candidates_use_from_cookie(self) -> None:
        header = TokenCandidate(kind="header", name="X-Csrf", seen_on=("GET /a",))
        cookie = TokenCandidate(kind="cookie", name="X-Csrf", seen_on=("GET /a",))
        spec = ScaffoldSpec(
            name="myshop",
            mode="new_project",
            backend="nodriver",
            base_url="https://myshop.example.com",
            digest=_digest(tokens=(header, cookie)),
        )
        plugin_code = render(spec)["src/graftpunk_myshop/plugin.py"]
        assert 'Token.from_cookie(cookie_name="X-Csrf", header="X-Csrf")' in plugin_code

    def test_unpaired_candidate_is_a_gp_fill_line(self) -> None:
        lone = TokenCandidate(kind="header", name="X-Lonely", seen_on=("GET /a",))
        spec = ScaffoldSpec(
            name="myshop",
            mode="new_project",
            backend="nodriver",
            base_url="https://myshop.example.com",
            digest=_digest(tokens=(lone,)),
        )
        plugin_code = render(spec)["src/graftpunk_myshop/plugin.py"]
        assert "GP-FILL: unpaired token candidate: header 'X-Lonely'" in plugin_code


class TestPluginModuleCommandStubs:
    def test_stub_per_endpoint_up_to_the_cap(self) -> None:
        endpoints = tuple(
            Endpoint(
                host="api.myshop.example.com",
                template=f"/item-{i}",
                methods=("GET",),
                count=1,
                statuses=(200,),
                content_type="application/json",
                query_params={},
                body_params={},
                body_kind="none",
                shape=ShapeNode(kind="object", children={}),
                custom_headers=(),
                examples=(),
            )
            for i in range(15)
        )
        spec = ScaffoldSpec(
            name="myshop",
            mode="new_project",
            backend="nodriver",
            base_url="https://myshop.example.com",
            digest=_digest(endpoints=endpoints),
        )
        plugin_code = render(spec)["src/graftpunk_myshop/plugin.py"]
        assert plugin_code.count("@command(") == _MAX_SCAFFOLD_ENDPOINTS

    def test_json_endpoint_calls_request_json_with_xhr_role(self) -> None:
        spec = ScaffoldSpec(
            name="myshop",
            mode="new_project",
            backend="nodriver",
            base_url="https://myshop.example.com",
            digest=_digest(endpoints=(_ORDERS_ENDPOINT,)),
        )
        plugin_code = render(spec)["src/graftpunk_myshop/plugin.py"]
        assert 'ctx.request_json("GET", f"/orders/{order_id}"' in plugin_code
        assert 'role="xhr"' in plugin_code

    def test_path_param_becomes_a_required_argument(self) -> None:
        spec = ScaffoldSpec(
            name="myshop",
            mode="new_project",
            backend="nodriver",
            base_url="https://myshop.example.com",
            digest=_digest(endpoints=(_ORDERS_ENDPOINT,)),
        )
        plugin_code = render(spec)["src/graftpunk_myshop/plugin.py"]
        assert "order_id: str" in plugin_code

    def test_query_param_becomes_an_optional_option(self) -> None:
        spec = ScaffoldSpec(
            name="myshop",
            mode="new_project",
            backend="nodriver",
            base_url="https://myshop.example.com",
            digest=_digest(endpoints=(_ORDERS_ENDPOINT,)),
        )
        plugin_code = render(spec)["src/graftpunk_myshop/plugin.py"]
        assert "page: int | None = None" in plugin_code

    def test_custom_headers_passed_explicitly(self) -> None:
        spec = ScaffoldSpec(
            name="myshop",
            mode="new_project",
            backend="nodriver",
            base_url="https://myshop.example.com",
            digest=_digest(endpoints=(_ORDERS_ENDPOINT,)),
        )
        plugin_code = render(spec)["src/graftpunk_myshop/plugin.py"]
        assert '"X-Shop-Client": "GP-FILL"' in plugin_code

    def test_html_endpoint_calls_request_text_with_navigation_role(self) -> None:
        html_endpoint = Endpoint(
            host="api.myshop.example.com",
            template="/page",
            methods=("GET",),
            count=1,
            statuses=(200,),
            content_type="text/html",
            query_params={},
            body_params={},
            body_kind="none",
            shape=None,
            custom_headers=(),
            examples=(),
        )
        spec = ScaffoldSpec(
            name="myshop",
            mode="new_project",
            backend="nodriver",
            base_url="https://myshop.example.com",
            digest=_digest(endpoints=(html_endpoint,)),
        )
        plugin_code = render(spec)["src/graftpunk_myshop/plugin.py"]
        assert 'ctx.request_text("GET", "/page", role="navigation")' in plugin_code

    def test_no_digest_emits_one_gp_fill_stub(self) -> None:
        spec = ScaffoldSpec(
            name="myshop",
            mode="new_project",
            backend="nodriver",
            base_url="https://myshop.example.com",
        )
        plugin_code = render(spec)["src/graftpunk_myshop/plugin.py"]
        assert plugin_code.count("@command(") == 1
        assert "GP-FILL" in plugin_code


class TestGeneratedPluginModuleParses:
    def test_ast_parses_with_and_without_a_digest(self) -> None:
        import ast

        bare = ScaffoldSpec(
            name="myshop",
            mode="new_project",
            backend="nodriver",
            base_url="https://myshop.example.com",
        )
        ast.parse(render(bare)["src/graftpunk_myshop/plugin.py"])

        full = ScaffoldSpec(
            name="myshop",
            mode="new_project",
            backend="nodriver",
            base_url="https://myshop.example.com",
            digest=_digest(
                endpoints=(_ORDERS_ENDPOINT,),
                login_forms=(
                    LoginForm(
                        action="/login",
                        method="POST",
                        fields={"password": "#pw"},
                        submit="#go",
                        hidden=(),
                        source="s",
                    ),
                ),
                tokens=(
                    TokenCandidate(kind="header", name="X-T", seen_on=("a",)),
                    TokenCandidate(kind="meta", name="X-T", seen_on=("b",)),
                ),
            ),
        )
        ast.parse(render(full)["src/graftpunk_myshop/plugin.py"])
