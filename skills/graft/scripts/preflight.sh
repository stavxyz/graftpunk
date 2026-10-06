#!/usr/bin/env bash
# Preflight for /graftpunk:graft. Checks that gp is installed, asks gp in one call
# whether it is new enough (--at-least) and whether it writes the payloads this
# skill reads at the schemas it reads (--contract), and relays gp plugin info
# --json. On success prints
#   {"installation": <gp version --json>, "project": <gp plugin info --json>}
# and exits 0. Otherwise prints one message on stderr, naming what failed and
# what to do, and exits 1. The failures, each with its own message:
#   gp is not on PATH
#   uv is not on PATH (the skill runs every command that loads the plugin through
#     uv run; preflight only looks for it)
#   gp version exited 1 (graftpunk older than this skill needs, or gp could not
#     read the floor or a --contract value this script passed)
#   gp reports a contract mismatch (gp's own lines name the older side)
#   gp could not read the project in this directory
#   gp rejected an option this script passed
#   gp version exited with a status this script does not expect
# It compares nothing and parses no JSON: gp answers by its exit status, and the
# JSON is relayed exactly as gp printed it.
set -u

SKILL_REQUIRES_GRAFTPUNK="1.18.0"
SKILL_READS_INFO_SCHEMA=1
SKILL_READS_ENDPOINTS_SCHEMA=1

INSTALL_LINE="uv tool install graftpunk   (or: pip install graftpunk)"
UPGRADE_LINE="uv tool upgrade graftpunk   (or: pip install --upgrade graftpunk)"
UV_INSTALL_PAGE="https://docs.astral.sh/uv/"
SKILL_UPDATE_LINE="/plugin marketplace update graftpunk, then update graftpunk on the Installed tab of /plugin (or run: claude plugin update graftpunk@graftpunk)"

if ! command -v gp >/dev/null 2>&1; then
  printf 'graftpunk is not installed: gp is not on PATH.\nInstall it: %s\n' "$INSTALL_LINE" >&2
  exit 1
fi

if ! command -v uv >/dev/null 2>&1; then
  printf 'uv is not on PATH. The skill runs every command that loads the plugin through uv run.\n' >&2
  printf 'Install it: %s\n' "$UV_INSTALL_PAGE" >&2
  exit 1
fi

errfile="$(mktemp)" || { printf 'preflight: mktemp could not create a temporary file.\n' >&2; exit 1; }
trap 'rm -f "$errfile"' EXIT

# Exit 2 from gp means an option it does not know, and nothing else: gp reports
# an unreadable value with exit 1 and a message of its own.
option_rejected() {
  printf 'gp rejected an option preflight passed (%s).\n' "$1" >&2
  printf 'Either the installed graftpunk is older than %s, or this skill passed a misspelled flag.\n' \
    "$SKILL_REQUIRES_GRAFTPUNK" >&2
  printf 'Upgrade first: %s\nIf it still fails after upgrading, the skill has a bug; report this message.\n' \
    "$UPGRADE_LINE" >&2
  cat "$errfile" >&2
  exit 1
}

installation="$(gp version --json --at-least "$SKILL_REQUIRES_GRAFTPUNK" \
  --contract "info=$SKILL_READS_INFO_SCHEMA" \
  --contract "endpoints=$SKILL_READS_ENDPOINTS_SCHEMA" 2>"$errfile")"
status=$?
case "$status" in
  0) ;;
  1)
    printf 'gp version refused (exit 1): the installed graftpunk is older than %s, or gp\n' \
      "$SKILL_REQUIRES_GRAFTPUNK" >&2
    printf 'could not read the version floor or a --contract value this skill passed.\n' >&2
    if [ -s "$errfile" ]; then
      printf "gp's message says which:\n" >&2
      cat "$errfile" >&2
    fi
    printf 'When graftpunk is older, upgrade it: %s\n' "$UPGRADE_LINE" >&2
    exit 1
    ;;
  2) option_rejected "gp version --json --at-least --contract" ;;
  3)
    printf 'graftpunk and this skill read a payload at different schemas:\n' >&2
    cat "$errfile" >&2
    printf 'When graftpunk is the older side, upgrade it: %s\n' "$UPGRADE_LINE" >&2
    printf 'When this skill is the older side, update it: %s\n' "$SKILL_UPDATE_LINE" >&2
    exit 1
    ;;
  *)
    printf 'gp version exited %s, which preflight does not expect:\n%s\n' "$status" "$installation" >&2
    cat "$errfile" >&2
    exit 1
    ;;
esac

project="$(gp plugin info --json 2>"$errfile")"
status=$?
case "$status" in
  0) ;;
  2) option_rejected "gp plugin info --json" ;;
  *)
    printf 'gp could not read the project in this directory:\n%s\n' "$project" >&2
    cat "$errfile" >&2
    exit 1
    ;;
esac

printf '{"installation": %s, "project": %s}\n' "$installation" "$project"
