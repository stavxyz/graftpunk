#!/usr/bin/env bash
# smoke_install.sh LITE_WHEEL FULL_WHEEL [VENV_ROOT]
#
# The lite-install contract, checked against built wheels: each wheel goes into
# its own clean venv under VENV_ROOT (a temporary directory, removed afterwards,
# when VENV_ROOT is not given; VENV_ROOT/lite, VENV_ROOT/full and
# VENV_ROOT/lite-config are recreated on every run); graftpunk-lite (no extras)
# must import, keep a working gp CLI (gp version, gp session list and
# gp plugins), load a cached session browser-free and name its own extra in a
# missing-browser error, all without the browser stack, and graftpunk must have
# that stack. Sessions the full install saves, as a pickle and as a record,
# are then used from lite against a local server that echoes the Cookie
# header: plugin commands, gp http, a cookie write-back, copy_session and
# login; a pickle of an API session or a plain requests.Session is written back
# and copied on lite. Run from the repository root (it reads tests/fixtures).
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
  remove_root=1
fi
server_pid=""
cleanup() {
  if [ -n "$server_pid" ]; then kill "$server_pid" 2> /dev/null || true; fi
  if [ -n "${remove_root:-}" ]; then rm -rf "$root"; fi
}
trap cleanup EXIT
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
from graftpunk._distribution import BROWSER_MODULES, has_browser_stack
mods = list(BROWSER_MODULES)
found = [m for m in mods if importlib.util.find_spec(m) is not None]
want = sys.argv[1] == "present"
assert found == (mods if want else []), f"{sys.argv[1]}: found {found}"
assert has_browser_stack() is want, sys.argv[1]
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

echo "== gp session list and gp plugins run on lite"
# An empty config directory of the run's own, so the user's sessions and
# settings are neither read nor written.
lite_config="$root/lite-config"
rm -rf "$lite_config"
GRAFTPUNK_CONFIG_DIR="$lite_config" GRAFTPUNK_STORAGE_BACKEND=local \
  "$lite/gp" session list > /dev/null
GRAFTPUNK_CONFIG_DIR="$lite_config" GRAFTPUNK_STORAGE_BACKEND=local \
  "$lite/gp" plugins > /dev/null
echo "gp session list and gp plugins OK"
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
fix = pathlib.Path("tests/fixtures/session_compat/legacy")
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


echo "== a stored session is used from lite, as a pickle and as a record"
# The full install saves the sessions (the lite install cannot pickle a browser
# session); the lite install reads them and writes them back. Everything lives
# in a config directory of this run's own.
store="$root/store-config"
rm -rf "$store"
mkdir -p "$store/plugins"
in_store() {
  env -u GRAFTPUNK_SESSION_FORMAT GRAFTPUNK_CONFIG_DIR="$store" \
    GRAFTPUNK_STORAGE_BACKEND=local NO_COLOR=1 "$@"
}
port_file="$root/echo-port"
rm -f "$port_file"
"$lite/python" - "$port_file" <<'PY' &
import http.server, json, pathlib, sys

class Echo(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"cookie": self.headers.get("Cookie", "")}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass

server = http.server.HTTPServer(("127.0.0.1", 0), Echo)
pathlib.Path(sys.argv[1]).write_text(str(server.server_address[1]))
server.serve_forever()
PY
server_pid=$!
for _ in $(seq 1 100); do
  [ -s "$port_file" ] && break
  sleep 0.1
done
[ -s "$port_file" ] || { echo "echo server did not start" >&2; exit 1; }
base="http://127.0.0.1:$(cat "$port_file")"

seed='
import sys
from requests.cookies import RequestsCookieJar, create_cookie
from graftpunk.cache import cache_session
from graftpunk.session import BrowserSession

jar = RequestsCookieJar()
jar.set_cookie(create_cookie("sid", "stored-sid", domain="127.0.0.1", rest={}))
for name in sys.argv[1:]:
    state = {
        "_backend_type": "nodriver", "_use_stealth": False, "cookies": jar,
        "headers": {"User-Agent": "smoke/1"}, "session_name": name,
        "current_url": "https://myshop.example/account",
        "_gp_cached_tokens": {}, "_gp_header_roles": {},
        "_gp_header_role_origins": {}, "_gp_account_identifier": None,
    }
    session = BrowserSession.__new__(BrowserSession)
    session.__setstate__(state)
    cache_session(session, name)
