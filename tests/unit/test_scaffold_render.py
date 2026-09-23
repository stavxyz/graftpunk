"""ScaffoldSpec -> file content, from a synthetic RunDigest (plugin tooling spec, 2026-09-11)."""

from __future__ import annotations

import ast
import dataclasses
import json
import subprocess
import sys
import warnings
from pathlib import Path
from typing import Any

import pytest

from graftpunk.devtools.scaffold import policy
from graftpunk.devtools.scaffold.pysrc import (
    GENERATED_LINE_LENGTH,
    literal_dict_entry_lines,
    literal_lines,
    wrapped_comment_lines,
    wrapped_docstring_lines,
)
from graftpunk.devtools.scaffold.render import (
    _MAX_COMMAND_NAME,
    _MAX_PARAM_NAME,
    _MAX_PLUGIN_NAME,
    _MAX_SCAFFOLD_ENDPOINTS,
    PLUGIN_NAME_RE,
    ScaffoldSpec,
    _command_name,
    _param_identifier,
    class_name_for,
    module_name_for,
    render,
    validate_plugin_name,
)
from graftpunk.har.digest import (
    SHAPE_UNAVAILABLE,
    DigestSource,
    Endpoint,
    LoginForm,
    LoginObservation,
    RunDigest,
    ShapeNode,
    TokenCandidate,
    digest,
)
from tests.unit.cli_harness import strip_ansi


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
        "tags": "list[str]",
    },
    body_kind="json",
    shape=ShapeNode(kind="object", children={"id": ShapeNode(kind="number")}),
    custom_headers=(),
    examples=("/orders/1/notes",),
)

_FORM_POST_ENDPOINT = Endpoint(
    host="api.myshop.example.com",
    template="/newsletter/subscribe",
    methods=("POST",),
    count=1,
    statuses=(200,),
    content_type="application/json",
    query_params={},
    body_params={"email": "str", "weekly": "bool"},
    body_kind="form",
    shape=ShapeNode(kind="object", children={"ok": ShapeNode(kind="boolean")}),
    custom_headers=(),
    examples=("/newsletter/subscribe",),
)

_PAYMENT_ENDPOINT = Endpoint(
    host="api.myshop.example.com",
    template="/payments",
    methods=("POST",),
    count=2,
    statuses=(201,),
    content_type="application/json",
    query_params={},
    body_params={"amount": "float"},
    body_kind="json",
    shape=ShapeNode(kind="object", children={"id": ShapeNode(kind="number")}),
    custom_headers=(),
    examples=("/payments",),
)

_SITE_NAMED_PARAMS_ENDPOINT = Endpoint(
    host="api.myshop.example.com",
    template="/results",
    methods=("GET",),
    count=4,
    statuses=(200,),
    content_type="application/json",
    query_params={
        "keywordSearch": "bool",
        "recorded-date-range": "str",
        "searchOcrText": "bool",
    },
    body_params={},
    body_kind="none",
    shape=ShapeNode(kind="object", children={"rows": ShapeNode(kind="array")}),
    custom_headers=(),
    examples=("/results?keywordSearch=false",),
)

_CAMEL_PATH_ENDPOINT = Endpoint(
    host="api.myshop.example.com",
    template="/searchResults/{searchResult_id}",
    methods=("GET",),
    count=2,
    statuses=(200,),
    content_type="application/json",
    query_params={},
    body_params={},
    body_kind="none",
    shape=ShapeNode(kind="object", children={"id": ShapeNode(kind="number")}),
    custom_headers=(),
    examples=("/searchResults/1",),
)

# Long enough that its stub's one-line summary pushes the run label past the
# docstring wrap width, so the label is what the wrapper has to place.
_LONG_TEMPLATE_ENDPOINT = Endpoint(
    host="api.myshop.example.com",
    template="/records/search-results/by-recorded-date-range/detail",
    methods=("GET",),
    count=3,
    statuses=(200,),
    content_type="application/json",
    query_params={},
    body_params={},
    body_kind="none",
    shape=ShapeNode(kind="object", children={"rows": ShapeNode(kind="array")}),
    custom_headers=(),
    examples=("/records/search-results/by-recorded-date-range/detail",),
)

# A template too wide for its endpoint= line, so the declaration wraps, and an int
# and a bool parameter named at the identifier cap, so their params= entries are
# too wide for one line each.
_LONG_TYPED_ENDPOINT = Endpoint(
    host="api.myshop.example.com",
    template=(
        "/accounts/{account_identifier}/statements/by-recorded-date-range/detail/summary/line-items"
    ),
    methods=("GET",),
    count=2,
    statuses=(200,),
    content_type="application/json",
    query_params={
        "include_" + "x" * (_MAX_PARAM_NAME - len("include_")): "bool",
        "page_" + "n" * (_MAX_PARAM_NAME - len("page_")): "int",
    },
    body_params={},
    body_kind="none",
    shape=ShapeNode(kind="object", children={"rows": ShapeNode(kind="array")}),
    custom_headers=(),
    examples=("/accounts/1/statements/by-recorded-date-range/detail/summary/line-items",),
)

_PASSWORD_LOGIN_FORM = LoginForm(
    action="/login",
    method="POST",
    fields={"username": "#email", "password": "#pw"},
    submit="#login-btn",
    hidden=("_token",),
    source="page-source.html",
)


def _class_docstring(code: str, name: str) -> str:
    """The docstring of class *name* in *code*, as Python itself reads it back."""
    for node in ast.parse(code).body:
        if isinstance(node, ast.ClassDef) and node.name == name:
            return ast.get_docstring(node) or ""
    raise AssertionError(f"no class {name} in the generated module")


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

    def test_accepts_a_name_at_the_cap(self) -> None:
        validate_plugin_name("a" * _MAX_PLUGIN_NAME)  # does not raise

    def test_rejects_a_name_one_over_the_cap(self) -> None:
        with pytest.raises(ValueError, match=f"at most {_MAX_PLUGIN_NAME} characters"):
            validate_plugin_name("a" * (_MAX_PLUGIN_NAME + 1))


class TestCommandName:
    def test_truncates_a_deep_path_to_the_cap(self) -> None:
        template = "/" + "/".join(f"segment-number-{i}" for i in range(8))
        name = _command_name(template, set())
        assert len(name) == _MAX_COMMAND_NAME

    def test_two_paths_truncating_to_the_same_base_stay_unique(self) -> None:
        first = "/" + "a" * (_MAX_COMMAND_NAME + 5) + "/one"
        second = "/" + "a" * (_MAX_COMMAND_NAME + 5) + "/two"
        seen: set[str] = set()
        first_name = _command_name(first, seen)
        second_name = _command_name(second, seen)
        assert first_name == "a" * _MAX_COMMAND_NAME
        assert second_name == f"{first_name}_2"

    def test_the_root_path_is_named_root(self) -> None:
        assert _command_name("/", set()) == "root"

    def test_a_placeholder_becomes_a_by_part(self) -> None:
        assert _command_name("/{order_id}", set()) == "by_order_id"

    def test_sibling_endpoints_read_as_what_they_fetch(self) -> None:
        """Dropping the placeholder named the second sibling api_orders_2."""
        seen: set[str] = set()
        assert _command_name("/api/orders", seen) == "api_orders"
        assert _command_name("/api/orders/{order_id}", seen) == "api_orders_by_order_id"

    def test_every_placeholder_contributes_a_by_part(self) -> None:
        assert _command_name("/a/{x}/b/{y}", set()) == "a_by_x_b_by_y"

    def test_the_counter_is_left_for_a_true_collision(self) -> None:
        seen: set[str] = set()
        assert _command_name("/api/orders", seen) == "api_orders"
        assert _command_name("/api/orders", seen) == "api_orders_2"


class TestParamIdentifier:
    def test_truncates_to_the_cap(self) -> None:
        identifier = _param_identifier("x" * (_MAX_PARAM_NAME + 20), set())
        assert identifier == "x" * _MAX_PARAM_NAME

    def test_dedupes_against_names_already_taken(self) -> None:
        seen = {"self", "ctx"}
        assert _param_identifier("page", seen) == "page"
        assert _param_identifier("page", seen) == "page_2"
        assert _param_identifier("ctx", seen) == "ctx_2"

    def test_a_python_keyword_gains_an_underscore(self) -> None:
        assert _param_identifier("class", set()) == "class_"

    def test_non_identifier_characters_become_underscores(self) -> None:
        assert _param_identifier("filter[by].name", set()) == "filter_by__name"

    def test_a_leading_digit_is_prefixed(self) -> None:
        assert _param_identifier("2fa", set()) == "p_2fa"

    def test_camel_case_becomes_snake_case(self) -> None:
        assert _param_identifier("keywordSearch", set()) == "keyword_search"
        assert _param_identifier("searchOcrText", set()) == "search_ocr_text"

    def test_kebab_case_becomes_snake_case(self) -> None:
        assert _param_identifier("recorded-date-range", set()) == "recorded_date_range"

    def test_an_all_caps_token_just_lowercases(self) -> None:
        assert _param_identifier("ID", set()) == "id"

    def test_an_upper_run_before_a_word_splits_once(self) -> None:
        assert _param_identifier("HTTPServer", set()) == "http_server"


