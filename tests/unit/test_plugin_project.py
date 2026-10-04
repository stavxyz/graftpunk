"""The one reader of a plugin project and its structural view (graft skill spec,
2026-09-21, "One owner for what plugin project is this directory")."""

from __future__ import annotations

import ast
import os
import shutil
import subprocess
import sys
from dataclasses import fields
from pathlib import Path
from types import MappingProxyType

import pytest

from graftpunk.devtools.plugin_project import (
    CommandView,
    NotAPluginProjectError,
    PluginDefect,
    PluginProjectError,
    PluginView,
    ProjectView,
    Span,
    classify,
    read_project,
    require_plugin_project,
)
from graftpunk.devtools.scaffold import policy
from graftpunk.devtools.scaffold.policy import (
    PROJECT_REQUIREMENTS,
    ProjectRequirement,
    fixtures_root,
    module_name_for,
)
from graftpunk.devtools.scaffold.project import write_scaffold
from graftpunk.devtools.scaffold.pysrc import with_import
from graftpunk.devtools.scaffold.render import ScaffoldSpec, fixtures_root_for
from graftpunk.har.digest import DigestSource, Endpoint, RunDigest, ShapeNode


def _endpoint(template: str) -> Endpoint:
    return Endpoint(
        host="myshop.example",
        template=template,
        methods=("GET",),
        count=1,
        statuses=(200,),
        content_type="application/json",
        query_params={},
        body_params={},
        body_kind="none",
        shape=ShapeNode(kind="object", children={"id": ShapeNode(kind="number")}),
        custom_headers=(),
        examples=(),
    )


def _digest(*templates: str) -> RunDigest:
    return RunDigest(
        source=DigestSource(har_path=Path("network.har"), session="myshop", run_id="run-1"),
        primary_host="myshop.example",
        hosts={"myshop.example": 1},
        endpoints=tuple(_endpoint(t) for t in templates),
        login=(),
        login_forms=(),
        tokens=(),
        cookies=(),
        dropped={"static": 0, "third_party": 0, "error": 0, "other_scheme": 0},
    )


def _generate(root: Path, name: str = "myshop", *templates: str) -> None:
    write_scaffold(
        root,
        ScaffoldSpec(
            name=name,
            mode="new_project",
            backend="nodriver",
            base_url="https://myshop.example",
            digest=_digest(*(templates or ("/api/orders", "/api/orders/{order_id}"))),
        ),
    )


_HAND_WRITTEN_PYPROJECT = """\
[project]
name = "graftpunk-myshop"

[project.entry-points."graftpunk.plugins"]
myshop = "graftpunk_myshop.plugin:MyshopPlugin"
"""

_HAND_WRITTEN_MODULE = """\
from graftpunk.plugins import CommandContext, SitePlugin, command


class MyshopPlugin(SitePlugin):
    site_name = "myshop"
    base_url = "https://myshop.example"

    @command(help="List orders")
    def orders(self, ctx: CommandContext) -> dict:
        return ctx.request_json("GET", "/api/orders", role="xhr")
"""


def _hand_written(root: Path, module: str = _HAND_WRITTEN_MODULE) -> Path:
    (root / "pyproject.toml").write_text(_HAND_WRITTEN_PYPROJECT)
    package = root / "src" / "graftpunk_myshop"
    package.mkdir(parents=True)
    (package / "plugin.py").write_text(module)
    return package / "plugin.py"


