"""``state_from_pickle`` reads the state dict out of any release's pickle."""

import json
import pickle
import subprocess
import sys
import types

import dill
import pytest

from graftpunk import session_riders
from graftpunk.session_legacy import (
    FROZEN_CLASS_PATHS,
    NoSessionStateError,
    _Capture,
    state_from_pickle,
)
from tests.unit.session_compat_cases import (
    EXPECTED_KIND,
    assert_leaf_imports,
    case_id,
    case_named,
    cases,
    cookie_dicts,
    decrypt,
    iter_cases,
    token_dicts,
)


def test_session_legacy_is_a_leaf():
    assert_leaf_imports("graftpunk.session_legacy", {"graftpunk.session_riders"})


def test_frozen_class_paths_are_the_riders_paths_plus_cached_token():
    token = (session_riders.CachedToken.__module__, session_riders.CachedToken.__qualname__)
    assert set(FROZEN_CLASS_PATHS) == set(session_riders.SESSION_CLASS_PATHS) | {token}
    assert FROZEN_CLASS_PATHS[token] is session_riders.CachedToken


@pytest.mark.parametrize("case", iter_cases(), ids=case_id)
def test_every_fixture_gives_its_state(case):
    state = state_from_pickle(decrypt(case)).state
    assert cookie_dicts(state["cookies"]) == case.expected["cookies"]


@pytest.mark.parametrize("case", iter_cases(), ids=case_id)
def test_no_graftpunk_class_is_ever_stubbed(case):
    legacy = state_from_pickle(decrypt(case))
    assert not [p for p in legacy.unresolved if p.startswith("graftpunk.")]


@pytest.mark.parametrize("case", cases(kinds=("browser",)), ids=case_id)
def test_a_browser_state_has_the_name_getstate_wrote(case):
    legacy = state_from_pickle(decrypt(case))
    assert legacy.kind == "browser" and legacy.state["session_name"]  # never "_session_name"


@pytest.mark.parametrize("case", cases(kinds=("api", "requests")), ids=case_id)
def test_api_and_requests_states_hold_no_name(case):
    assert "session_name" not in state_from_pickle(decrypt(case)).state


@pytest.mark.parametrize("case", iter_cases(), ids=case_id)
def test_kind_follows_the_saved_class(case):
    legacy = state_from_pickle(decrypt(case))
    assert legacy.kind == EXPECTED_KIND[case.case]


def test_a_subclass_has_no_kind():
    legacy = state_from_pickle(decrypt(case_named("1.44.0", "subclass")))
    assert legacy.kind is None


def test_a_pickled_namespace_gives_its_attributes_and_a_pickled_list_is_refused():
    legacy = state_from_pickle(dill.dumps(types.SimpleNamespace(cookies=1, headers=2)))
    assert legacy.state == {"cookies": 1, "headers": 2} and legacy.kind is None
    with pytest.raises(NoSessionStateError):
        state_from_pickle(dill.dumps([1, 2]))
    with pytest.raises(pickle.UnpicklingError):
        state_from_pickle(b"this is not a pickle at all")


def test_a_selenium_pickle_gives_its_state():
    legacy = state_from_pickle(decrypt(case_named("1.44.0", "selenium")))
    assert legacy.kind == "browser" and legacy.state["_backend_type"] == "selenium"


def test_tokens_come_back_as_cached_tokens():
    case = case_named("1.44.0", "tokens")
    state = state_from_pickle(decrypt(case)).state
    tokens = state["_gp_cached_tokens"]
    assert all(type(t) is session_riders.CachedToken for t in tokens.values())
    assert token_dicts(types.SimpleNamespace(**state)) == case.expected["cached_tokens"]


def test_reading_tokens_never_imports_graftpunk_tokens(tmp_path):
    case = case_named("1.44.0", "tokens")
    payload = tmp_path / "payload.pickle"
    payload.write_bytes(decrypt(case))
    script = (
        "import json, sys, types\n"
        "import graftpunk.session_legacy as legacy\n"
        "sys.modules['graftpunk.tokens'] = None\n"
        "from tests.unit.session_compat_cases import token_dicts\n"
        f"state = legacy.state_from_pickle(open({str(payload)!r}, 'rb').read()).state\n"
        "print(json.dumps(token_dicts(types.SimpleNamespace(**state))))\n"
    )
    done = subprocess.run(  # noqa: S603
        [sys.executable, "-c", script], capture_output=True, text=True, check=True
    )
    assert json.loads(done.stdout) == case.expected["cached_tokens"]


def test_a_class_that_cannot_be_imported_is_stubbed_and_reported(monkeypatch):
    module = types.ModuleType("absent_pkg_for_legacy_test")
    exec("class Thing:\n    pass\n", module.__dict__)  # noqa: S102
    module.Thing.__module__ = "absent_pkg_for_legacy_test"
    monkeypatch.setitem(sys.modules, "absent_pkg_for_legacy_test", module)
    obj = module.Thing()
    obj.cookies = [1]
    payload = pickle.dumps(obj)
    monkeypatch.delitem(sys.modules, "absent_pkg_for_legacy_test")
    legacy = state_from_pickle(payload)
    assert legacy.unresolved == ("absent_pkg_for_legacy_test.Thing",)
    assert legacy.kind is None and legacy.state == {"cookies": [1]}


def test_the_placeholder_takes_the_dict_and_slot_state_pickle_gives_a_slots_class():
    placeholder = type("WithSlots", (_Capture,), {})()
    placeholder.__setstate__(({"cookies": "c"}, {"headers": "h"}))
    assert (placeholder.cookies, placeholder.headers) == ("c", "h")


def test_the_placeholder_refuses_a_state_it_would_drop():
    placeholder = type("Refuses", (_Capture,), {})()
    with pytest.raises(TypeError, match="cannot restore pickle state"):
        placeholder.__setstate__("not a dict or tuple")


@pytest.mark.parametrize("version", ["1.21.0", "1.40.1", "1.43.1", "1.44.0"])
def test_a_selenium_pickle_reads_where_the_browser_stack_cannot_import(version, tmp_path):
    case = case_named(version, "selenium")
    payload = tmp_path / "payload.pickle"
    payload.write_bytes(decrypt(case))
    script = (
        "import json, sys\n"
        "class Block:\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name.split('.')[0] in ('requestium', 'selenium', 'webdriver_manager'):\n"
        "            raise ModuleNotFoundError(f'No module named {name!r}', name=name)\n"
        "sys.meta_path.insert(0, Block())\n"
        "import graftpunk.session_legacy as legacy\n"
        "from tests.unit.session_compat_cases import cookie_dicts\n"
        f"got = legacy.state_from_pickle(open({str(payload)!r}, 'rb').read())\n"
        "print(json.dumps({'kind': got.kind, 'unresolved': got.unresolved,\n"
        "    'cookies': cookie_dicts(got.state['cookies']),\n"
        "    'headers': [list(i) for i in got.state['headers'].items()]}))\n"
    )
    done = subprocess.run(  # noqa: S603
        [sys.executable, "-c", script], capture_output=True, text=True, check=True
    )
    out = json.loads(done.stdout)
    assert out["kind"] == "browser"
    assert out["cookies"] == case.expected["cookies"]
    assert out["headers"] == case.expected["headers"]
    assert any(p.startswith("requestium.") for p in out["unresolved"]), out["unresolved"]