class TestModuleNameFor:
    def test_hyphens_become_underscores(self) -> None:
        assert module_name_for("my-shop") == "my_shop"

    def test_already_lowercase_and_clean(self) -> None:
        assert module_name_for("myshop") == "myshop"


class TestLiteralLines:
    def test_short_value_renders_on_one_line(self) -> None:
        assert literal_lines("#login-btn", indent=8, prefix="submit=") == [
            '        submit="#login-btn",'
        ]

    def test_long_value_with_spaces_reconstructs_exactly(self) -> None:
        value = (
            "#login-form div.field-wrapper.username-wrapper > label + "
            "input[name='username'][type='text'].form-control.input-lg"
        )
        assert len(value) > 100
        indent = 16
        lines = literal_lines(value, indent=indent, prefix="submit=")
        pad = " " * indent
        continuation_pad = " " * (indent + 4)
        assert lines[0] == f"{pad}submit=("
        assert lines[-1] == f"{pad}),"
        chunks = [line[len(continuation_pad) + 1 : -1] for line in lines[1:-1]]
        assert "".join(chunks) == value
        for line in lines:
            assert len(line) <= 100

    def test_long_value_with_no_whitespace_reconstructs_exactly(self) -> None:
        value = "x" * 90
        indent = 12
        lines = literal_lines(value, indent=indent, prefix="header=")
        continuation_pad = " " * (indent + 4)
        chunks = [line[len(continuation_pad) + 1 : -1] for line in lines[1:-1]]
        assert "".join(chunks) == value
        for line in lines:
            assert len(line) <= 100


class TestLiteralDictEntryLines:
    def test_short_pair_renders_on_one_line(self) -> None:
        assert literal_dict_entry_lines("username", "#email", indent=16) == [
            '                "username": "#email",'
        ]

    def test_a_key_too_wide_for_its_line_splits_alongside_the_value(self) -> None:
        key = "k" * 110
        value = "v" * 110
        indent = 16
        lines = literal_dict_entry_lines(key, value, indent=indent)
        pad = " " * indent
        continuation_pad = " " * (indent + 4)
        assert lines[0] == f"{pad}("
        assert lines[-1] == f"{pad}),"
        colon_index = lines.index(f"{pad}): (")

        def chunks(from_lines: list[str]) -> str:
            return "".join(line[len(continuation_pad) + 1 : -1] for line in from_lines)

        key_chunks = chunks(lines[1:colon_index])
        value_chunks = chunks(lines[colon_index + 1 : -1])
        assert key_chunks == key
        assert value_chunks == value
        for line in lines:
            assert len(line) <= GENERATED_LINE_LENGTH


class TestWrappedCommentLines:
    def test_short_text_is_one_line(self) -> None:
        assert wrapped_comment_lines("short comment", indent=4) == ["    # short comment"]

    def test_long_text_reconstructs_after_stripping_prefixes(self) -> None:
        text = (
            "1. GET https://api.myshop.example.com/some/reasonably/long/path and "
            "several more plain words to force this comment across more than one "
            "wrapped line (auth_api)"
        )
        indent = 4
        lines = wrapped_comment_lines(text, indent=indent)
        assert len(lines) > 1
        pad = " " * indent
        first_prefix = f"{pad}# "
        rest_prefix = f"{pad}#   "
        assert lines[0].startswith(first_prefix)
        parts = [lines[0][len(first_prefix) :]]
        for line in lines[1:]:
            assert line.startswith(rest_prefix)
            parts.append(line[len(rest_prefix) :])
        assert " ".join(parts) == text
        for line in lines:
            assert len(line) <= 100