class TestClassify:
    def test_no_pyproject_is_empty(self, tmp_path: Path) -> None:
        (tmp_path / "notes.txt").write_text("not a project")
        assert classify(tmp_path) == "empty"
        assert read_project(tmp_path) == ProjectView(
            directory="empty",
            plugins=(),
            defects=(),
            requirements={},
            requirement_set=PROJECT_REQUIREMENTS,
            test_markers=(),
            fixtures_tree_present=False,
        )

    def test_a_pyproject_with_the_group_is_a_plugin(self, tmp_path: Path) -> None:
        _hand_written(tmp_path)
        assert classify(tmp_path) == "plugin"

    def test_a_pyproject_without_the_group_is_foreign(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "other"\n')
        assert classify(tmp_path) == "foreign"
        assert read_project(tmp_path).plugins == ()

    def test_a_pyproject_that_is_a_directory_is_refused_not_read_as_empty(
        self, tmp_path: Path
    ) -> None:
        """A wrong pyproject.toml path must not read as "empty" (create mode),
        the same rule a missing --dir gets."""
        (tmp_path / "pyproject.toml").mkdir()
        with pytest.raises(PluginProjectError, match="exists but is not a regular file"):
            classify(tmp_path)
        with pytest.raises(PluginProjectError, match="exists but is not a regular file"):
            read_project(tmp_path)

    def test_a_dangling_pyproject_symlink_is_refused_not_read_as_empty(
        self, tmp_path: Path
    ) -> None:
        """A dangling symlink `lexists` but not `exists`: reading "empty" from
        it would let gp plugin new overwrite the link instead of refusing."""
        (tmp_path / "pyproject.toml").symlink_to(tmp_path / "nonexistent.toml")
        with pytest.raises(PluginProjectError, match="exists but is not a regular file"):
            classify(tmp_path)
        with pytest.raises(PluginProjectError, match="exists but is not a regular file"):
            read_project(tmp_path)


class TestReadProjectRefusesABadDirectory:
    """A mistyped --dir reads as "empty" (create mode) otherwise, instead of a
    refusal; every one of info/check/add-command/upgrade routes through
    read_project or require_plugin_project, so the refusal belongs here."""

    def test_a_directory_that_does_not_exist_is_refused(self, tmp_path: Path) -> None:
        missing = tmp_path / "nonexistent"
        with pytest.raises(PluginProjectError, match="no such directory"):
            read_project(missing)

    def test_a_path_that_is_a_regular_file_is_refused(self, tmp_path: Path) -> None:
        not_a_dir = tmp_path / "plain_file"
        not_a_dir.write_text("not a directory")
        with pytest.raises(PluginProjectError, match="not a directory"):
            read_project(not_a_dir)

    def test_classify_shares_the_same_guard_for_a_missing_directory(self, tmp_path: Path) -> None:
        """classify is read_project's own classifier (exported in __all__), so
        the two public entry points of the one reader must not disagree on
        the same bad --dir."""
        missing = tmp_path / "nonexistent"
        with pytest.raises(PluginProjectError, match="no such directory"):
            classify(missing)

    def test_classify_shares_the_same_guard_for_a_regular_file(self, tmp_path: Path) -> None:
        not_a_dir = tmp_path / "plain_file"
        not_a_dir.write_text("not a directory")
        with pytest.raises(PluginProjectError, match="not a directory"):
            classify(not_a_dir)


class TestRequirePluginProject:
    """The one owner of "this directory must be a plugin project": insert, upgrade,
    and check all ask it, so they refuse with one type and one message."""

    def test_a_plugin_project_is_returned(self, tmp_path: Path) -> None:
        _hand_written(tmp_path)
        assert require_plugin_project(tmp_path) == read_project(tmp_path)

    @pytest.mark.parametrize("kind", ["empty", "foreign"])
    def test_anything_else_is_refused_naming_its_classification(
        self, tmp_path: Path, kind: str
    ) -> None:
        if kind == "foreign":
            (tmp_path / "pyproject.toml").write_text('[project]\nname = "other"\n')
        expected = rf"not a graftpunk plugin project \({kind}\)"
        with pytest.raises(NotAPluginProjectError, match=expected):
            require_plugin_project(tmp_path)


class TestTheView:
    def test_the_view_has_exactly_the_fields_its_section_lists(self) -> None:
        """Copied from the spec's list, so a second enumeration cannot drift from it."""
        assert [f.name for f in fields(ProjectView)] == [
            "directory",
            "plugins",
            "defects",
            "requirements",
            "requirement_set",
            "test_markers",
            "fixtures_tree_present",
            "fixtures_tree_blocked",
            "first_party_packages",
        ]
        assert [f.name for f in fields(PluginDefect)] == ["entry_point", "module_path", "message"]
        assert [f.name for f in fields(PluginView)] == [
            "entry_point",
            "module_path",
            "class_name",
            "class_span",
            "site_name",
            "base_url",
            "commands",
            "markers",
            "fixtures_root",
            "class_names",
        ]
        assert [f.name for f in fields(CommandView)] == ["method", "span", "keywords", "group"]

    def test_fixtures_tree_present_is_true_for_a_generated_project(self, tmp_path: Path) -> None:
        _generate(tmp_path)
        assert read_project(tmp_path).fixtures_tree_present is True

    def test_fixtures_tree_present_is_false_when_the_tree_is_missing(self, tmp_path: Path) -> None:
        """check and upgrade take this fact from the one place the reader
        records it, so the two consumers cannot disagree."""
        _generate(tmp_path)
        shutil.rmtree(tmp_path / "tests" / "fixtures")
        assert read_project(tmp_path).fixtures_tree_present is False

    def test_a_generated_plugin_declares_every_endpoint(self, tmp_path: Path) -> None:
        _generate(tmp_path)
        (plugin,) = read_project(tmp_path).plugins
        assert plugin.entry_point == "myshop"
        assert plugin.module_path == "src/graftpunk_myshop/plugin.py"
        assert plugin.class_name == "MyshopPlugin"
        assert (plugin.site_name, plugin.base_url) == ("myshop", "https://myshop.example")
        assert [c.endpoint for c in plugin.commands] == [
            "GET /api/orders",
            "GET /api/orders/{order_id}",
        ]
        text = (tmp_path / plugin.module_path).read_text().splitlines()
        assert plugin.markers
        assert all("GP-FILL" in text[line - 1] for line in plugin.markers)
        assert plugin.fixtures_root == "tests/fixtures/"

    def test_a_form_feed_above_a_marker_does_not_shift_its_reported_line(
        self, tmp_path: Path
    ) -> None:
        """str.splitlines() also breaks on a form feed, which is not a line break
        to ast or the tokenizer; the old splitter reported the marker one line
        late here."""
        module = (
            "from graftpunk.plugins import SitePlugin\n"
            "\n"
            "# note\x0c continued\n"
            "\n"
            "class MyshopPlugin(SitePlugin):\n"
            '    site_name = "myshop"\n'
            "    # GP-FILL: something\n"
        )
        _hand_written(tmp_path, module)
        (plugin,) = read_project(tmp_path).plugins
        assert plugin.markers == (7,)

    def test_test_markers_records_gp_fill_in_the_test_module(self, tmp_path: Path) -> None:
        _generate(tmp_path)
        view = read_project(tmp_path)
        assert view.test_markers
        for path, line in view.test_markers:
            assert path == "tests/test_plugin.py"
            text = (tmp_path / path).read_text().splitlines()
            assert "GP-FILL" in text[line - 1]

    def test_test_markers_are_not_shifted_by_a_form_feed(self, tmp_path: Path) -> None:
        """As test_a_form_feed_above_a_marker_does_not_shift_its_reported_line,
        for _test_markers's own scan of the tests directory."""
        _generate(tmp_path)
        (tmp_path / "tests" / "extra.py").write_text(
            "# note\x0c continued\n\n# GP-FILL: something\n"
        )
        view = read_project(tmp_path)
        assert ("tests/extra.py", 3) in view.test_markers

    def test_test_markers_skips_a_file_that_does_not_decode(self, tmp_path: Path) -> None:
        _generate(tmp_path)
        (tmp_path / "tests" / "broken.py").write_bytes(b"\xff# GP-FILL: unreadable\n")
        view = read_project(tmp_path)
        assert all(path != "tests/broken.py" for path, _ in view.test_markers)

    @pytest.mark.skipif(os.geteuid() == 0, reason="root reads a 000-mode file")
    def test_test_markers_skips_an_unreadable_file(self, tmp_path: Path) -> None:
        """A permission error, not just a decode error, is skipped rather than
        raised."""
        _generate(tmp_path)
        locked = tmp_path / "tests" / "locked.py"
        locked.write_text("# GP-FILL: unreadable\n")
        locked.chmod(0)
        try:
            view = read_project(tmp_path)
        finally:
            locked.chmod(0o644)
        assert all(path != "tests/locked.py" for path, _ in view.test_markers)

    @pytest.mark.skipif(os.geteuid() == 0, reason="root traverses a 000-mode directory")
    def test_a_src_with_no_execute_bit_refuses_in_one_line(self, tmp_path: Path) -> None:
        """chmod 000 denies even traversing into src/, so the plugin module
        itself cannot be reached; the reader must refuse in one line, not
        leak the PermissionError as a traceback."""
        _generate(tmp_path)
        src = tmp_path / "src"
        src.chmod(0o000)
        try:
            with pytest.raises(PluginProjectError):
                read_project(tmp_path)
        finally:
            src.chmod(0o755)

    @pytest.mark.skipif(os.geteuid() == 0, reason="root lists a 300-mode directory")
    def test_an_unlistable_src_contributes_no_first_party_names(self, tmp_path: Path) -> None:
        """chmod 300 denies listing src/ but still allows traversing to a
        known file inside it: the plugin still reads, but the directory
        contributes no first-party name since it cannot be listed."""
        _generate(tmp_path)
        src = tmp_path / "src"
        src.chmod(0o300)
        try:
            view = read_project(tmp_path)
        finally:
            src.chmod(0o755)
        assert "graftpunk_myshop" not in view.first_party_packages

    def test_known_first_party_from_the_new_isort_table_changes_placement(
        self, tmp_path: Path
    ) -> None:
        """[tool.ruff.lint.isort] known-first-party names a module with no
        directory on disk for the scan to find; the placed import must still
        land in the first-party section ruff itself agrees with."""
        _generate(tmp_path)
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            pyproject.read_text()
            + '\n[tool.ruff.lint.isort]\nknown-first-party = ["myshop_shared"]\n'
        )
        view = read_project(tmp_path)
        assert "myshop_shared" in view.first_party_packages
        text = with_import(
            "import pytest\n",
            "myshop_shared.util",
            "helper",
            first_party=view.first_party_packages,
        )
        assert text == "import pytest\n\nfrom myshop_shared.util import helper\n"
        (tmp_path / "mod.py").write_text(text)
        check = subprocess.run(  # noqa: S603 - argv is fixed, not untrusted input
            [sys.executable, "-m", "ruff", "check", "--select", "I", "mod.py"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
        )
        assert check.returncode == 0, check.stdout + check.stderr

    def test_known_first_party_from_the_legacy_isort_table_changes_placement(
        self, tmp_path: Path
    ) -> None:
        """The legacy [tool.ruff.isort] table, for a project that has not
        migrated to [tool.ruff.lint.isort], is read the same way."""
        _generate(tmp_path)
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            pyproject.read_text() + '\n[tool.ruff.isort]\nknown-first-party = ["myshop_shared"]\n'
        )
        view = read_project(tmp_path)
        assert "myshop_shared" in view.first_party_packages
        text = with_import(
            "import pytest\n",
            "myshop_shared.util",
            "helper",
            first_party=view.first_party_packages,
        )
        assert text == "import pytest\n\nfrom myshop_shared.util import helper\n"
        (tmp_path / "mod.py").write_text(text)
        check = subprocess.run(  # noqa: S603 - argv is fixed, not untrusted input
            [sys.executable, "-m", "ruff", "check", "--select", "I", "mod.py"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
        )
        assert check.returncode == 0, check.stdout + check.stderr

    def test_test_markers_skips_a_venv_directory(self, tmp_path: Path) -> None:
        _generate(tmp_path)
        marker = tmp_path / "tests" / ".venv" / "x.py"
        marker.parent.mkdir(parents=True)
        marker.write_text("# GP-FILL: unreadable\n")
        view = read_project(tmp_path)
        assert all(not path.startswith("tests/.venv") for path, _ in view.test_markers)

    def test_test_markers_skips_a_directory_named_like_a_python_file(self, tmp_path: Path) -> None:
        """A directory whose name ends in .py matches the *.py glob too; reading
        it as text must not raise."""
        _generate(tmp_path)
        (tmp_path / "tests" / "odd.py").mkdir()
        view = read_project(tmp_path)
        assert all(path != "tests/odd.py" for path, _ in view.test_markers)

    def test_class_names_records_every_top_level_binding(self, tmp_path: Path) -> None:
        module = (
            "from graftpunk.plugins import CommandContext, SitePlugin, command\n\n\n"
            "class MyshopPlugin(SitePlugin):\n"
            '    site_name = "myshop"\n\n'
            "    orders: int = 1\n\n"
            "    def helper(self) -> None:\n"
            "        pass\n\n"
            '    @command(help="List orders")\n'
            "    def orders_command(self, ctx: CommandContext) -> dict:\n"
            "        return {}\n\n"
            '    @command(help="Admin")\n'
            "    class Admin:\n"
            "        pass\n"
        )
        _hand_written(tmp_path, module)
        (plugin,) = read_project(tmp_path).plugins
        assert plugin.class_names == frozenset(
            {"site_name", "orders", "helper", "orders_command", "Admin"}
        )

    def test_class_names_records_an_import_binding(self, tmp_path: Path) -> None:
        module = (
            "from graftpunk.plugins import SitePlugin\n\n\n"
            "class MyshopPlugin(SitePlugin):\n"
            '    site_name = "myshop"\n\n'
            "    import functools\n"
        )
        _hand_written(tmp_path, module)
        (plugin,) = read_project(tmp_path).plugins
        assert "functools" in plugin.class_names

    def test_class_names_records_a_definition_under_except_star(self, tmp_path: Path) -> None:
        """ast.TryStar (a try/except* block, added in Python 3.11, which this
        project's floor is) is walked the same as ast.Try, so a definition
        nested under it is still refused as an add-command name."""
        module = (
            "from graftpunk.plugins import SitePlugin\n\n\n"
            "class MyshopPlugin(SitePlugin):\n"
            '    site_name = "myshop"\n\n'
            "    try:\n"
            "        pass\n"
            "    except* ValueError:\n"
            "        def helper() -> None:\n"
            "            pass\n"
        )
        _hand_written(tmp_path, module)
        (plugin,) = read_project(tmp_path).plugins
        assert "helper" in plugin.class_names

    def test_class_names_records_tuple_unpacking(self, tmp_path: Path) -> None:
        module = (
            "from graftpunk.plugins import SitePlugin\n\n\n"
            "class MyshopPlugin(SitePlugin):\n"
            '    site_name = "myshop"\n\n'
            "    first, second = 1, 2\n"
        )
        _hand_written(tmp_path, module)
        (plugin,) = read_project(tmp_path).plugins
        assert {"first", "second"} <= plugin.class_names

    def test_a_command_group_is_recorded_as_a_command_with_no_endpoint(
        self, tmp_path: Path
    ) -> None:
        module = (
            "from graftpunk.plugins import CommandContext, SitePlugin, command\n\n\n"
            "class MyshopPlugin(SitePlugin):\n"
            '    site_name = "myshop"\n\n'
            '    @command(help="Admin commands")\n'
            "    class Admin:\n"
            '        @command(help="List admins")\n'
            "        def list(self, ctx: CommandContext) -> dict:\n"
            "            return {}\n"
        )
        _hand_written(tmp_path, module)
        (plugin,) = read_project(tmp_path).plugins
        (group,) = plugin.commands
        assert (group.method, group.cli_name, group.endpoint) == ("Admin", "admin", None)
        assert group.group is True
        assert "Admin" in plugin.class_names

    def test_a_plugin_is_found_by_its_entry_point_name(self, tmp_path: Path) -> None:
        _generate(tmp_path)
        view = read_project(tmp_path)
        (plugin,) = view.plugins
        assert view.plugin("myshop") is plugin
        assert view.plugin("nope") is None

    def test_a_hand_written_command_without_a_declaration_reads_none(self, tmp_path: Path) -> None:
        _hand_written(tmp_path)
        (plugin,) = read_project(tmp_path).plugins
        (command,) = plugin.commands
        assert (command.method, command.endpoint) == ("orders", None)
        assert command.keywords == {"help": "List orders"}
        assert command.group is False
        assert plugin.markers == ()

    def test_cli_name_is_the_pinned_name_or_the_kebab_cased_method(self) -> None:
        span = Span(1, 2)
        assert CommandView("api_orders", span, MappingProxyType({})).cli_name == "api-orders"
        pinned = CommandView("by_id", span, MappingProxyType({"name": "order"}))
        assert pinned.cli_name == "order"

    def test_the_view_is_read_only(self, tmp_path: Path) -> None:
        _hand_written(tmp_path)
        view = read_project(tmp_path)
        (command,) = view.plugins[0].commands
        with pytest.raises(TypeError):
            command.keywords["endpoint"] = "GET /api/orders"
        with pytest.raises(TypeError):
            view.requirements["tests/conftest.py:FIXTURES_TREE"] = True

    def test_spans_cover_the_decorator_through_the_body(self, tmp_path: Path) -> None:
        _hand_written(tmp_path)
        (plugin,) = read_project(tmp_path).plugins
        lines = _HAND_WRITTEN_MODULE.splitlines()
        (command,) = plugin.commands
        assert lines[command.span.start - 1].strip().startswith("@command(")
        assert lines[command.span.end - 1].strip().startswith("return ctx.request_json")
        assert lines[plugin.class_span.start - 1].startswith("class MyshopPlugin")
        assert plugin.class_span.end == command.span.end

    @pytest.mark.parametrize("classes", [0, 2])
    def test_a_module_without_exactly_one_plugin_class_is_recorded_not_raised(
        self, tmp_path: Path, classes: int
    ) -> None:
        """A per-plugin defect: the rest of the project still reads, and each
        consumer decides whether the defect stops it."""
        body = "from graftpunk.plugins import SitePlugin\n\n\n" + "".join(
            f"class P{i}(SitePlugin):\n    site_name = 'p{i}'\n\n\n" for i in range(classes)
        )
        _hand_written(tmp_path, body)
        view = read_project(tmp_path)
        assert view.plugins == ()
        (defect,) = view.defects
        assert (defect.entry_point, defect.module_path) == (
            "myshop",
            "src/graftpunk_myshop/plugin.py",
        )
        assert f"exactly one SitePlugin subclass, found {classes}" in defect.message
        assert view.requirements, "the requirements still read"
        assert view.plugin("myshop") == defect

    def test_a_module_that_does_not_parse_is_refused(self, tmp_path: Path) -> None:
        _hand_written(tmp_path, "class (:\n")
        with pytest.raises(PluginProjectError, match="does not parse"):
            read_project(tmp_path)

    def test_a_missing_module_is_refused(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(_HAND_WRITTEN_PYPROJECT)
        with pytest.raises(PluginProjectError, match="graftpunk_myshop/plugin.py"):
            read_project(tmp_path)


class TestFixturesRoots:
    def test_a_suite_gives_its_first_member_the_tree_and_the_next_its_own_root(
        self, tmp_path: Path
    ) -> None:
        _generate(tmp_path, "myshop")
        write_scaffold(
            tmp_path,
            ScaffoldSpec(
                name="widgets",
                mode="new_project",
                backend="nodriver",
                base_url="https://myshop.example",
                digest=_digest("/api/widgets"),
            ),
        )
        roots = {p.site_name: p.fixtures_root for p in read_project(tmp_path).plugins}
        assert roots == {"myshop": "tests/fixtures/", "widgets": "tests/fixtures/widgets/"}

    @pytest.mark.parametrize("second", [False, True])
    def test_the_rule_gives_the_same_answer_from_the_spec_and_from_the_project(
        self, tmp_path: Path, second: bool
    ) -> None:
        _generate(tmp_path, "myshop")
        name, mode = ("widgets", "add_to_suite") if second else ("myshop", "new_project")
        if second:
            write_scaffold(
                tmp_path,
                ScaffoldSpec(
                    name=name,
                    mode="new_project",
                    backend="nodriver",
                    base_url="https://myshop.example",
                    digest=_digest("/api/widgets"),
                ),
            )
        spec = ScaffoldSpec(
            name=name, mode=mode, backend="nodriver", base_url="https://myshop.example"
        )
        view = next(p for p in read_project(tmp_path).plugins if p.site_name == name)
        assert (
            fixtures_root_for(spec)
            == view.fixtures_root
            == fixtures_root(suite_member=second, module_name=module_name_for(name))
        )

    def test_a_hand_written_single_plugin_project_is_not_a_suite_member(
        self, tmp_path: Path
    ) -> None:
        """A one-entry-point project is never a suite, even when its
        [project].name differs from its package, which is exactly the case a
        hand-written project (the one add-command's enhance mode targets) is
        likely to be in; gp plugin new never produces this shape itself, since
        its first plugin's package always matches the project name."""
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "myshop-plugin"\n\n'
            '[project.entry-points."graftpunk.plugins"]\n'
            'myshop = "graftpunk_myshop.plugin:MyshopPlugin"\n'
        )
        package = tmp_path / "src" / "graftpunk_myshop"
        package.mkdir(parents=True)
        (package / "plugin.py").write_text(
            "from graftpunk.plugins import SitePlugin\n\n\n"
            "class MyshopPlugin(SitePlugin):\n"
            '    site_name = "myshop"\n'
        )
        (plugin,) = read_project(tmp_path).plugins
        assert plugin.fixtures_root == "tests/fixtures/"

    def test_fixtures_dir_is_the_root_and_the_conftest_tree_contains_it(
        self, tmp_path: Path
    ) -> None:
        _generate(tmp_path, "myshop")
        write_scaffold(
            tmp_path,
            ScaffoldSpec(
                name="widgets",
                mode="new_project",
                backend="nodriver",
                base_url="https://myshop.example",
                digest=_digest("/api/widgets"),
            ),
        )
        tree = _assigned(tmp_path / "tests" / "conftest.py", "FIXTURES_TREE")
        for plugin, test_file in (("myshop", "test_plugin.py"), ("widgets", "test_widgets.py")):
            view = next(p for p in read_project(tmp_path).plugins if p.site_name == plugin)
            fixtures_dir = _assigned(tmp_path / "tests" / test_file, "FIXTURES_DIR")
            assert fixtures_dir == tmp_path / view.fixtures_root.rstrip("/")
            assert fixtures_dir.is_relative_to(tree)


def _assigned(module: Path, name: str) -> Path:
    """The value of *name*'s module-level Path assignment in *module*, evaluated."""
    for node in ast.parse(module.read_text()).body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == name for t in node.targets
        ):
            return eval(  # noqa: S307 - evaluates the generator's own Path expression
                ast.unparse(node.value), {"Path": Path, "__file__": str(module)}
            )
    raise AssertionError(f"{module} does not assign {name}")


