#!/usr/bin/env bash
# smoke_install.sh LITE_WHEEL FULL_WHEEL [VENV_ROOT]
#
# The lite-install contract, checked against built wheels: each wheel goes into
# its own clean venv under VENV_ROOT (a temporary directory, removed afterwards,
# when VENV_ROOT is not given; VENV_ROOT/lite and VENV_ROOT/full are recreated on
# every run); graftpunk-lite (no extras) must import, keep a working gp CLI,
# load a cached session browser-free and name its own extra in a missing-browser
# error, all without the browser stack, and graftpunk must have that stack. Run
# from the repository root (it reads tests/fixtures).
# Used by the "Lite install" job in python-quality.yml and by the release build
# before anything is published, so both check the same things.
set -euo pipefail

usage="usage: scripts/smoke_install.sh LITE_WHEEL FULL_WHEEL [VENV_ROOT]"
lite_wheel="${1:?$usage}"
full_wheel="${2:?$usage}"
if [ -n "${3:-}" ]; then
  root="$3"
else
  root="$(mktemp -d)"
  trap 'rm -rf "$root"' EXIT
fi
python="${UV_PYTHON:-3.12}"

rm -rf "$root/lite" "$root/full"
uv venv -q --python "$python" "$root/lite"
uv pip install -q --python "$root/lite/bin/python" "$lite_wheel"
uv venv -q --python "$python" "$root/full"
uv pip install -q --python "$root/full/bin/python" "$full_wheel"
lite="$root/lite/bin"
full="$root/full/bin"

echo "== lite lacks the browser stack; graftpunk has it"
check='
import importlib.util, sys
mods = ["selenium", "nodriver", "requestium", "webdriver_manager",
        "undetected_chromedriver", "selenium_stealth", "httpie"]
found = [m for m in mods if importlib.util.find_spec(m) is not None]
want = sys.argv[1] == "present"
assert found == (mods if want else []), f"{sys.argv[1]}: found {found}"
print(sys.argv[1], "OK:", ", ".join(mods))
'
"$lite/python" -c "$check" absent
"$full/python" -c "$check" present

echo "== import graftpunk without pulling the browser stack"
"$lite/python" - <<'PY'
import sys
import graftpunk
from graftpunk import load_session_for_api_from_bytes  # noqa: F401
for mod in ("graftpunk.session", "graftpunk.stealth", "selenium"):
    assert mod not in sys.modules, f"{mod} eagerly imported by `import graftpunk`"
assert graftpunk.__version__ != "0.0.0+unknown", graftpunk.__version__
print("import graftpunk OK", graftpunk.__version__)
PY

echo "== the CLI starts (regression: gp was broken on browser-free installs)"
"$lite/gp" --help > /dev/null
"$full/gp" --help > /dev/null
lite_version="$("$lite/gp" version)"
case "$lite_version" in
  *"graftpunk-lite "[0-9]*) echo "gp version names graftpunk-lite OK" ;;
  *) printf 'gp version on graftpunk-lite does not name it:\n%s\n' "$lite_version" >&2; exit 1 ;;
esac
"$lite/python" - <<'PY'
import subprocess, sys
# The CLI import itself must not drag in the browser stack.
code = ("import graftpunk.cli.main, sys; "
        "assert 'graftpunk.session' not in sys.modules; "
        "assert 'httpie' not in sys.modules; print('cli import OK')")
out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
assert out.returncode == 0, out.stderr
print(out.stdout.strip())
PY

echo "== browser-free session load (A4 fixture round-trip)"
"$lite/python" - <<'PY'
import pathlib
from graftpunk import load_session_for_api_from_bytes
fix = pathlib.Path("tests/fixtures")
enc = (fix / "browserfree_session.enc").read_bytes()
key = (fix / "browserfree_session.key").read_bytes().strip()
api = load_session_for_api_from_bytes(enc, key=key)
cookies = {c.name: c.value for c in api.cookies}
assert cookies.get("User") == "dummyuser", cookies
assert cookies.get("Password") == "dummypass", cookies
assert "Mozilla/5.0 (dummy)" in api.headers["User-Agent"]
print("browser-free session load OK:", sorted(cookies))
PY

echo "== a missing-browser error names lite's extra"
"$lite/python" - <<'PY'
import graftpunk
try:
    getattr(graftpunk, "BrowserSession")
except ImportError as exc:
    assert "graftpunk-lite[browser]" in str(exc), exc
    print("actionable error OK")
else:
    raise AssertionError("expected ImportError on graftpunk-lite without [browser]")
PY

echo "smoke_install: graftpunk-lite and graftpunk OK"