'
in_store env GRAFTPUNK_SESSION_FORMAT=pickle "$full/python" -c "$seed" \
  shoppkl refpkl cpypkl > /dev/null
in_store env GRAFTPUNK_SESSION_FORMAT=record "$full/python" -c "$seed" \
  shoprec refrec > /dev/null

stored_format() {
  in_store "$full/python" - "$store/sessions/$1/session.pickle" <<'PY'
import pathlib, sys
from graftpunk.encryption import decrypt_data
plain = decrypt_data(pathlib.Path(sys.argv[1]).read_bytes())
print("record" if plain[:1] == b"{" else "pickle")
PY
}

for slot in shoppkl shoprec; do
  cat > "$store/plugins/$slot.yaml" <<YAML
site_name: $slot
base_url: "$base"
commands:
  echo:
    url: "/"
YAML
done
for slot in refpkl refrec; do
  cat > "$store/plugins/$slot.py" <<PYPLUGIN
from graftpunk.plugins.cli_plugin import SitePlugin, command


class Refresh(SitePlugin):
    site_name = "$slot"
    base_url = "$base"
    session_name = "$slot"
    requires_session = True

    @command(help="Show the Cookie header the server saw")
    def show(self, ctx):
        return ctx.session.get(ctx.base_url + "/").json()

    @command(help="Refresh a cookie and save the session", saves_session=True)
    def refresh(self, ctx):
        ctx.session.cookies.set("sid", "refreshed-sid", domain="127.0.0.1", path="/")
        return {"refreshed": True}
PYPLUGIN
done

expect_in() {  # expect_in LABEL NEEDLE OUTPUT
  case "$3" in
    *"$2"*) echo "$1 OK" ;;
    *) printf '%s: expected %s in:\n%s\n' "$1" "$2" "$3" >&2; exit 1 ;;
  esac
}
expect_not_in() {  # expect_not_in LABEL NEEDLE OUTPUT
  case "$3" in
    *"$2"*) printf '%s: unexpected %s in:\n%s\n' "$1" "$2" "$3" >&2; exit 1 ;;
    *) ;;
  esac
}
gp_lite() { in_store "$lite/gp" "$@" 2>&1; }
# gp_out ARGS...: run gp on lite, leave its output in $out, and fail with the
# output shown when it exits non-zero.
gp_out() {
  local status=0
  out="$(gp_lite "$@")" || status=$?
  if [ "$status" != 0 ]; then
    printf 'FAIL: gp %s exited %s:\n%s\n' "$*" "$status" "$out" >&2
    exit 1
  fi
}

for slot in shoppkl shoprec; do
  gp_out "$slot" echo
  expect_in "plugin command with a stored $slot session" "sid=stored-sid" "$out"
  expect_not_in "plugin command with a stored $slot session" "browser automation stack" "$out"
  gp_out http get --session "$slot" "$base/"
  expect_in "gp http with a stored $slot session" "sid=stored-sid" "$out"
  expect_not_in "gp http with a stored $slot session" "browser automation stack" "$out"
done

# The ambient session (gp session use writes .gp-session in the working
# directory; GRAFTPUNK_SESSION overrides it) loads on lite too.
ambient="$root/ambient-cwd"
rm -rf "$ambient"
mkdir -p "$ambient"
out="$(cd "$ambient" && gp_lite session use shoppkl)" || { printf 'gp session use failed on lite:\n%s\n' "$out" >&2; exit 1; }
out="$(cd "$ambient" && gp_lite http get "$base/")" || { printf 'gp http with .gp-session failed on lite:\n%s\n' "$out" >&2; exit 1; }
expect_in "gp http with a session from gp session use" "sid=stored-sid" "$out"
gp_out_env=$(GRAFTPUNK_SESSION=shoprec gp_lite http get "$base/") || { printf 'gp http with GRAFTPUNK_SESSION failed on lite:\n%s\n' "$gp_out_env" >&2; exit 1; }
expect_in "gp http with a session from GRAFTPUNK_SESSION" "sid=stored-sid" "$gp_out_env"