class TestRequirementsAreDecidedStructurally:
    @pytest.mark.parametrize(
        "conftest",
        [
            'from pathlib import Path\nFIXTURES_TREE = Path(__file__).parent / "fixtures"\n'
            "sanitised_fixtures = 1\n",
            "from pathlib import Path\n"
            'FIXTURES_TREE = (\n    Path(__file__).parent\n    / "fixtures"\n)\n'
            "from graftpunk.testing.plugin import fixtures_are_sanitised as sanitised_fixtures\n",
            "from somewhere import FIXTURES_TREE, sanitised_fixtures\n",
            "FIXTURES_TREE, sanitised_fixtures = 1, 2\n",
        ],
    )
    def test_any_module_level_binding_satisfies(self, tmp_path: Path, conftest: str) -> None:
        _generate(tmp_path)
        (tmp_path / "tests" / "conftest.py").write_text(conftest)
        assert _states(tmp_path) == {"bound"}

    @pytest.mark.parametrize(
        "conftest",
        [
            "",
            "from graftpunk.testing.plugin import *\n",
            "if True:\n    FIXTURES_TREE = 1\n    sanitised_fixtures = 2\n",
            "try:\n    from x import FIXTURES_TREE, sanitised_fixtures\n"
            "except ImportError:\n    pass\n",
        ],
    )
    def test_a_star_import_a_conditional_or_nothing_does_not(
        self, tmp_path: Path, conftest: str
    ) -> None:
        _generate(tmp_path)
        (tmp_path / "tests" / "conftest.py").write_text(conftest)
        assert _states(tmp_path) == {"unbound"}

    def test_a_generated_project_satisfies_every_requirement(self, tmp_path: Path) -> None:
        _generate(tmp_path)
        requirements = read_project(tmp_path).requirements
        assert {key: status.state for key, status in requirements.items()} == {
            "tests/conftest.py:FIXTURES_TREE": "bound",
            "tests/conftest.py:sanitised_fixtures": "bound",
        }

    def test_a_requirement_file_that_does_not_parse_is_unreadable_not_raised(
        self, tmp_path: Path
    ) -> None:
        """One broken conftest must not blind info and add-command, which never read
        it: the view records it, upgrade refuses on it, and check reports it."""
        _generate(tmp_path)
        (tmp_path / "tests" / "conftest.py").write_text("def (:\n")
        view = read_project(tmp_path)
        assert view.plugins, "the plugins still read"
        for status in view.requirements.values():
            assert status.state == "unreadable"
            assert status.reason is not None and "does not parse" in status.reason
        assert view.missing_requirements() == ()
        ((path, reason),) = view.unreadable_files()
        assert path == "tests/conftest.py"
        assert reason.startswith("does not parse (")

    def test_a_requirement_file_with_a_bad_byte_is_unreadable_not_raised(
        self, tmp_path: Path
    ) -> None:
        _generate(tmp_path)
        (tmp_path / "tests" / "conftest.py").write_bytes(b"# \xff\n")
        view = read_project(tmp_path)
        assert view.plugins, "the plugins still read"
        for status in view.requirements.values():
            assert status.state == "unreadable"
            assert status.reason is not None and "UTF-8" in status.reason
        ((path, reason),) = view.unreadable_files()
        assert path == "tests/conftest.py"
        assert "UTF-8" in reason

    def test_each_requirement_file_is_parsed_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two requirements live in tests/conftest.py; the reader parses it once and
        both statuses come from that one tree."""
        _generate(tmp_path)
        conftest = (tmp_path / "tests" / "conftest.py").read_text(encoding="utf-8")
        parsed: list[str] = []
        real_parse = ast.parse

        def counting_parse(source: str) -> ast.Module:
            parsed.append(source)
            return real_parse(source)

        monkeypatch.setattr(ast, "parse", counting_parse)
        view = read_project(tmp_path)
        assert parsed.count(conftest) == 1
        assert {status.state for status in view.requirements.values()} == {"bound"}

    def test_requirement_statuses_uses_the_set_it_is_given(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """read_project reads policy.PROJECT_REQUIREMENTS once and passes that
        snapshot to _requirement_statuses; the function computes from the
        argument, not a second live read of the module attribute, so the two
        cannot drift within one call."""
        from graftpunk.devtools.plugin_project import _requirement_statuses

        _generate(tmp_path)
        monkeypatch.setattr(policy, "PROJECT_REQUIREMENTS", ())
        given = (
            ProjectRequirement(
                path="tests/conftest.py",
                name="FIXTURES_TREE",
                statement='FIXTURES_TREE = Path(__file__).parent / "fixtures"',
            ),
        )
        statuses = _requirement_statuses(tmp_path, given)
        assert set(statuses) == {"tests/conftest.py:FIXTURES_TREE"}

    def test_missing_requirements_are_the_unbound_entries_in_declared_order(
        self, tmp_path: Path
    ) -> None:
        _generate(tmp_path)
        (tmp_path / "tests" / "conftest.py").write_text(
            'from pathlib import Path\nFIXTURES_TREE = Path(__file__).parent / "fixtures"\n'
        )
        missing = read_project(tmp_path).missing_requirements()
        assert [r.name for r in missing] == ["sanitised_fixtures"]

    def test_a_directory_that_is_not_a_plugin_project_lacks_nothing(self, tmp_path: Path) -> None:
        assert read_project(tmp_path).missing_requirements() == ()


def _states(root: Path) -> set[str]:
    return {status.state for status in read_project(root).requirements.values()}


def test_the_reader_imports_neither_the_writer_nor_the_renderer() -> None:
    """A structural view: it reads policy, never the module that writes the disk or
    the one that renders a project."""
    script = (
        "import sys\n"
        "import graftpunk.devtools.plugin_project\n"
        "print(sorted(m for m in ('graftpunk.devtools.scaffold.write',"
        " 'graftpunk.devtools.scaffold.render') if m in sys.modules))\n"
    )
    result = subprocess.run(  # noqa: S603 - argv is fixed, not untrusted input
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=60, check=True
    )
    assert result.stdout.strip() == "[]"
