"""Family templates for sibling letters-only path segments (#271, part 1).

Two or more recorded paths that differ in one letters-only segment, and agree on
everything else, are reported as one more endpoint with a placeholder beside
their literal rows. Nothing is removed or renamed: a run with no family digests,
projects, and scaffolds exactly as graftpunk 1.31.1 did, which the golden file
pins.
"""

from __future__ import annotations

import json
from pathlib import Path

from graftpunk.devtools.scaffold.render import ScaffoldSpec, render
from graftpunk.devtools.scaffold.selection import CommandSelection
from graftpunk.har.digest import DigestSource, digest
from graftpunk.har.report import render_endpoints_json, render_json, render_markdown
from tests.unit.test_har_digest import _entry, _write_har

_HOST = "https://myshop.example.com"
_FIXTURES = Path(__file__).parent.parent / "fixtures"
_GOLDEN = _FIXTURES / "digest_without_families.json"
_RESULTS = json.dumps({"results": [{"name": "x"}], "total": 1})
_HITS = json.dumps({"hits": [], "page": 1})
_PAGE = "<html><body><p>page</p></body></html>"


def _get(path: str, body: str = _RESULTS, content_type: str = "application/json") -> dict:
    return _entry("GET", f"{_HOST}{path}", content_type=content_type, body=body)


# The runs the golden file holds the 1.31.1 output of: each is a near miss of a
# family, or holds none at all, so none of them gains a row.
def _golden_runs(base: Path) -> dict[str, tuple[DigestSource, tuple[CommandSelection, ...]]]:
    runs: dict[str, tuple[list[dict], tuple[CommandSelection, ...]]] = {
        "shapes-differ": (
            [_get("/search/north-region/items/"), _get("/search/south-region/items/", _HITS)],
            (CommandSelection("north", "GET", "/search/north-region/items/"),),
        ),
        "content-types-differ": (
            [
                _get("/search/north-region/items/"),
                _get("/search/south-region/items/", _PAGE, "text/html"),
            ],
            (),
        ),
        "methods-differ": (
            [
                _get("/search/north-region/items/"),
                _entry("POST", f"{_HOST}/search/south-region/items/", body=_RESULTS),
            ],
            (),
        ),
        "two-segments-differ": (
            [_get("/search/north-region/items/"), _get("/browse/south-region/items/")],
            (),
        ),
        "one-member": ([_get("/search/north-region/items/"), _get("/api/orders", _HITS)], ()),
        "file-extension": (
            [
                _get("/docs/intro.html", _PAGE, "text/html"),
                _get("/docs/setup.html", _PAGE, "text/html"),
            ],
            (),
        ),
        "fixed-vocabulary": ([_get("/api/items"), _get("/search/items")], ()),
        "one-segment": (
            [_get("/about", _PAGE, "text/html"), _get("/contact", _PAGE, "text/html")],
            (),
        ),
        "digit-bearing": ([_get("/shop/page2/items"), _get("/shop/page3/items")], ()),
        "trailing-slash-differs": (
            [_get("/search/north-region/items/"), _get("/search/south-region/items")],
            (),
        ),
    }
    sources: dict[str, tuple[DigestSource, tuple[CommandSelection, ...]]] = {}
    for name, (entries, commands) in runs.items():
        run = base / name
        run.mkdir(parents=True)
        sources[name] = (DigestSource(har_path=_write_har(run, entries)), commands)
    sources["sample"] = (
        DigestSource(har_path=_FIXTURES / "sample.har"),
        (CommandSelection("user-posts", "GET", "/api/users/{user_id}/posts"),),
    )
    return sources


def golden_outputs(base: Path) -> dict[str, dict[str, str]]:
    """Every output the digest gives for each golden run, with *base* spelled
    ``<run>`` and the fixtures directory ``<fixtures>``: the markdown, the JSON, the
    endpoints projection, and the plugin and test module the generator writes, by
    default and, where a run names one, for a literal ``--command``."""
    outputs: dict[str, dict[str, str]] = {}
    for name, (source, commands) in _golden_runs(base).items():
        result = digest(source)
        files: dict[str, str] = {
            "markdown": render_markdown(result),
            "json": render_json(result),
            "endpoints": render_endpoints_json(result),
        }
        for label, selected in (("default", ()), ("command", commands)):
            if label == "command" and not selected:
                continue
            rendered = render(
                ScaffoldSpec(
                    name="myshop",
                    mode="new_project",
                    backend="nodriver",
                    base_url=_HOST,
                    digest=result,
                    graftpunk_version="1.31.1",
                    commands=selected,
                )
            )
            files[f"{label}-plugin"] = rendered["src/graftpunk_myshop/plugin.py"]
            files[f"{label}-tests"] = rendered["tests/test_plugin.py"]
        outputs[name] = {
            key: value.replace(str(base), "<run>").replace(str(_FIXTURES), "<fixtures>")
            for key, value in files.items()
        }
    return outputs


def test_a_run_without_a_family_digests_as_1_31_1_did(tmp_path: Path) -> None:
    """The golden file was written by graftpunk 1.31.1 (main at 9946864b) from these
    runs; every output must match it byte for byte."""
    golden = json.loads(_GOLDEN.read_text(encoding="utf-8"))
    assert golden_outputs(tmp_path) == golden
