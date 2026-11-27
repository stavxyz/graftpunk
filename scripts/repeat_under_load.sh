#!/usr/bin/env bash
# Run one pytest node many times while busy loops hold the CPU, and count the
# failures. A test that passes alone and fails in CI often fails here too: a
# shared runner is a loaded machine.
#
# Usage: scripts/repeat_under_load.sh [-l LOOPS] N TEST_ID
#   N        how many times to run TEST_ID (a whole number, at least 1)
#   TEST_ID  a pytest node id, for example
#            tests/unit/test_login_verdict.py::TestVerdictComesLast::test_a_rate_limit_page_is_named
#   -l LOOPS how many busy loops to start (default: four per CPU; one per CPU
#            was too light to reproduce a known 0.2 s login-wait race)
#
# Each run is `uv run -p 3.12 --all-extras pytest`, the interpreter and extras
# the project's own checks use, so a mismatched venv cannot pose as a flake.
# The test is first run once with no load: a test that fails then is broken,
# not flaky, and the script exits 2 without starting the load. Zero failures
# shows little unless the same load reproduced the failure before the fix.
# Prints "runs=N failures=F" and exits 0 when every run passed, 1 when any run
# failed, and 2 on a usage error or an unloaded failure. Ctrl+C stops the busy
# loops and the running test and exits 130.
# Each run's output is kept in a temporary directory, printed at the end.
set -u

usage() {
  echo "usage: scripts/repeat_under_load.sh [-l LOOPS] N TEST_ID" >&2
  exit 2
}
is_count() { case "$1" in '' | *[!0-9]* | 0 | 0*) return 1 ;; *) return 0 ;; esac; }

loops=$(( $(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 4) * 4 ))
if [ "${1:-}" = "-l" ]; then
  [ "$#" -ge 2 ] || usage
  loops="$2"
  shift 2
fi
[ "$#" -eq 2 ] || usage
n="$1"
test_id="$2"
is_count "$n" || { echo "N must be a whole number of at least 1, got: $n" >&2; exit 2; }
is_count "$loops" || { echo "LOOPS must be a whole number of at least 1, got: $loops" >&2; exit 2; }

cd "$(dirname "$0")/.." || exit 2
out="$(mktemp -d)"
pids=()
child=""
stop() {
  [ -n "$child" ] && kill "$child" 2>/dev/null
  [ "${#pids[@]}" -gt 0 ] && kill "${pids[@]}" 2>/dev/null
}
run_once() {
  NO_COLOR=1 PYTHONDONTWRITEBYTECODE=1 uv run -q -p 3.12 --all-extras python -m pytest -q -n 0 \
    -p no:cacheprovider "$test_id" >"$1" 2>&1 &
  child=$!
  wait "$child"
  local code=$?
  child=""
  return "$code"
}
trap stop EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

if ! run_once "$out/unloaded.txt"; then
  echo "the test fails with no load, so this is not a timing failure; see $out/unloaded.txt" >&2
  exit 2
fi

i=0
while [ "$i" -lt "$loops" ]; do
  ( while :; do :; done ) &
  pids+=($!)
  i=$((i + 1))
done

failures=0
i=1
while [ "$i" -le "$n" ]; do
  if ! run_once "$out/run-$i.txt"; then
    failures=$((failures + 1))
  fi
  i=$((i + 1))
done

echo "runs=$n failures=$failures (busy loops: $loops; output in $out)"
[ "$failures" -eq 0 ]
