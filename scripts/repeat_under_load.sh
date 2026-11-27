#!/usr/bin/env bash
# Run one pytest node many times while busy loops hold the CPU, and count the
# failures. A test that passes alone and fails in CI often fails here too: a
# shared runner is a loaded machine.
#
# Usage: scripts/repeat_under_load.sh [-l LOOPS] N TEST_ID
#   N        how many times to run TEST_ID
#   TEST_ID  a pytest node id, for example
#            tests/unit/test_login_verdict.py::TestVerdictComesLast::test_a_rate_limit_page_is_named
#   -l LOOPS how many busy loops to start (default: the number of CPUs)
#
# Prints "runs=N failures=F" and exits 1 when any run failed. Each run's output
# is kept in a temporary directory, printed at the end, for reading a failure.
set -u

loops="$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 4)"
if [ "${1:-}" = "-l" ]; then
  loops="${2:?-l needs a number}"
  shift 2
fi
n="${1:?usage: scripts/repeat_under_load.sh [-l LOOPS] N TEST_ID}"
test_id="${2:?usage: scripts/repeat_under_load.sh [-l LOOPS] N TEST_ID}"

cd "$(dirname "$0")/.." || exit 2
out="$(mktemp -d)"
pids=()
stop() { [ "${#pids[@]}" -gt 0 ] && kill "${pids[@]}" 2>/dev/null; }
trap stop EXIT INT TERM
for _ in $(seq "$loops"); do
  ( while :; do :; done ) &
  pids+=($!)
done

failures=0
for i in $(seq "$n"); do
  if ! NO_COLOR=1 PYTHONDONTWRITEBYTECODE=1 uv run --no-sync python -m pytest -q -n 0 \
      -p no:cacheprovider "$test_id" >"$out/run-$i.txt" 2>&1; then
    failures=$((failures + 1))
  fi
done

echo "runs=$n failures=$failures (busy loops: $loops; output in $out)"
[ "$failures" -eq 0 ]
