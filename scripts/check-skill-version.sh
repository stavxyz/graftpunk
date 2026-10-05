#!/usr/bin/env bash
# check-skill-version.sh BASE
#
# When this branch's changes since it left BASE touch skills/ or .claude-plugin/,
# the version field of .claude-plugin/plugin.json must differ from the merge
# base's. That version pins an installed plugin: Claude Code keeps a GitHub install
# on that string, so a skill change without a bump never reaches people who
# already installed it. Comparing against the merge base, not BASE's tip, keeps
# a base branch that moved on after the branch point out of this branch's result.
# Run by .github/workflows/skill-version.yml on pull requests, and by hand as
# `just skill-version`.
set -euo pipefail

base="${1:?usage: scripts/check-skill-version.sh <base commit>}"
plugin=".claude-plugin/plugin.json"

version_of() {
  python3 -c 'import json, sys; print(json.load(sys.stdin)["version"])'
}

# Separate steps, so a bad BASE fails here under set -e instead of reading as
# "no change".
merge_base="$(git merge-base "$base" HEAD)"
all_changed="$(git diff --name-only "$base"...HEAD)"
changed="$(printf '%s\n' "$all_changed" | grep -E '^(skills|\.claude-plugin)/' || true)"
if [ -z "$changed" ]; then
  echo "skill-version: nothing under skills/ or .claude-plugin/ changed; no bump needed."
  exit 0
fi

head_plugin="$(version_of < "$plugin")"
base_json="$(git show "$merge_base:$plugin" 2>/dev/null || true)"
if [ -z "$base_json" ]; then
  echo "skill-version: $plugin is new at $head_plugin."
  exit 0
fi
base_version="$(printf '%s' "$base_json" | version_of)"
if [ "$head_plugin" = "$base_version" ]; then
  next="$(python3 -c 'import sys; p = sys.argv[1].split("."); p[-1] = str(int(p[-1]) + 1); print(".".join(p))' "$base_version")"
  echo "skill-version: skills/ or .claude-plugin/ changed, but the version is still $base_version. Bump $plugin to $next." >&2
  exit 1
fi
echo "skill-version: $base_version -> $head_plugin"