[ "$(stored_format refrec)" = "record" ] || { echo "refrec is not stored as a record" >&2; exit 1; }
gp_out refrec refresh
expect_in "refresh on a stored record exits 0" "refreshed" "$out"
[ "$(stored_format refrec)" = "record" ] || { echo "refresh rewrote the record as a pickle" >&2; exit 1; }
echo "write-back of a stored record stays a record OK"
gp_out refrec show
expect_in "the next run sends the refreshed cookie" "sid=refreshed-sid" "$out"

[ "$(stored_format refpkl)" = "pickle" ] || { echo "refpkl is not stored as a pickle" >&2; exit 1; }
gp_out refpkl refresh
expect_in "refresh on a stored pickle skips the save" "session_save_skipped_load_failed" "$out"
[ "$(stored_format refpkl)" = "pickle" ] || { echo "the skipped save changed the pickle" >&2; exit 1; }

before="$(shasum -a 256 "$store/sessions/cpypkl/session.pickle")"
in_store "$lite/python" - <<'PY'
from graftpunk.cache import copy_session
try:
    copy_session("cpypkl", "cpypkl@alice", account_identifier="alice")
except ImportError as exc:
    print("copy_session on a stored pickle raises ImportError OK")
else:
    raise AssertionError("copy_session on a stored pickle did not raise on lite")
PY
after="$(shasum -a 256 "$store/sessions/cpypkl/session.pickle")"
[ "$before" = "$after" ] || { echo "copy_session changed the source slot" >&2; exit 1; }
[ ! -e "$store/sessions/cpypkl@alice" ] || { echo "copy_session left a destination slot" >&2; exit 1; }
echo "copy_session leaves the source slot unchanged OK"

# A pickle of an API session or of a plain requests.Session names no browser
# class, so lite writes it back and copies it, as a pickle.
in_store env GRAFTPUNK_SESSION_FORMAT=pickle "$full/python" - <<'PY' > /dev/null
import requests
from requests.cookies import create_cookie
from graftpunk.cache import cache_session
from graftpunk.graftpunk_session import GraftpunkSession

for cls, tag in ((GraftpunkSession, "api"), (requests.Session, "req")):
    for slot in (f"{tag}wb", f"{tag}cp"):
        session = cls()
        session.cookies.set_cookie(create_cookie("sid", "stored-sid", domain="127.0.0.1"))
        cache_session(session, slot)
PY
for tag in api req; do
  before="$(shasum -a 256 "$store/sessions/${tag}wb/session.pickle")"
  in_store "$lite/python" - "$tag" <<'PY'
import sys
from graftpunk.cache import copy_session, load_session_for_api, update_session_cookies

tag = sys.argv[1]
api = load_session_for_api(f"{tag}wb")
api.cookies.set("sid", "refreshed-sid", domain="127.0.0.1", path="/")
update_session_cookies(api, f"{tag}wb")
copy_session(f"{tag}cp", f"{tag}cp@alice", account_identifier="alice")
print(tag, "pickle: write-back and copy_session on lite OK")
PY
  after="$(shasum -a 256 "$store/sessions/${tag}wb/session.pickle")"
  [ "$before" != "$after" ] || { echo "the $tag pickle was not written back on lite" >&2; exit 1; }
  [ "$(stored_format "${tag}wb")" = "pickle" ] || { echo "the $tag write-back changed the format" >&2; exit 1; }
  [ -e "$store/sessions/${tag}cp@alice/session.pickle" ] || { echo "copy_session of the $tag pickle wrote no copy" >&2; exit 1; }
done

cat > "$store/plugins/loginhint.py" <<PYPLUGIN
from graftpunk.plugins.cli_plugin import LoginConfig, LoginStep, SitePlugin, command


class LoginHint(SitePlugin):
    site_name = "loginhint"
    base_url = "$base"
    session_name = "loginhint"
    backend = "nodriver"
    requires_session = True
    login_config = LoginConfig(
        url="/login", steps=[LoginStep(fields={"username": "#user"}, submit="#go")]
    )

    @command(help="Unused")
    def noop(self, ctx):
        return {}
PYPLUGIN
status=0
out="$(LOGINHINT_USERNAME=u gp_lite loginhint login)" || status=$?
[ "$status" = 1 ] || { printf 'login exited %s, expected 1:\n%s\n' "$status" "$out" >&2; exit 1; }
expect_in "login prints the install hint" "graftpunk-lite[browser]" "$out"

echo "smoke_install: graftpunk-lite and graftpunk OK"