class TestDocstringWrappingKeepsHyphenatedFactsWhole:
    """A hyphen in this text belongs to a captured fact, never to a word the
    wrapper may break (polish round 2, 2026-09-12)."""

    # Long enough to leave room for "myshop-" but not for the whole label, so
    # a hyphen-breaking wrapper would split it exactly there.
    _FILLER = "x" * 75
    _LABEL = "myshop-tirekick/2026-09-11T22-19-05Z"

    def test_a_run_label_is_never_split_at_its_hyphen(self) -> None:
        lines = wrapped_docstring_lines(f"{self._FILLER} run {self._LABEL}.")
        assert len(lines) > 1, "the input must actually wrap for this test to mean anything"
        assert any(self._LABEL in line for line in lines)

    def test_the_generated_stub_docstring_carries_the_whole_run_label(self) -> None:
        digest_with_hyphenated_run = dataclasses.replace(
            _digest(endpoints=(_LONG_TEMPLATE_ENDPOINT,)),
            source=DigestSource(
                har_path=Path("network.har"),
                session="myshop-tirekick",
                run_id="2026-09-11T22-19-05Z",
            ),
        )
        spec = ScaffoldSpec(
            name="myshop",
            mode="new_project",
            backend="nodriver",
            base_url="https://myshop.example.com",
            digest=digest_with_hyphenated_run,
        )
        plugin_code = render(spec)["src/graftpunk_myshop/plugin.py"]
        assert "myshop-tirekick/2026-09-11T22-19-05Z" in plugin_code


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

    def test_hyphenated_name_in_add_to_suite_mode_uses_the_module_name(self) -> None:
        spec = ScaffoldSpec(
            name="my-shop",
            mode="add_to_suite",
            backend="nodriver",
            base_url="https://myshop.example.com",
        )
        files = render(spec)
        assert "tests/test_my_shop.py" in files
        assert "tests/test_my-shop.py" not in files


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

    def test_readme_names_the_policys_fixtures_tree(self) -> None:
        """The tests directory is spelled once, in policy: the README quotes it
        rather than a hand-spelled `tests/fixtures/`."""
        spec = ScaffoldSpec(
            name="myshop",
            mode="new_project",
            backend="nodriver",
            base_url="https://myshop.example.com",
        )
        readme = render(spec)["README.md"]
        assert f"Fixtures under `{policy.FIXTURES_TREE}`" in readme

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
        assert "from graftpunk.testing.plugin import site_env_scrubber" in conftest
        assert 'site_env_scrubber("MYSHOP_")' in conftest
        # Naming the module in pytest_plugins as well asks pytest to rewrite
        # assertions in a module the import already loaded, which it warns
        # about on every run of the generated suite.
        assert "pytest_plugins" not in conftest


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
            "tests/fixtures/widgets/.gitkeep",
        }

    def test_each_suite_member_owns_its_fixture_directory(self) -> None:
        """Two plugins in one suite with a GET /orders between them would claim
        the same fixture file if they shared one directory."""
        widgets = render(
            ScaffoldSpec(
                name="widgets",
                mode="add_to_suite",
                backend="nodriver",
                base_url="https://myshop.example.com",
            )
        )
        gadgets = render(
            ScaffoldSpec(
                name="gadgets",
                mode="add_to_suite",
                backend="nodriver",
                base_url="https://myshop.example.com",
            )
        )
        assert "tests/fixtures/widgets/.gitkeep" in widgets
        assert "tests/fixtures/gadgets/.gitkeep" in gadgets
        assert (
            'FIXTURES_DIR = Path(__file__).parent / "fixtures" / "widgets"'
            in widgets["tests/test_widgets.py"]
        )
        assert (
            'FIXTURES_DIR = Path(__file__).parent / "fixtures" / "gadgets"'
            in gadgets["tests/test_gadgets.py"]
        )

    def test_a_new_project_keeps_the_flat_fixtures_directory(self) -> None:
        files = render(
            ScaffoldSpec(
                name="myshop",
                mode="new_project",
                backend="nodriver",
                base_url="https://myshop.example.com",
            )
        )
        assert "tests/fixtures/.gitkeep" in files
        test_module = files["tests/test_plugin.py"]
        assert 'FIXTURES_DIR = Path(__file__).parent / "fixtures"\n' in test_module


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

    @staticmethod
    def _login_spec(*observations: LoginObservation) -> ScaffoldSpec:
        return ScaffoldSpec(
            name="myshop",
            mode="new_project",
            backend="nodriver",
            base_url="https://myshop.example.com",
            digest=_digest(login_forms=(_PASSWORD_LOGIN_FORM,), login=observations),
        )

    def test_success_url_comes_from_a_redirecting_credential_post(self) -> None:
        """The POST that answers 302 is the only observation, and it carries the path."""
        plugin_code = render(self._login_spec(_REDIRECTING_CREDENTIAL_POST))[
            "src/graftpunk_myshop/plugin.py"
        ]
        assert '        success_url="*/dashboard*",\n' in plugin_code
        # The element check is still a human's job, and a GP-FILL literal would be a
        # second configured signal the engine polls for and never finds.
        assert "# GP-FILL: success, a CSS selector for an element" in plugin_code
        assert "success=" not in plugin_code

    def test_success_url_comes_from_the_end_of_a_redirect_chain(self) -> None:
        """A post to /auth/callback to /dashboard prefills the last hop, not the first."""
        post_to_callback = LoginObservation(
            order=2,
            method="POST",
            url="https://api.myshop.example.com/login",
            status=302,
            kind="credential_post",
            fields=("password", "username"),
            redirect_to="/auth/callback",
        )
        plugin_code = render(self._login_spec(post_to_callback, _DASHBOARD_REDIRECT_OBSERVATION))[
            "src/graftpunk_myshop/plugin.py"
        ]
        assert '        success_url="*/dashboard*",\n' in plugin_code

    def test_a_redirect_before_the_credential_post_is_not_the_landing_page(self) -> None:
        """Only the span from the credential post onwards says where the login ended."""
        pre_login_redirect = LoginObservation(
            order=1,
            method="GET",
            url="https://api.myshop.example.com/account",
            status=302,
            kind="redirect",
            fields=(),
            redirect_to="/login",
        )
        plugin_code = render(self._login_spec(pre_login_redirect, _CREDENTIAL_POST_OBSERVATION))[
            "src/graftpunk_myshop/plugin.py"
        ]
        assert "# GP-FILL: success_url, a glob matched against the whole URL" in plugin_code
        # A GP-FILL literal would be a configured signal the engine polls for.
        assert "success_url=" not in plugin_code

    def test_success_url_is_a_comment_when_no_redirect_was_observed(self) -> None:
        """With nothing to copy from the run, success_url is left unset and explained."""
        spec = ScaffoldSpec(
            name="myshop",
            mode="new_project",
            backend="nodriver",
            base_url="https://myshop.example.com",
            digest=_digest(login_forms=(_PASSWORD_LOGIN_FORM,)),
        )
        plugin_code = render(spec)["src/graftpunk_myshop/plugin.py"]
        assert "# GP-FILL: success_url, a glob matched against the whole URL" in plugin_code
        # A GP-FILL literal would be a configured signal the engine polls for.
        assert "success_url=" not in plugin_code

    def test_a_glob_metacharacter_in_the_observed_path_is_escaped(self) -> None:
        """A path the site spells with brackets must not become glob syntax."""
        from fnmatch import fnmatchcase

        bracketed_redirect = LoginObservation(
            order=3,
            method="GET",
            url="https://app.myshop.example.com/auth/callback",
            status=302,
            kind="redirect",
            fields=(),
            redirect_to="/a[b]/c",
        )
        plugin_code = render(self._login_spec(_CREDENTIAL_POST_OBSERVATION, bracketed_redirect))[
            "src/graftpunk_myshop/plugin.py"
        ]
        assert '        success_url="*/a[[]b]/c*",\n' in plugin_code
        assert fnmatchcase("https://app.myshop.example.com/a[b]/c?x=1", "*/a[[]b]/c*")

    def test_a_redirect_to_the_site_root_yields_no_pattern(self) -> None:
        """``*/*`` would match the login page too, so the root is left to be filled in."""
        root_redirect = LoginObservation(
            order=3,
            method="GET",
            url="https://app.myshop.example.com/auth/callback",
            status=302,
            kind="redirect",
            fields=(),
            redirect_to="/",
        )
        plugin_code = render(self._login_spec(_CREDENTIAL_POST_OBSERVATION, root_redirect))[
            "src/graftpunk_myshop/plugin.py"
        ]
        assert "# GP-FILL: success_url, a glob matched against the whole URL" in plugin_code
        # A GP-FILL literal would be a configured signal the engine polls for.
        assert "success_url=" not in plugin_code

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

    def test_an_observation_comment_carries_the_templated_url(self) -> None:
        """A login URL's path can hold an account id or a one-time token, and the
        comment lands in a file the author commits."""
        segment = "7f3a9c2e8b1d4f60a9e2c3b4d5f6a7b8"
        observation = LoginObservation(
            order=1,
            method="GET",
            url=f"https://myshop.example.com/signin/{segment}",
            status=200,
            kind="form_page",
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
        assert segment not in plugin_code
        assert "https://myshop.example.com/signin/{signin_id} (form_page)" in plugin_code

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
        assert "Token.from_meta_tag(" in plugin_code
        assert 'name="X-CSRF-Token",' in plugin_code
        assert 'header="X-CSRF-Token",' in plugin_code

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
        assert "Token.from_cookie(" in plugin_code
        assert 'cookie_name="X-Csrf",' in plugin_code
        assert 'header="X-Csrf",' in plugin_code

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

    def test_a_captured_method_carrying_a_quote_still_parses(self) -> None:
        """The method is a captured value like any other: interpolated bare it
        ended its own string literal."""
        endpoint = Endpoint(
            host="api.myshop.example.com",
            template="/orders",
            methods=('GE"T',),
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
        spec = ScaffoldSpec(
            name="myshop",
            mode="new_project",
            backend="nodriver",
            base_url="https://myshop.example.com",
            digest=_digest(endpoints=(endpoint,)),
        )
        plugin_code = render(spec)["src/graftpunk_myshop/plugin.py"]
        assert """'GE"T',""" in plugin_code
        ast.parse(plugin_code)

    def test_a_get_that_recorded_a_body_declares_no_body_arguments(self) -> None:
        """The body dict is only emitted for a mutating method, so declaring its
        parameters on a GET gave the stub arguments it never used."""
        endpoint = Endpoint(
            host="api.myshop.example.com",
            template="/search",
            methods=("GET",),
            count=1,
            statuses=(200,),
            content_type="application/json",
            query_params={"q": "str"},
            body_params={"filters": "str", "cursor": "str"},
            body_kind="json",
            shape=ShapeNode(kind="object", children={}),
            custom_headers=(),
            examples=(),
        )
        spec = ScaffoldSpec(
            name="myshop",
            mode="new_project",
            backend="nodriver",
            base_url="https://myshop.example.com",
            digest=_digest(endpoints=(endpoint,)),
        )
        plugin_code = render(spec)["src/graftpunk_myshop/plugin.py"]
        assert "q: str | None = None" in plugin_code
        assert "filters" not in plugin_code
        assert "cursor" not in plugin_code
        assert "json={" not in plugin_code
        ast.parse(plugin_code)

    def test_a_post_still_declares_its_body_arguments(self) -> None:
        spec = ScaffoldSpec(
            name="myshop",
            mode="new_project",
            backend="nodriver",
            base_url="https://myshop.example.com",
            digest=_digest(endpoints=(_NOTES_ENDPOINT,)),
        )
        plugin_code = render(spec)["src/graftpunk_myshop/plugin.py"]
        assert "author: str | None = None" in plugin_code
        assert '"author": author,' in plugin_code

    def test_a_camel_case_site_parameter_is_declared_in_snake_case(self) -> None:
        """The identifier is this project's own; the dict key stays the site's
        (polish round 2, 2026-09-12)."""
        spec = ScaffoldSpec(
            name="myshop",
            mode="new_project",
            backend="nodriver",
            base_url="https://myshop.example.com",
            digest=_digest(endpoints=(_SITE_NAMED_PARAMS_ENDPOINT,)),
        )
        plugin_code = render(spec)["src/graftpunk_myshop/plugin.py"]
        assert "keyword_search: bool | None = None" in plugin_code
        assert "recorded_date_range: str | None = None" in plugin_code
        assert "search_ocr_text: bool | None = None" in plugin_code
        assert '"keywordSearch": keyword_search,' in plugin_code
        assert '"recorded-date-range": recorded_date_range,' in plugin_code
        assert "keywordSearch:" not in plugin_code
        assert "recordedDateRange" not in plugin_code

    def test_a_camel_case_path_placeholder_is_declared_in_snake_case(self) -> None:
        spec = ScaffoldSpec(
            name="myshop",
            mode="new_project",
            backend="nodriver",
            base_url="https://myshop.example.com",
            digest=_digest(endpoints=(_CAMEL_PATH_ENDPOINT,)),
        )
        rendered = render(spec)
        plugin_code = rendered["src/graftpunk_myshop/plugin.py"]
        test_code = rendered["tests/test_plugin.py"]
        assert "search_result_id: str," in plugin_code
        assert 'f"/searchResults/{search_result_id}",' in plugin_code
        assert 'search_result_id="1"' in test_code

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

    def test_an_empty_base_url_carries_the_gp_fill_marker(self) -> None:
        """With neither --url nor --from-run the attribute renders empty: a grep for
        GP-FILL has to find the line the author must edit, not only the docstring."""
        spec = ScaffoldSpec(name="myshop", mode="new_project", backend="nodriver", base_url="")
        plugin_code = render(spec)["src/graftpunk_myshop/plugin.py"]
        assert '    base_url = ""  # GP-FILL: base URL' in plugin_code
        ast.parse(plugin_code)

    def test_a_known_base_url_carries_no_marker(self) -> None:
        spec = ScaffoldSpec(
            name="myshop",
            mode="new_project",
            backend="nodriver",
            base_url="https://myshop.example.com",
        )
        plugin_code = render(spec)["src/graftpunk_myshop/plugin.py"]
        assert '    base_url = "https://myshop.example.com"\n' in plugin_code

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

    def test_every_stub_declares_its_endpoint_on_a_line_of_its_own(self) -> None:
        spec = ScaffoldSpec(
            name="myshop",
            mode="new_project",
            backend="nodriver",
            base_url="https://myshop.example.com",
            digest=_digest(endpoints=(_ORDERS_ENDPOINT,)),
        )
        plugin_code = render(spec)["src/graftpunk_myshop/plugin.py"]
        assert '        endpoint="GET /orders/{order_id}",' in plugin_code.splitlines()
        tree = ast.parse(plugin_code)
        (stub,) = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]
        (decorator,) = stub.decorator_list
        assert isinstance(decorator, ast.Call)
        keywords = {k.arg: k.value for k in decorator.keywords}
        endpoint = keywords["endpoint"]
        assert isinstance(endpoint, ast.Constant) and endpoint.value == "GET /orders/{order_id}"

    def test_every_declared_endpoint_reads_back_through_parse_endpoint(self) -> None:
        """The declaration is written for tooling that reads it with parse_endpoint,
        so each one has to come back as the method and template the stub calls."""
        from graftpunk.har.naming import parse_endpoint

        endpoints = (
            _ORDERS_ENDPOINT,
            _SEARCH_ENDPOINT,
            _NOTES_ENDPOINT,
            _LONG_TEMPLATE_ENDPOINT,
            _LONG_TYPED_ENDPOINT,
        )
        spec = ScaffoldSpec(
            name="myshop",
            mode="new_project",
            backend="nodriver",
            base_url="https://myshop.example.com",
            digest=_digest(endpoints=endpoints),
        )
        tree = ast.parse(render(spec)["src/graftpunk_myshop/plugin.py"])
        declared = set()
        for stub in (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)):
            (decorator,) = stub.decorator_list
            assert isinstance(decorator, ast.Call)
            (endpoint,) = [k.value for k in decorator.keywords if k.arg == "endpoint"]
            assert isinstance(endpoint, ast.Constant) and isinstance(endpoint.value, str)
            declared.add(parse_endpoint(endpoint.value))
        assert declared == {(e.methods[0], e.template) for e in endpoints}

    def test_a_declaration_too_wide_for_its_line_wraps_and_reads_back_whole(self) -> None:
        spec = ScaffoldSpec(
            name="myshop",
            mode="new_project",
            backend="nodriver",
            base_url="https://myshop.example.com",
            digest=_digest(endpoints=(_LONG_TYPED_ENDPOINT,)),
        )
        plugin_code = render(spec)["src/graftpunk_myshop/plugin.py"]
        assert "        endpoint=(" in plugin_code.splitlines()
        assert all(len(line) <= GENERATED_LINE_LENGTH for line in plugin_code.splitlines())
        (stub,) = [n for n in ast.walk(ast.parse(plugin_code)) if isinstance(n, ast.FunctionDef)]
        (decorator,) = stub.decorator_list
        assert isinstance(decorator, ast.Call)
        (endpoint,) = [k.value for k in decorator.keywords if k.arg == "endpoint"]
        assert isinstance(endpoint, ast.Constant)
        assert endpoint.value == f"GET {_LONG_TYPED_ENDPOINT.template}"

    def test_the_placeholder_stub_declares_no_endpoint(self) -> None:
        """With no digest there is no observed endpoint, and a declaration of the
        placeholder path would be a claim tooling reads as true."""
        spec = ScaffoldSpec(
            name="myshop",
            mode="new_project",
            backend="nodriver",
            base_url="https://myshop.example.com",
        )
        tree = ast.parse(render(spec)["src/graftpunk_myshop/plugin.py"])
        (stub,) = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]
        (decorator,) = stub.decorator_list
        assert isinstance(decorator, ast.Call)
        assert "endpoint" not in {k.arg for k in decorator.keywords}

    def test_a_comment_above_the_request_ties_it_to_the_declaration(self) -> None:
        spec = ScaffoldSpec(
            name="myshop",
            mode="new_project",
            backend="nodriver",
            base_url="https://myshop.example.com",
            digest=_digest(endpoints=(_ORDERS_ENDPOINT,)),
        )
        lines = render(spec)["src/graftpunk_myshop/plugin.py"].splitlines()
        request = lines.index("        return ctx.request_json(")
        assert "endpoint=" in lines[request - 1] and "change both together" in lines[request - 1]

    def test_typed_parameters_get_explicit_param_specs(self) -> None:
        """#208: introspected options arrive as strings under the future import, so a
        stub with an int or bool parameter declares every parameter explicitly."""
        spec = ScaffoldSpec(
            name="myshop",
            mode="new_project",
            backend="nodriver",
            base_url="https://myshop.example.com",
            digest=_digest(endpoints=(_ORDERS_ENDPOINT,)),
        )
        plugin_code = render(spec)["src/graftpunk_myshop/plugin.py"]
        assert 'PluginParamSpec.option("order_id", required=True),' in plugin_code
        assert 'PluginParamSpec.option("page", type=int),' in plugin_code
        assert "PluginParamSpec" in plugin_code.split("class ")[0]

    @staticmethod
    def _registered_calls(
        monkeypatch: pytest.MonkeyPatch, endpoint: Endpoint, argv: list[str]
    ) -> tuple[Any, list[dict[str, Any]]]:
        """Render a plugin for *endpoint*, register it through the real CLI factory,
        invoke *argv*, and return the CLI result with the keyword arguments each
        request the handler made carried. The request itself is recorded, not sent:
        what is under test is what the CLI hands the handler."""
        from graftpunk.plugins import CommandContext
        from tests.unit.cli_harness import invoke_plugin_app

        calls: list[dict[str, Any]] = []

        def record(_ctx: Any, method: str, url: str, **kwargs: Any) -> dict:
            calls.append(kwargs)
            return {}

        monkeypatch.setattr(CommandContext, "request_json", record)
        spec = ScaffoldSpec(
            name="myshop",
            mode="new_project",
            backend="nodriver",
            base_url="https://myshop.example.com",
            digest=_digest(endpoints=(endpoint,)),
        )
        namespace: dict[str, Any] = {"__name__": "generated_plugin"}
        exec(render(spec)["src/graftpunk_myshop/plugin.py"], namespace)  # noqa: S102

        class _SessionlessPlugin(namespace["MyshopPlugin"]):
            requires_session = False

        return invoke_plugin_app(_SessionlessPlugin(), argv), calls

    def test_a_stub_with_a_bool_parameter_registers_and_passes_typed_values(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#208 compensation, end to end: the generated params= list has to survive
        the CLI factory's own checks (a bool option must be a flag), and the handler
        has to receive an int and a bool, not their text."""
        result, _ = self._registered_calls(
            monkeypatch, _SEARCH_ENDPOINT, ["myshop", "search", "--help"]
        )
        assert result.exit_code == 0, result.output
        for option in ("--include-meta", "--page", "--q"):
            assert option in strip_ansi(result.output)

        result, calls = self._registered_calls(
            monkeypatch, _SEARCH_ENDPOINT, ["myshop", "search", "--page", "2", "--include-meta"]
        )
        assert result.exit_code == 0, result.output
        (call,) = calls
        assert call["params"]["page"] == 2 and type(call["params"]["page"]) is int
        assert call["params"]["include_meta"] is True

        result, calls = self._registered_calls(monkeypatch, _SEARCH_ENDPOINT, ["myshop", "search"])
        assert result.exit_code == 0, result.output
        (call,) = calls
        assert call["params"]["include_meta"] is None
        assert call["params"]["page"] is None

    def test_a_stub_with_only_an_int_parameter_registers_and_passes_an_int(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        argv = ["myshop", "orders-by-order-id", "--order-id", "7", "--page", "3"]
        result, calls = self._registered_calls(monkeypatch, _ORDERS_ENDPOINT, argv)
        assert result.exit_code == 0, result.output
        (call,) = calls
        assert call["params"] == {"page": 3}
        assert type(call["params"]["page"]) is int

    def test_a_bool_parameter_is_a_flag_with_a_negative(self) -> None:
        """A bool the site recorded as true or false must be able to send either."""
        spec = ScaffoldSpec(
            name="myshop",
            mode="new_project",
            backend="nodriver",
            base_url="https://myshop.example.com",
            digest=_digest(endpoints=(_SEARCH_ENDPOINT,)),
        )
        plugin_code = render(spec)["src/graftpunk_myshop/plugin.py"]
        assert (
            'click_kwargs={"is_flag": True, "flag": "--include-meta/--no-include-meta"},'
            in plugin_code
        )

    @staticmethod
    def _wire_requests(
        monkeypatch: pytest.MonkeyPatch, endpoint: Endpoint, argv: list[str]
    ) -> tuple[Any, list[Any]]:
        """Render a plugin for *endpoint*, register it through the real CLI factory,
        invoke *argv*, and return the CLI result with every request as ``requests``
        prepared it for the wire: the real ``ctx.request_json`` runs, and only the
        socket is replaced."""
        import requests

        from tests.unit.cli_harness import invoke_plugin_app

        sent: list[Any] = []

        def send(_session: Any, prepared: Any, **_kwargs: Any) -> requests.Response:
            sent.append(prepared)
            response = requests.Response()
            response.status_code = 200
            response.headers["Content-Type"] = "application/json"
            response._content = b"{}"
            response.url = prepared.url
            response.request = prepared
            return response

        monkeypatch.setattr(requests.Session, "send", send)
        spec = ScaffoldSpec(
            name="myshop",
            mode="new_project",
            backend="nodriver",
            base_url="https://myshop.example.com",
            digest=_digest(endpoints=(endpoint,)),
        )
        namespace: dict[str, Any] = {"__name__": "generated_plugin"}
        exec(render(spec)["src/graftpunk_myshop/plugin.py"], namespace)  # noqa: S102

        class _SessionlessPlugin(namespace["MyshopPlugin"]):
            requires_session = False

        return invoke_plugin_app(_SessionlessPlugin(), argv), sent

    @pytest.mark.parametrize(
        ("flag", "expected_query"),
        [
            ((), "q=widget"),
            (("--include-meta",), "include_meta=true&q=widget"),
            (("--no-include-meta",), "include_meta=false&q=widget"),
        ],
    )
    def test_a_bool_query_parameter_sends_the_recorded_spelling_or_nothing(
        self, monkeypatch: pytest.MonkeyPatch, flag: tuple[str, ...], expected_query: str
    ) -> None:
        argv = ["myshop", "search", "--q", "widget", *flag]
        result, sent = self._wire_requests(monkeypatch, _SEARCH_ENDPOINT, argv)
        assert result.exit_code == 0, result.output
        (prepared,) = sent
        assert prepared.url == f"https://myshop.example.com/search?{expected_query}"

    @pytest.mark.parametrize(("flag", "expected"), [("--pinned", True), ("--no-pinned", False)])
    def test_a_bool_json_body_field_sends_a_json_boolean(
        self, monkeypatch: pytest.MonkeyPatch, flag: str, expected: bool
    ) -> None:
        argv = ["myshop", "orders-by-order-id-notes", "--order-id", "7", flag]
        result, sent = self._wire_requests(monkeypatch, _NOTES_ENDPOINT, argv)
        assert result.exit_code == 0, result.output
        (prepared,) = sent
        assert json.loads(prepared.body)["pinned"] is expected

    def test_a_json_body_carries_only_the_fields_given(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An option left off is left out of the body, as a query parameter is,
        rather than sent as null."""
        argv = ["myshop", "orders-by-order-id-notes", "--order-id", "7", "--body", "hi"]
        result, sent = self._wire_requests(monkeypatch, _NOTES_ENDPOINT, argv)
        assert result.exit_code == 0, result.output
        (prepared,) = sent
        assert prepared.url == "https://myshop.example.com/orders/7/notes"
        assert json.loads(prepared.body) == {"body": "hi"}

    def test_a_form_body_is_sent_as_form_data(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The digest recorded a form post, so the stub posts a form, not JSON."""
        argv = ["myshop", "newsletter-subscribe", "--email", "alice@example.com", "--weekly"]
        result, sent = self._wire_requests(monkeypatch, _FORM_POST_ENDPOINT, argv)
        assert result.exit_code == 0, result.output
        (prepared,) = sent
        assert prepared.headers["Content-Type"] == "application/x-www-form-urlencoded"
        assert prepared.body == "email=alice%40example.com&weekly=true"

    def test_a_form_body_carries_only_the_fields_given(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        argv = ["myshop", "newsletter-subscribe", "--no-weekly"]
        result, sent = self._wire_requests(monkeypatch, _FORM_POST_ENDPOINT, argv)
        assert result.exit_code == 0, result.output
        (prepared,) = sent
        assert prepared.body == "weekly=false"

    @staticmethod
    def _endpoint(
        template: str,
        method: str = "GET",
        *,
        query: dict[str, str] | None = None,
        body: dict[str, str] | None = None,
        body_kind: str = "none",
    ) -> Endpoint:
        return Endpoint(
            host="api.myshop.example.com",
            template=template,
            methods=(method,),
            count=1,
            statuses=(200,),
            content_type="application/json",
            query_params=query or {},
            body_params=body or {},
            body_kind=body_kind,  # type: ignore[arg-type]
            shape=ShapeNode(kind="object", children={}),
            custom_headers=(),
            examples=(template,),
        )

    @staticmethod
    def _plugin_code(endpoint: Endpoint) -> str:
        spec = ScaffoldSpec(
            name="myshop",
            mode="new_project",
            backend="nodriver",
            base_url="https://myshop.example.com",
            digest=_digest(endpoints=(endpoint,)),
        )
        return render(spec)["src/graftpunk_myshop/plugin.py"]

    @pytest.mark.parametrize(
        ("label", "reason"),
        [
            ("object", "a JSON object"),
            ("mixed", "values of more than one JSON type"),
            ("list[mixed]", "a JSON array whose elements have more than one type"),
            ("list[object]", "a JSON array of objects"),
            ("list[unknown]", "only empty JSON arrays"),
        ],
    )
    def test_a_json_field_no_option_can_send_is_not_declared(
        self, monkeypatch: pytest.MonkeyPatch, label: str, reason: str
    ) -> None:
        """G1: a value of another type is never sent; the stub names the field instead."""
        endpoint = self._endpoint(
            "/profile", "POST", body={"extra": label, "name": "str"}, body_kind="json"
        )
        plugin_code = self._plugin_code(endpoint)
        assert "extra:" not in plugin_code
        comment = " ".join(
            line.strip().lstrip("#").strip()
            for line in plugin_code.splitlines()
            if line.strip().startswith("#")
        )
        assert f'GP-FILL: body field "extra" is not an option: the recording sent {reason}' in (
            comment
        )
        result, sent = self._wire_requests(
            monkeypatch, endpoint, ["myshop", "profile", "--name", "alice"]
        )
        assert result.exit_code == 0, result.output
        (prepared,) = sent
        assert json.loads(prepared.body) == {"name": "alice"}

    def test_a_json_body_with_no_declarable_field_sends_no_body(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        endpoint = self._endpoint("/profile", "POST", body={"extra": "object"}, body_kind="json")
        result, sent = self._wire_requests(monkeypatch, endpoint, ["myshop", "profile"])
        assert result.exit_code == 0, result.output
        (prepared,) = sent
        assert prepared.body is None

    @pytest.mark.parametrize(
        ("argv", "expected_query"),
        [([], ""), (["--id", "1"], "?id=1"), (["--id", "1", "--id", "2"], "?id=1&id=2")],
    )
    def test_a_repeated_query_key_is_a_repeatable_option(
        self, monkeypatch: pytest.MonkeyPatch, argv: list[str], expected_query: str
    ) -> None:
        endpoint = self._endpoint("/orders", query={"id": "list[int]"})
        result, sent = self._wire_requests(monkeypatch, endpoint, ["myshop", "orders", *argv])
        assert result.exit_code == 0, result.output
        (prepared,) = sent
        assert prepared.url == f"https://myshop.example.com/orders{expected_query}"

    def test_a_repeated_query_key_option_is_typed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        endpoint = self._endpoint("/orders", query={"id": "list[int]"})
        result, sent = self._wire_requests(monkeypatch, endpoint, ["myshop", "orders", "--id", "x"])
        assert result.exit_code == 2
        assert sent == []

    def test_a_repeated_form_key_is_sent_as_repeated_keys(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        endpoint = self._endpoint("/tags", "POST", body={"tag": "list[str]"}, body_kind="form")
        argv = ["myshop", "tags", "--tag", "a", "--tag", "b"]
        result, sent = self._wire_requests(monkeypatch, endpoint, argv)
        assert result.exit_code == 0, result.output
        (prepared,) = sent
        assert prepared.body == "tag=a&tag=b"

    def test_a_json_array_is_sent_as_a_json_array(self, monkeypatch: pytest.MonkeyPatch) -> None:
        endpoint = self._endpoint("/batch", "POST", body={"ids": "list[int]"}, body_kind="json")
        argv = ["myshop", "batch", "--ids", "1", "--ids", "2"]
        result, sent = self._wire_requests(monkeypatch, endpoint, argv)
        assert result.exit_code == 0, result.output
        (prepared,) = sent
        assert prepared.body == b'{"ids": [1, 2]}'

    def test_a_json_float_field_sends_a_number(self, monkeypatch: pytest.MonkeyPatch) -> None:
        endpoint = self._endpoint("/pay", "POST", body={"amount": "float"}, body_kind="json")
        argv = ["myshop", "pay", "--amount", "3.5"]
        result, sent = self._wire_requests(monkeypatch, endpoint, argv)
        assert result.exit_code == 0, result.output
        (prepared,) = sent
        assert prepared.body == b'{"amount": 3.5}'

    def test_a_float_body_field_is_a_float_option(self) -> None:
        spec = ScaffoldSpec(
            name="myshop",
            mode="new_project",
            backend="nodriver",
            base_url="https://myshop.example.com",
            digest=_digest(endpoints=(_PAYMENT_ENDPOINT,)),
        )
        plugin_code = render(spec)["src/graftpunk_myshop/plugin.py"]
        assert 'PluginParamSpec.option("amount", type=float),' in plugin_code
        assert "amount: float | None = None," in plugin_code

    def test_a_float_option_reaches_the_handler_as_a_float(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        argv = ["myshop", "payments", "--amount", "12.5"]
        result, calls = self._registered_calls(monkeypatch, _PAYMENT_ENDPOINT, argv)
        assert result.exit_code == 0, result.output
        (call,) = calls
        assert call["json"]["amount"] == 12.5 and type(call["json"]["amount"]) is float

    def test_a_leading_zero_query_value_is_a_str_option_end_to_end(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Digest to stub: a postal code 07030 stays text, so the command sends
        it as typed instead of refusing it or dropping the zero."""
        har = tmp_path / "network.har"
        entry = {
            "startedDateTime": "2026-09-10T10:00:00.000Z",
            "time": 5,
            "request": {
                "method": "GET",
                "url": "https://api.myshop.example.com/stores?zip=07030",
                "headers": [],
                "cookies": [],
                "queryString": [],
            },
            "response": {
                "status": 200,
                "statusText": "OK",
                "headers": [{"name": "Content-Type", "value": "application/json"}],
                "cookies": [],
                "content": {"mimeType": "application/json", "text": "{}", "size": 2},
            },
        }
        har.write_text(json.dumps({"log": {"version": "1.2", "entries": [entry]}}))
        (endpoint,) = digest(DigestSource.from_har(har)).endpoints
        result, calls = self._registered_calls(
            monkeypatch, endpoint, ["myshop", "stores", "--zip", "07030"]
        )
        assert result.exit_code == 0, result.output
        (call,) = calls
        assert call["params"] == {"zip": "07030"}

    def test_an_untyped_stub_carries_no_params_list(self) -> None:
        spec = ScaffoldSpec(
            name="myshop",
            mode="new_project",
            backend="nodriver",
            base_url="https://myshop.example.com",
            digest=_digest(endpoints=(_CAMEL_PATH_ENDPOINT,)),
        )
        plugin_code = render(spec)["src/graftpunk_myshop/plugin.py"]
        assert "params=[" not in plugin_code
        assert "PluginParamSpec" not in plugin_code

    def test_a_long_plugins_import_is_exploded(self) -> None:
        spec = ScaffoldSpec(
            name="myshop",
            mode="new_project",
            backend="nodriver",
            base_url="https://myshop.example.com",
            digest=_digest(endpoints=(_ORDERS_ENDPOINT,), login_forms=(_PASSWORD_LOGIN_FORM,)),
        )
        plugin_code = render(spec)["src/graftpunk_myshop/plugin.py"]
        assert "from graftpunk.plugins import (" in plugin_code
        assert all(len(line) <= GENERATED_LINE_LENGTH for line in plugin_code.splitlines())


_LOGIN_PAGE_ENDPOINT = Endpoint(
    host="api.myshop.example.com",
    template="/login",
    methods=("GET",),
    count=1,
    statuses=(200,),
    content_type="text/html",
    query_params={},
    body_params={},
    body_kind="none",
    shape=None,
    custom_headers=(),
    examples=("/login",),
)

_CREDENTIAL_POST_ENDPOINT = Endpoint(
    host="api.myshop.example.com",
    template="/login",
    methods=("POST",),
    count=1,
    statuses=(302,),
    content_type="text/html",
    query_params={},
    body_params={"username": "str", "password": "str"},
    body_kind="form",
    shape=None,
    custom_headers=(),
    examples=("/login",),
)

_FORM_PAGE_OBSERVATION = LoginObservation(
    order=1,
    method="GET",
    url="https://api.myshop.example.com/login",
    status=200,
    kind="form_page",
    fields=(),
)

_CREDENTIAL_POST_OBSERVATION = LoginObservation(
    order=2,
    method="POST",
    url="https://api.myshop.example.com/login",
    status=302,
    kind="credential_post",
    fields=("password", "username"),
)

_DASHBOARD_REDIRECT_OBSERVATION = LoginObservation(
    order=3,
    method="GET",
    url="https://app.myshop.example.com/auth/callback",
    status=302,
    kind="redirect",
    fields=(),
    redirect_to="/dashboard",
)

# The common shape: the credential post itself answers 302, so the digest records
# no separate redirect observation and the post carries the landing path.
_REDIRECTING_CREDENTIAL_POST = LoginObservation(
    order=2,
    method="POST",
    url="https://api.myshop.example.com/login",
    status=302,
    kind="credential_post",
    fields=("password", "username"),
    redirect_to="/dashboard",
)


# /account/login is one member of a family the digest collapsed, so the
# endpoint's final template no longer spells the login path.
_COLLAPSED_ACCOUNT_FAMILY = Endpoint(
    host="api.myshop.example.com",
    template="/account/{account_id}",
    methods=("GET",),
    count=11,
    statuses=(200,),
    content_type="text/html",
    query_params={},
    body_params={},
    body_kind="none",
    shape=None,
    custom_headers=(),
    examples=("/account/login", "/account/2024-07", "/account/2024-08"),
)

_COLLAPSED_FORM_PAGE_OBSERVATION = LoginObservation(
    order=1,
    method="GET",
    url="https://api.myshop.example.com/account/login",
    status=200,
    kind="form_page",
    fields=(),
)


def _owned(endpoint: Endpoint) -> Endpoint:
    """*endpoint* as the digest hands it over when the login flow owns it."""
    return dataclasses.replace(endpoint, login_flow=True)


class TestLoginFlowEndpointsAreNotCommandStubs:
    """login_config owns the login form's GET and the credential POST. Rendered
    as stubs they were wrong for the developer and their generated tests could
    only fail."""

    @staticmethod
    def _spec(*endpoints: Endpoint) -> ScaffoldSpec:
        return ScaffoldSpec(
            name="myshop",
            mode="new_project",
            backend="nodriver",
            base_url="https://myshop.example.com",
            digest=_digest(
                endpoints=endpoints,
                login_forms=(_PASSWORD_LOGIN_FORM,),
                login=(_FORM_PAGE_OBSERVATION, _CREDENTIAL_POST_OBSERVATION),
            ),
        )

    def test_no_login_stub_beside_a_real_endpoint(self) -> None:
        files = render(
            self._spec(
                _owned(_LOGIN_PAGE_ENDPOINT), _owned(_CREDENTIAL_POST_ENDPOINT), _ORDERS_ENDPOINT
            )
        )
        plugin_code = files["src/graftpunk_myshop/plugin.py"]
        assert plugin_code.count("@command(") == 1
        assert "def login(" not in plugin_code
        assert "def login_2(" not in plugin_code
        assert "login_config = LoginConfig(" in plugin_code
        ast.parse(plugin_code)
        test_code = files["tests/test_plugin.py"]
        assert "def test_login(" not in test_code
        ast.parse(test_code)

    def test_a_run_with_nothing_but_the_login_flow_falls_back_to_the_gp_fill_stub(self) -> None:
        files = render(self._spec(_owned(_LOGIN_PAGE_ENDPOINT), _owned(_CREDENTIAL_POST_ENDPOINT)))
        plugin_code = files["src/graftpunk_myshop/plugin.py"]
        assert plugin_code.count("@command(") == 1
        assert "def example(self, ctx: CommandContext)" in plugin_code
        ast.parse(plugin_code)
        test_code = files["tests/test_plugin.py"]
        assert "fixture_context" not in test_code
        ast.parse(test_code)

    def test_an_endpoint_that_is_not_part_of_the_login_flow_keeps_its_stub(self) -> None:
        """The same path under another method is a different endpoint, and the
        digest leaves it unflagged (test_har_digest.py holds that rule)."""
        other_method = dataclasses.replace(_LOGIN_PAGE_ENDPOINT, methods=("DELETE",))
        files = render(self._spec(other_method))
        plugin_code = files["src/graftpunk_myshop/plugin.py"]
        assert "def login(" in plugin_code
        ast.parse(plugin_code)

    def test_a_login_path_inside_a_collapsed_family_is_still_owned(self) -> None:
        """The digest's high-cardinality collapse can re-template the endpoint
        the login observation belongs to; the digest
        flags it (test_har_digest.py holds that rule), and neither the stub nor
        its generated test is rendered."""
        spec = ScaffoldSpec(
            name="myshop",
            mode="new_project",
            backend="nodriver",
            base_url="https://myshop.example.com",
            digest=_digest(
                endpoints=(_owned(_COLLAPSED_ACCOUNT_FAMILY), _ORDERS_ENDPOINT),
                login_forms=(_PASSWORD_LOGIN_FORM,),
                login=(_COLLAPSED_FORM_PAGE_OBSERVATION,),
            ),
        )
        files = render(spec)
        plugin_code = files["src/graftpunk_myshop/plugin.py"]
        assert "def account_by_account_id(" not in plugin_code
        assert plugin_code.count("@command(") == 1
        ast.parse(plugin_code)
        test_code = files["tests/test_plugin.py"]
        assert "def test_account_by_account_id(" not in test_code
        ast.parse(test_code)

    def test_the_generator_reads_the_digest_flag(self) -> None:
        """With no login observation at all, an endpoint the digest flagged is still
        skipped: the generator reads the flag and never recomputes it."""
        flagged = dataclasses.replace(_ORDERS_ENDPOINT, login_flow=True)
        spec = ScaffoldSpec(
            name="myshop",
            mode="new_project",
            backend="nodriver",
            base_url="https://myshop.example.com",
            digest=_digest(endpoints=(flagged, _SEARCH_ENDPOINT)),
        )
        plugin_code = render(spec)["src/graftpunk_myshop/plugin.py"]
        assert "def orders_by_order_id(" not in plugin_code
        assert "def search(" in plugin_code


class TestUnavailableShapeIsOmittedFromTheDocstring:
    def test_no_shape_line_when_the_shape_could_not_be_read(self) -> None:
        """An unavailable shape is not a fact about the site, and the docstring
        used to claim `Shape: non-JSON.` for every body over the threshold."""
        endpoint = Endpoint(
            host="api.myshop.example.com",
            template="/orders",
            methods=("GET",),
            count=1,
            statuses=(200,),
            content_type="application/json",
            query_params={},
            body_params={},
            body_kind="none",
            shape=SHAPE_UNAVAILABLE,
            custom_headers=(),
            examples=(),
        )
        spec = ScaffoldSpec(
            name="myshop",
            mode="new_project",
            backend="nodriver",
            base_url="https://myshop.example.com",
            digest=_digest(endpoints=(endpoint,)),
        )
        plugin_code = render(spec)["src/graftpunk_myshop/plugin.py"]
        assert "Shape:" not in plugin_code
        assert "return ctx.request_json(" in plugin_code
        ast.parse(plugin_code)

    def test_a_known_shape_still_carries_the_line(self) -> None:
        spec = ScaffoldSpec(
            name="myshop",
            mode="new_project",
            backend="nodriver",
            base_url="https://myshop.example.com",
            digest=_digest(endpoints=(_ORDERS_ENDPOINT,)),
        )
        plugin_code = render(spec)["src/graftpunk_myshop/plugin.py"]
        assert "Shape: object{id}." in plugin_code


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

    def test_a_template_with_a_stray_brace_still_parses(self) -> None:
        # "{year-2024}" is not a placeholder (a hyphen cannot appear in one), so the
        # braces are literal text inside an f-string and must be doubled.
        endpoint = Endpoint(
            host="api.myshop.example.com",
            template="/reports/{year-2024}/orders/{order_id}",
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
        spec = ScaffoldSpec(
            name="myshop",
            mode="new_project",
            backend="nodriver",
            base_url="https://myshop.example.com",
            digest=_digest(endpoints=(endpoint,)),
        )
        plugin_code = render(spec)["src/graftpunk_myshop/plugin.py"]
        assert 'f"/reports/{{year-2024}}/orders/{order_id}",' in plugin_code
        ast.parse(plugin_code)

    def test_a_template_repeating_a_placeholder_has_no_duplicate_arguments(self) -> None:
        # /orders/1/orders/2 templates to the same placeholder name twice, and a
        # `def` with two arguments of one name is a SyntaxError.
        endpoint = Endpoint(
            host="api.myshop.example.com",
            template="/orders/{order_id}/orders/{order_id}",
            methods=("GET",),
            count=1,
            statuses=(200,),
            content_type="application/json",
            query_params={"order_id": "str"},
            body_params={},
            body_kind="none",
            shape=ShapeNode(kind="object", children={}),
            custom_headers=(),
            examples=(),
        )
        spec = ScaffoldSpec(
            name="myshop",
            mode="new_project",
            backend="nodriver",
            base_url="https://myshop.example.com",
            digest=_digest(endpoints=(endpoint,)),
        )
        plugin_code = render(spec)["src/graftpunk_myshop/plugin.py"]
        module = ast.parse(plugin_code)
        stubs = [
            node
            for node in ast.walk(module)
            if isinstance(node, ast.FunctionDef) and node.name != "__init__"
        ]
        assert stubs, "the digest has an endpoint, so the module must carry a stub"
        for stub in stubs:
            names = [
                argument.arg
                for argument in (*stub.args.posonlyargs, *stub.args.args, *stub.args.kwonlyargs)
            ]
            assert len(names) == len(set(names)), f"{stub.name} declares {names}"


class TestRenderedTreeIsRuffClean:
    """The generated project is a real ruff target: its own pyproject.toml declares
    the config, so running ruff against the written-out tree is the actual gate a
    freshly scaffolded plugin's own CI would run (validation Important 2, 2026-09-12).
    """

    def _assert_within_the_generated_width(self, files: dict[str, str]) -> None:
        """Every line of every generated Python file fits the width the generated
        project's own pyproject.toml declares. The property the per-shape wrapping and
        the identifier caps exist to hold, asserted for every tree this class renders
        rather than one patched site at a time (validation fix round 4, 2026-09-12)."""
        for relative_path, content in sorted(files.items()):
            if not relative_path.endswith(".py"):
                continue
            for number, line in enumerate(content.splitlines(), start=1):
                assert len(line) <= GENERATED_LINE_LENGTH, (
                    f"{relative_path}:{number} is {len(line)} characters: {line!r}"
                )

    def _write_tree(self, root: Path, files: dict[str, str]) -> Path:
        self._assert_within_the_generated_width(files)
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
        digest = _digest(endpoints=(_SEARCH_ENDPOINT, _NOTES_ENDPOINT, _FORM_POST_ENDPOINT))
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
        files = render(spec)
        assert "Token.from_" in files["src/graftpunk_myshop/plugin.py"]
        tree = self._write_tree(tmp_path / "login_token", files)
        self._assert_tree_is_clean(tree)

    def test_long_redirect_path_project(self, tmp_path: Path) -> None:
        # A redirect path past the generated width: success_url is a captured site
        # fact like the selectors, so it wraps the same way rather than overflowing.
        long_redirect = LoginObservation(
            order=3,
            method="GET",
            url="https://app.myshop.example.com/auth/callback",
            status=302,
            kind="redirect",
            fields=(),
            redirect_to="/" + "/".join(["account-overview"] * 6),
        )
        spec = ScaffoldSpec(
            name="myshop",
            mode="new_project",
            backend="nodriver",
            base_url="https://myshop.example.com",
            digest=_digest(
                login_forms=(_PASSWORD_LOGIN_FORM,),
                login=(_CREDENTIAL_POST_OBSERVATION, long_redirect),
            ),
        )
        files = render(spec)
        assert "success_url=" in files["src/graftpunk_myshop/plugin.py"]
        tree = self._write_tree(tmp_path / "long_redirect", files)
        self._assert_tree_is_clean(tree)

    def test_long_typed_parameters_and_template_project(self, tmp_path: Path) -> None:
        # params= entries too wide for one line explode one argument per line, and
        # a wrapped endpoint= declaration: both have to be the shape ruff format
        # would give them.
        spec = ScaffoldSpec(
            name="myshop",
            mode="new_project",
            backend="nodriver",
            base_url="https://myshop.example.com",
            digest=_digest(endpoints=(_LONG_TYPED_ENDPOINT, _SEARCH_ENDPOINT)),
        )
        files = render(spec)
        plugin_lines = files["src/graftpunk_myshop/plugin.py"].splitlines()
        assert "            PluginParamSpec.option(" in plugin_lines
        assert "        endpoint=(" in plugin_lines
        tree = self._write_tree(tmp_path / "long_typed", files)
        self._assert_tree_is_clean(tree)

    def test_long_selectors_and_header_name_project(self, tmp_path: Path) -> None:
        # A username selector well past the generated width, a submit selector
        # long enough to be typical but still short enough to fit on one line,
        # and a header name with no whitespace at all: the three shapes
        # literal_lines must handle.
        long_selector = (
            "#login-form div.field-wrapper.username-wrapper > label + "
            "input[name='username'][type='text'].form-control.input-lg"
        )
        submit_selector = "#login-form button.btn.btn-primary.submit-button[type='submit']"
        long_header_name = "X-" + "A" * 88
        # A credential role is the form input's own name when the input is neither
        # the username nor the password field (har.documents._guess_role), so the
        # fields dict has a captured site fact on both sides of the colon.
        long_role = "customer_" + "r" * 80
        form = LoginForm(
            action="/login",
            method="POST",
            fields={"username": long_selector, "password": "#pw", long_role: long_selector},
            submit=submit_selector,
            hidden=(),
            source="page-source.html",
        )
        header = TokenCandidate(kind="header", name=long_header_name, seen_on=("GET /dashboard",))
        cookie = TokenCandidate(kind="cookie", name=long_header_name, seen_on=("GET /dashboard",))
        digest = _digest(login_forms=(form,), tokens=(header, cookie))
        spec = ScaffoldSpec(
            name="myshop",
            mode="new_project",
            backend="nodriver",
            base_url="https://myshop.example.com",
            digest=digest,
        )
        files = render(spec)
        plugin_code = files["src/graftpunk_myshop/plugin.py"]
        assert "Token.from_" in plugin_code
        assert "LoginStep(" in plugin_code
        tree = self._write_tree(tmp_path / "long_selectors", files)
        self._assert_tree_is_clean(tree)

    def test_long_observation_url_and_unpaired_token_project(self, tmp_path: Path) -> None:
        # No password form (so the "no form found" observation-comment branch
        # renders), a long observation URL (a path, no query), an unpaired
        # token candidate with a long name, and a long base_url (which the
        # class docstring, and the base_url attribute itself, both
        # interpolate): the shapes the round-3 re-review reproduced its
        # failures with (validation fix round 3, 2026-09-12).
        long_path = "/".join(f"segment-{i}" for i in range(12))
        long_url = f"https://api.myshop.example.com/{long_path}"
        assert 140 <= len(long_url) <= 180
        observation = LoginObservation(
            order=1, method="GET", url=long_url, status=200, kind="form_page", fields=()
        )
        long_token_name = "X-" + "B" * 88
        assert len(long_token_name) == 90
        lone = TokenCandidate(kind="header", name=long_token_name, seen_on=("GET /dashboard",))
        digest = _digest(login=(observation,), tokens=(lone,))
        long_base_url = "https://" + "x" * 100 + ".example.com"
        assert 110 <= len(long_base_url) <= 130
        spec = ScaffoldSpec(
            name="myshop",
            mode="new_project",
            backend="nodriver",
            base_url=long_base_url,
            digest=digest,
        )
        files = render(spec)
        plugin_code = files["src/graftpunk_myshop/plugin.py"]
        assert "1." in plugin_code
        assert "unpaired token candidate" in plugin_code
        tree = self._write_tree(tmp_path / "long_observation", files)
        self._assert_tree_is_clean(tree)

    def test_selectors_and_names_carrying_their_own_quotes_project(self, tmp_path: Path) -> None:
        # har.documents._selector_for builds `form[action="..."] input[name="..."]`
        # for every login input without an id, which is the common case, and a query
        # parameter or header name can carry a quote of its own: embedded raw, each
        # ends its string literal early and the generated module does not parse.
        quoted_selector = 'form[action="/login"] input[name="username"]'
        both_quotes_selector = 'form[action="/login"] input[name=\'pw\'][data-x="1"]'
        form = LoginForm(
            action="/login",
            method="POST",
            fields={"username": quoted_selector, "password": both_quotes_selector},
            submit='form[action="/login"] button[type="submit"]',
            hidden=(),
            source="page-source.html",
        )
        endpoint = Endpoint(
            host="api.myshop.example.com",
            template='/orders/{order_id}/notes-"quoted"-segment',
            methods=("GET",),
            count=1,
            statuses=(200,),
            content_type="application/json",
            query_params={'filter["name"]': "str"},
            body_params={},
            body_kind="none",
            shape=ShapeNode(kind="object", children={}),
            custom_headers=('X-Shop-"Client"',),
            examples=(),
        )
        spec = ScaffoldSpec(
            name="myshop",
            mode="new_project",
            backend="nodriver",
            base_url="https://myshop.example.com",
            digest=_digest(endpoints=(endpoint,), login_forms=(form,)),
        )
        files = render(spec)
        plugin_code = files["src/graftpunk_myshop/plugin.py"]
        ast.parse(plugin_code)
        assert f"\"username\": '{quoted_selector}'," in plugin_code
        tree = self._write_tree(tmp_path / "quoted", files)
        self._assert_tree_is_clean(tree)

    def test_docstring_text_carrying_backslashes_and_triple_quotes_project(
        self, tmp_path: Path
    ) -> None:
        # base_url reaches the class docstring and a captured JSON key reaches the
        # stub's shape line, both verbatim: a backslash there starts an escape
        # sequence nobody wrote and a triple quote ends the docstring early (final
        # fix wave, 2026-09-12). The long key also forces the wrap, so the repair
        # of an escape cut in half by word-wrapping is exercised, not just skipped.
        base_url = 'https://myshop.example.com/a\\b/"""c\\'
        long_key = "k\\" + "x" * 120 + '"""y'
        endpoint = Endpoint(
            host="api.myshop.example.com",
            template="/orders/{order_id}",
            methods=("GET",),
            count=1,
            statuses=(200,),
            content_type="application/json",
            query_params={},
            body_params={},
            body_kind="none",
            shape=ShapeNode(
                kind="object",
                children={long_key: ShapeNode(kind="string"), 'q\\"""': ShapeNode(kind="number")},
            ),
            custom_headers=(),
            examples=(),
        )
        spec = ScaffoldSpec(
            name="myshop",
            mode="new_project",
            backend="nodriver",
            base_url=base_url,
            digest=_digest(endpoints=(endpoint,)),
        )
        files = render(spec)
        for relative_path, content in sorted(files.items()):
            if relative_path.endswith(".py"):
                with warnings.catch_warnings():
                    # An invalid escape sequence in a generated docstring is a
                    # SyntaxWarning, not a SyntaxError, so ast.parse alone would
                    # pass the very bug this test covers.
                    warnings.simplefilter("error", SyntaxWarning)
                    ast.parse(content, filename=relative_path)
        plugin_code = files["src/graftpunk_myshop/plugin.py"]
        assert base_url in _class_docstring(plugin_code, "MyshopPlugin"), (
            "the class docstring must still read back as the base_url it quotes"
        )
        tree = self._write_tree(tmp_path / "escaped_docstrings", files)
        self._assert_tree_is_clean(tree)

    def test_maximal_name_deep_paths_and_wide_parameter_names_project(self, tmp_path: Path) -> None:
        # Every width-relevant input at once, each at or past the bar the round-4
        # audit found: the longest name validate_plugin_name accepts (so the class,
        # module, env prefix, import line and assert are all at their maximum), a
        # deeply nested template, a template past the width on its own with two
        # placeholders, parameter names longer than a dict entry can hold, a header
        # name longer still, and a base_url of 120 characters on a digest that does
        # have endpoints (so the generated test's fixture_context call carries it).
        name = "a" + "b" * (_MAX_PLUGIN_NAME - 1)
        assert len(name) == _MAX_PLUGIN_NAME
        deep_template = (
            "/api/v2/customer-accounts/{account_id}/payment-methods/default-billing-address"
        )
        assert len(deep_template) == 78
        wide_template = (
            "/api/v2/customer-accounts/{account_id}/payment-methods/{payment_method_id}"
            "/scheduled-deliveries/recurring-orders/preferences-list/default-billing-addresses"
        )
        assert len(wide_template) == 155
        query_41 = "include_related_objects_and_metadata_flag"
        query_60 = "include_related_objects_and_metadata_and_pricing_breakdown_x"
        body_60 = "delivery_instructions_for_the_courier_at_the_loading_dock_xy"
        header_75 = "X-Myshop-Client-Request-Correlation-Identifier-For-Downstream-Traced-Header"
        assert (len(query_41), len(query_60), len(body_60), len(header_75)) == (41, 60, 60, 75)
        deep = Endpoint(
            host="api.myshop.example.com",
            template=deep_template,
            methods=("GET",),
            count=4,
            statuses=(200,),
            content_type="application/json",
            query_params={query_41: "str", query_60: "int"},
            body_params={},
            body_kind="none",
            shape=ShapeNode(kind="object", children={"id": ShapeNode(kind="number")}),
            custom_headers=(header_75,),
            examples=(),
        )
        wide = Endpoint(
            host="api.myshop.example.com",
            template=wide_template,
            methods=("POST",),
            count=2,
            statuses=(201,),
            content_type="application/json",
            query_params={},
            body_params={body_60: "str"},
            body_kind="json",
            shape=ShapeNode(kind="object", children={"id": ShapeNode(kind="number")}),
            custom_headers=(),
            examples=(),
        )
        base_url = "https://" + "x" * 100 + ".example.com"
        assert len(base_url) == 120
        spec = ScaffoldSpec(
            name=name,
            mode="new_project",
            backend="nodriver",
            base_url=base_url,
            digest=_digest(endpoints=(deep, wide)),
        )
        files = render(spec)
        plugin_code = files[f"src/graftpunk_{name}/plugin.py"]
        # Non-vacuous, and each assertion names which guard fired: both stubs are
        # present; the 78-character template still fits one line; the 155-character
        # one split at a "/" boundary; the 75-character header name became a
        # parenthesised dict key; the 41-character query parameter became a
        # truncated identifier.
        assert plugin_code.count("@command(") == 2
        assert f'f"{deep_template}",' in plugin_code
        assert 'f"/api/v2/customer-accounts/{account_id}/payment-methods' in plugin_code
        assert 'f"/scheduled-deliveries' in plugin_code
        assert f'"{header_75}": "GP-FILL",' not in plugin_code
        assert '): "GP-FILL",' in plugin_code
        assert f"{query_41[:_MAX_PARAM_NAME]}: str | None = None," in plugin_code
        test_code = files["tests/test_plugin.py"]
        assert "ctx = fixture_context(" in test_code
        assert f'base_url="{base_url}"' not in test_code  # the long base_url was split
        assert '        account_id="1",' in test_code  # the wide stub's call exploded
        tree = self._write_tree(tmp_path / "maximal", files)
        self._assert_tree_is_clean(tree)


class TestFixturesDirFollowsThePolicy:
    """The generated test module's FIXTURES_DIR is the policy's root for that plugin."""

    @staticmethod
    def _fixtures_dir(test_module: str, project: Path, test_file: str) -> Path:
        for node in ast.parse(test_module).body:
            if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "FIXTURES_DIR" for t in node.targets
            ):
                expression = ast.unparse(node.value)
                return eval(  # noqa: S307 - evaluates the generator's own Path expression
                    expression, {"Path": Path, "__file__": str(project / test_file)}
                )
        raise AssertionError("no FIXTURES_DIR in the generated test module")

    def test_a_new_project(self, tmp_path: Path) -> None:
        from graftpunk.devtools.scaffold.policy import fixtures_root

        spec = ScaffoldSpec(
            name="myshop", mode="new_project", backend="nodriver", base_url="https://myshop.example"
        )
        files = render(spec)
        found = self._fixtures_dir(files["tests/test_plugin.py"], tmp_path, "tests/test_plugin.py")
        expected = fixtures_root(suite_member=False, module_name="myshop")
        assert found == tmp_path / expected.rstrip("/")

    def test_a_suite_member(self, tmp_path: Path) -> None:
        from graftpunk.devtools.scaffold.policy import fixtures_root

        spec = ScaffoldSpec(
            name="my-shop",
            mode="add_to_suite",
            backend="nodriver",
            base_url="https://myshop.example",
        )
        files = render(spec)
        found = self._fixtures_dir(
            files["tests/test_my_shop.py"], tmp_path, "tests/test_my_shop.py"
        )
        expected = fixtures_root(suite_member=True, module_name="my_shop")
        assert found == tmp_path / expected.rstrip("/")
        assert f"{expected}.gitkeep" in files
