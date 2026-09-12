"""ScaffoldSpec -> file content, from a synthetic RunDigest (plugin tooling spec, 2026-09-11)."""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

from graftpunk.devtools.scaffold.render import (
    _MAX_SCAFFOLD_ENDPOINTS,
    PLUGIN_NAME_RE,
    ScaffoldSpec,
    class_name_for,
    module_name_for,
    render,
    validate_plugin_name,
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

_SEARCH_ENDPOINT = Endpoint(
    host="api.myshop.example.com",
    template="/search",
    methods=("GET",),
    count=9,
    statuses=(200,),
    content_type="application/json",
    query_params={
        "q": "str",
        "page": "int",
        "per_page": "int",
        "sort": "str",
        "filter": "str",
        "include_meta": "bool",
    },
    body_params={},
    body_kind="none",
    shape=ShapeNode(kind="object", children={"results": ShapeNode(kind="array")}),
    custom_headers=(),
    examples=("/search?q=widget",),
)

_NOTES_ENDPOINT = Endpoint(
    host="api.myshop.example.com",
    template="/orders/{order_id}/notes",
    methods=("POST",),
    count=3,
    statuses=(201,),
    content_type="application/json",
    query_params={},
    body_params={
        "body": "str",
        "author": "str",
        "pinned": "bool",
        "tags": "list",
    },
    body_kind="json",
    shape=ShapeNode(kind="object", children={"id": ShapeNode(kind="number")}),
    custom_headers=(),
    examples=("/orders/1/notes",),
)

_PASSWORD_LOGIN_FORM = LoginForm(
    action="/login",
    method="POST",
    fields={"username": "#email", "password": "#pw"},
    submit="#login-btn",
    hidden=("_token",),
    source="page-source.html",
)

_HEADER_TOKEN = TokenCandidate(kind="header", name="X-CSRF-Token", seen_on=("GET /dashboard",))
_COOKIE_TOKEN = TokenCandidate(kind="cookie", name="X-CSRF-Token", seen_on=("GET /dashboard",))


class TestClassNameFor:
    def test_simple_name(self) -> None:
        assert class_name_for("myshop") == "MyshopPlugin"

    def test_hyphenated_name(self) -> None:
        assert class_name_for("my-shop") == "MyShopPlugin"


class TestValidatePluginName:
    @pytest.mark.parametrize("name", ["myshop", "my-shop", "my_shop2"])
    def test_accepts(self, name: str) -> None:
        validate_plugin_name(name)  # does not raise

    @pytest.mark.parametrize("name", ["2fa-site", "my shop", "my.shop", "-shop", ""])
    def test_rejects_with_the_message(self, name: str) -> None:
        with pytest.raises(ValueError, match="must start with a letter and contain only"):
            validate_plugin_name(name)

    def test_matches_the_public_regex_directly(self) -> None:
        assert PLUGIN_NAME_RE.fullmatch("my-shop")
        assert not PLUGIN_NAME_RE.fullmatch("2fa-site")


class TestModuleNameFor:
    def test_hyphens_become_underscores(self) -> None:
        assert module_name_for("my-shop") == "my_shop"

    def test_already_lowercase_and_clean(self) -> None:
        assert module_name_for("myshop") == "myshop"


class TestScaffoldSpecValidatesItsName:
    def test_invalid_name_raises(self) -> None:
        with pytest.raises(ValueError, match="must start with a letter"):
            ScaffoldSpec(
                name="2fa-site",
                mode="new_project",
                backend="nodriver",
                base_url="https://myshop.example.com",
            )


class TestPluginNameNormalization:
    def test_hyphenated_name_produces_an_importable_project(self) -> None:
        spec = ScaffoldSpec(
            name="my-shop",
            mode="new_project",
            backend="nodriver",
            base_url="https://myshop.example.com",
        )
        files = render(spec)
        assert "src/graftpunk_my_shop/__init__.py" in files
        assert "src/graftpunk_my_shop/plugin.py" in files
        ast.parse(files["src/graftpunk_my_shop/plugin.py"])
        test_module = files["tests/test_plugin.py"]
        ast.parse(test_module)
        assert "from graftpunk_my_shop.plugin import MyShopPlugin" in test_module
        pyproject = files["pyproject.toml"]
        assert 'my-shop = "graftpunk_my_shop.plugin:MyShopPlugin"' in pyproject
        assert 'packages = ["src/graftpunk_my_shop"]' in pyproject


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
        spec = ScaffoldSpec(
            name="myshop",
            mode="new_project",
            backend="nodriver",
            base_url="https://myshop.example.com",
            digest=_digest(login_forms=(_PASSWORD_LOGIN_FORM,)),
        )
        plugin_code = render(spec)["src/graftpunk_myshop/plugin.py"]
        assert "login_config = LoginConfig(" in plugin_code
        assert '"username": "#email"' in plugin_code
        assert '"password": "#pw"' in plugin_code
        assert 'submit="#login-btn"' in plugin_code
        assert 'url="/login"' in plugin_code
        assert "GP-FILL" in plugin_code  # failure/success still need a human
        assert "from graftpunk.plugins import" in plugin_code
        assert "LoginConfig" in plugin_code
        assert "LoginStep" in plugin_code

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

    def test_no_login_form_omits_the_login_import(self) -> None:
        spec = ScaffoldSpec(
            name="myshop",
            mode="new_project",
            backend="nodriver",
            base_url="https://myshop.example.com",
        )
        plugin_code = render(spec)["src/graftpunk_myshop/plugin.py"]
        import_line = next(
            line for line in plugin_code.splitlines() if line.startswith("from graftpunk.plugins")
        )
        assert "LoginConfig" not in import_line
        assert "LoginStep" not in import_line


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
        assert "return ctx.request_json(" in plugin_code
        assert '"GET",' in plugin_code
        assert 'f"/orders/{order_id}",' in plugin_code
        assert 'role="xhr",' in plugin_code

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
        assert '"X-Shop-Client": "GP-FILL",' in plugin_code

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
        assert "return ctx.request_text(" in plugin_code
        assert '"/page",' in plugin_code
        assert 'role="navigation",' in plugin_code

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

    def test_empty_endpoints_emits_one_gp_fill_stub(self) -> None:
        spec = ScaffoldSpec(
            name="myshop",
            mode="new_project",
            backend="nodriver",
            base_url="https://myshop.example.com",
            digest=_digest(endpoints=()),
        )
        plugin_code = render(spec)["src/graftpunk_myshop/plugin.py"]
        assert plugin_code.count("@command(") == 1
        assert "GP-FILL" in plugin_code

    def test_no_trailing_whitespace_on_any_line(self) -> None:
        spec = ScaffoldSpec(
            name="myshop",
            mode="new_project",
            backend="nodriver",
            base_url="https://myshop.example.com",
            digest=_digest(endpoints=(_ORDERS_ENDPOINT, _SEARCH_ENDPOINT, _NOTES_ENDPOINT)),
        )
        plugin_code = render(spec)["src/graftpunk_myshop/plugin.py"]
        for line in plugin_code.splitlines():
            assert line == line.rstrip()


class TestGeneratedPluginModuleParses:
    def test_ast_parses_with_and_without_a_digest(self) -> None:
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


class TestRenderedTreeIsRuffClean:
    """The generated project is a real ruff target: its own pyproject.toml declares
    the config, so running ruff against the written-out tree is the actual gate a
    freshly scaffolded plugin's own CI would run (validation Important 2, 2026-09-12).
    """

    def _write_tree(self, root: Path, files: dict[str, str]) -> Path:
        for relative_path, content in files.items():
            path = root / relative_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        return root

    def _assert_ruff_clean(self, tree: Path, *args: str) -> None:
        result = subprocess.run(  # noqa: S603 - argv is a fixed ruff invocation, not untrusted input
            [sys.executable, "-m", "ruff", *args, str(tree)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, (
            f"ruff {' '.join(args)} failed for {tree}:\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )

    def _assert_tree_is_clean(self, tree: Path) -> None:
        self._assert_ruff_clean(tree, "check")
        self._assert_ruff_clean(tree, "format", "--check")

    def test_no_digest_project(self, tmp_path: Path) -> None:
        spec = ScaffoldSpec(
            name="myshop",
            mode="new_project",
            backend="nodriver",
            base_url="https://myshop.example.com",
        )
        tree = self._write_tree(tmp_path / "no_digest", render(spec))
        self._assert_tree_is_clean(tree)

    def test_endpoints_project(self, tmp_path: Path) -> None:
        digest = _digest(endpoints=(_SEARCH_ENDPOINT, _NOTES_ENDPOINT))
        spec = ScaffoldSpec(
            name="myshop",
            mode="new_project",
            backend="nodriver",
            base_url="https://myshop.example.com",
            digest=digest,
        )
        tree = self._write_tree(tmp_path / "endpoints", render(spec))
        self._assert_tree_is_clean(tree)

    def test_login_and_token_project(self, tmp_path: Path) -> None:
        digest = _digest(login_forms=(_PASSWORD_LOGIN_FORM,), tokens=(_HEADER_TOKEN, _COOKIE_TOKEN))
        spec = ScaffoldSpec(
            name="myshop",
            mode="new_project",
            backend="nodriver",
            base_url="https://myshop.example.com",
            digest=digest,
        )
        tree = self._write_tree(tmp_path / "login_token", render(spec))
        self._assert_tree_is_clean(tree)
