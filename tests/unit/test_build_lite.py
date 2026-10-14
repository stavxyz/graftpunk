"""graftpunk-lite is derived from the root pyproject.toml by scripts/build_lite.py.

These tests pin the derivation (what lite keeps, drops and rewrites), the
[tool.graftpunk.lite] table it reads, and the distributions it builds.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "build_lite.py"

# Packages that only make sense with a real browser. A root dependency named
# here must also be in browser-only, or graftpunk-lite would pull it in.
KNOWN_BROWSER_PACKAGES = {
    "selenium",
    "requestium",
    "webdriver-manager",
    "undetected-chromedriver",
    "selenium-stealth",
    "nodriver",
    "httpie",
    "playwright",
    "pyppeteer",
    "zendriver",
}


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("build_lite", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


build_lite = _load_script()


@pytest.fixture(scope="module")
def root() -> dict[str, Any]:
    with (REPO_ROOT / "pyproject.toml").open("rb") as f:
        return tomllib.load(f)


@pytest.fixture(scope="module")
def lite(root: dict[str, Any]) -> dict[str, Any]:
    return build_lite.lite_pyproject(root)


def _name(requirement: str) -> str:
    return canonicalize_name(Requirement(requirement).name)


def _browser_only(root: dict[str, Any]) -> set[str]:
    return {canonicalize_name(n) for n in root["tool"]["graftpunk"]["lite"]["browser-only"]}


class TestLitePyproject:
    def test_name(self, lite: dict[str, Any]) -> None:
        assert lite["project"]["name"] == "graftpunk-lite"

    def test_no_browser_only_requirement_in_dependencies(
        self, root: dict[str, Any], lite: dict[str, Any]
    ) -> None:
        names = {_name(r) for r in lite["project"]["dependencies"]}
        assert not names & _browser_only(root)
        assert "requests" in names

    def test_dependencies_keep_root_order_and_text(
        self, root: dict[str, Any], lite: dict[str, Any]
    ) -> None:
        kept = [r for r in root["project"]["dependencies"] if _name(r) not in _browser_only(root)]
        assert lite["project"]["dependencies"] == kept

    def test_browser_extra_holds_exactly_the_removed_requirements(
        self, root: dict[str, Any], lite: dict[str, Any]
    ) -> None:
        removed = [r for r in root["project"]["dependencies"] if _name(r) in _browser_only(root)]
        assert lite["project"]["optional-dependencies"]["browser"] == removed
        assert "nodriver>=0.48; python_version < '3.14'" in removed
        assert "nodriver>=0.50.6; python_version >= '3.14'" in removed
        assert len(removed) == 8

    def test_self_referencing_extras_point_at_lite(self, lite: dict[str, Any]) -> None:
        extras = lite["project"]["optional-dependencies"]
        assert extras["nodriver"] == ["graftpunk-lite[browser]"]
        assert extras["dev"][0] == "graftpunk-lite[browser]"
        assert extras["all"] == ["graftpunk-lite[browser,supabase,s3,jmespath,nodriver,dev]"]
        for requirements in extras.values():
            assert all(_name(r) != "graftpunk" for r in requirements)

    def test_other_extras_unchanged(self, root: dict[str, Any], lite: dict[str, Any]) -> None:
        for extra in ("supabase", "s3", "jmespath"):
            assert (
                lite["project"]["optional-dependencies"][extra]
                == root["project"]["optional-dependencies"][extra]
            )

    def test_description_says_lite(self, root: dict[str, Any], lite: dict[str, Any]) -> None:
        description = lite["project"]["description"]
        assert description.startswith(root["project"]["description"])
        assert "Lite install" in description

    def test_every_other_key_is_copied(self, root: dict[str, Any], lite: dict[str, Any]) -> None:
        changed = {"name", "description", "dependencies", "optional-dependencies"}
        for key, value in root["project"].items():
            if key not in changed:
                assert lite["project"][key] == value, key
        assert set(lite["project"]) == set(root["project"])
        for table in root:
            if table != "project":
                assert lite[table] == root[table], table

    def test_root_is_not_mutated(self, root: dict[str, Any]) -> None:
        build_lite.lite_pyproject(root)
        assert root["project"]["name"] == "graftpunk"
        assert root["project"]["optional-dependencies"]["browser"] == []

    def test_written_toml_reads_back_equal(self, lite: dict[str, Any]) -> None:
        assert tomllib.loads(build_lite.dumps_toml(lite)) == lite

    def test_writer_escapes_strings(self) -> None:
        data = {"t": {"s": 'a "quoted" \\ back\nslash', "dotted.key": True, "e": {}}}
        assert tomllib.loads(build_lite.dumps_toml(data)) == data


class TestRootConsistency:
    def test_every_browser_only_name_matches_a_dependency(self, root: dict[str, Any]) -> None:
        dependency_names = {_name(r) for r in root["project"]["dependencies"]}
        missing = _browser_only(root) - dependency_names
        assert not missing, f"browser-only names no dependency: {sorted(missing)}"

    def test_no_browser_package_outside_browser_only(self, root: dict[str, Any]) -> None:
        dependency_names = {_name(r) for r in root["project"]["dependencies"]}
        leaked = (dependency_names & KNOWN_BROWSER_PACKAGES) - _browser_only(root)
        assert not leaked, f"browser packages graftpunk-lite would install: {sorted(leaked)}"

    def test_root_browser_extra_is_empty(self, root: dict[str, Any]) -> None:
        assert root["project"]["optional-dependencies"]["browser"] == []


def test_lite_readme_leads_with_lite_and_links_the_full_readme_at_the_tag() -> None:
    readme = build_lite.lite_readme("1.20.0")
    assert readme.startswith("# graftpunk-lite\n\ngraftpunk-lite is graftpunk without")
    assert "pip install graftpunk-lite\n" in readme
    assert "pip install 'graftpunk-lite[browser]'" in readme
    assert "never both" in readme
    assert "https://github.com/stavxyz/graftpunk/blob/v1.20.0/README.md" in readme
    # It must not tell a lite reader to install the other distribution.
    assert "pip install graftpunk\n" not in readme
    assert "pip install graftpunk " not in readme


def test_lite_readme_promises_only_the_browser_free_session_load() -> None:
    readme = build_lite.lite_readme("1.20.0")
    assert "`load_session_for_api_from_bytes`" in readme
    assert "load_session_for_api(" not in readme
    # load_session_for_api unpickles the browser session object, so the README may
    # name it only where it says the [browser] extra is needed.
    store_loaders = [p for p in readme.split("\n\n") if "`load_session_for_api`" in p]
    assert store_loaders
    assert all("graftpunk-lite[browser]" in p for p in store_loaders)
    flat = " ".join(readme.split())
    assert "full `gp` CLI" not in flat
    assert "`gp` CLI, API" not in flat


def test_lite_readme_names_what_gp_does_without_the_browser_extra() -> None:
    """Checked against a clean graftpunk-lite venv: these commands run, loading a
    cached session needs [browser], and a plugin needs graftpunk (installing one
    --no-deps on lite makes every gp run report it failed to load)."""
    flat = " ".join(build_lite.lite_readme("1.20.0").split())
    assert (
        "On graftpunk-lite, `gp version`, `gp session list`, `gp session show`, "
        "`gp config`, `gp plugins`, `gp http` with `--no-session`, and "
        "`load_session_for_api_from_bytes` work."
    ) in flat
    assert (
        "Everything that loads a cached session from the session store "
        "(`load_session_for_api`, `gp http` with a session from `--session`, "
        "`gp session use` or `GRAFTPUNK_SESSION`, and `gp session export`), every "
        "command that drives a browser (recording with `gp observe`, and a plugin's "
        "`login` command), and a plugin's own commands need `graftpunk-lite[browser]` or "
        "`graftpunk`."
    ) in flat
    assert (
        "Plugins and their `gp` commands need the browser stack (graftpunk, or "
        "`graftpunk-lite[browser]`). Plugins declare `graftpunk[browser]`, so installing "
        "one where graftpunk-lite is installed pulls graftpunk in beside it."
    ) in flat
    assert "--no-deps" not in flat


def _uv_build(args: list[str]) -> None:
    subprocess.run(  # noqa: S603 - argv is fixed, not untrusted input
        ["uv", "build", *args],  # noqa: S607 - uv from PATH, as the build script runs it
        check=True,
        capture_output=True,
        cwd=REPO_ROOT,
    )


def _graftpunk_files(wheel: Path) -> list[str]:
    with zipfile.ZipFile(wheel) as zf:
        return sorted(n for n in zf.namelist() if n.startswith("graftpunk/"))


def test_build_produces_lite_sdist_and_wheel(tmp_path: Path, root: dict[str, Any]) -> None:
    version = root["project"]["version"]
    lite_out = tmp_path / "lite"
    subprocess.run(  # noqa: S603 - argv is fixed, not untrusted input
        [sys.executable, str(SCRIPT), "--out-dir", str(lite_out)],
        check=True,
        capture_output=True,
    )
    assert sorted(p.name for p in lite_out.glob("graftpunk*")) == [
        f"graftpunk_lite-{version}-py3-none-any.whl",
        f"graftpunk_lite-{version}.tar.gz",
    ]

    wheel = lite_out / f"graftpunk_lite-{version}-py3-none-any.whl"
    with zipfile.ZipFile(wheel) as zf:
        metadata = zf.read(f"graftpunk_lite-{version}.dist-info/METADATA").decode()
    assert "Name: graftpunk-lite\n" in metadata
    assert f"Version: {version}\n" in metadata
    requires = [line for line in metadata.splitlines() if line.startswith("Requires-Dist:")]
    # Hatchling flattens self-referencing extras, so the browser stack also
    # appears under nodriver, dev and all; it must never appear unconditionally.
    base = [line for line in requires if "extra ==" not in line]
    assert base
    for line in base:
        assert not any(pkg in line for pkg in ("selenium", "nodriver", "requestium")), line
    assert any("selenium>=4.0.0; extra == 'browser'" in line for line in requires)
    # The long description is lite's own README, not graftpunk's.
    description = metadata.split("\n\n", 1)[1]
    assert description.strip() == build_lite.lite_readme(version).strip()

    root_out = tmp_path / "root"
    _uv_build(["--wheel", "--out-dir", str(root_out)])
    root_wheel = root_out / f"graftpunk-{version}-py3-none-any.whl"
    assert _graftpunk_files(wheel) == _graftpunk_files(root_wheel)
    assert len(_graftpunk_files(wheel)) > 50
