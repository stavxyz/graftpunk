#!/usr/bin/env bash
# check-skill-version.sh BASE
#
# When this branch's changes since it left BASE touch what an installed skill
# carries (skills/, .claude-plugin/, or the guide the skill reads,
# docs/PLUGIN_DEVELOPMENT.md), the version field of .claude-plugin/plugin.json
# must be higher than BASE's. That version pins an installed plugin: Claude Code
# keeps a GitHub install on that string, so a change without a bump never reaches
# people who already installed it. What counts as this branch's change is
# measured from the merge base, so a base that moved on after the branch point
# is not this branch's change; the version is compared with BASE itself, so a
# bump that another merge already used fails here and not after both land.
# Run by .github/workflows/skill-version.yml on pull requests, and by hand as
# `just skill-version`.
set -euo pipefail

base="${1:?usage: scripts/check-skill-version.sh <base commit>}"
plugin=".claude-plugin/plugin.json"
watched='^(skills/|\.claude-plugin/|docs/PLUGIN_DEVELOPMENT\.md$)'

version_of() {
  python3 -c 'import json, sys; print(json.load(sys.stdin)["version"])'
}

# Separate steps, so a bad BASE fails here under set -e instead of reading as
# "no change".
git merge-base "$base" HEAD > /dev/null
all_changed="$(git diff --name-only "$base"...HEAD)"
changed="$(printf '%s\n' "$all_changed" | grep -E "$watched" || true)"
if [ -z "$changed" ]; then
  echo "skill-version: the skill is unchanged (skills/, .claude-plugin/, and the guide); no bump needed."
  exit 0
fi

head_version="$(version_of < "$plugin")"
base_json="$(git show "$base:$plugin" 2>/dev/null || true)"
if [ -z "$base_json" ]; then
  echo "skill-version: $plugin is new at $head_version."
  exit 0
fi
base_version="$(printf '%s' "$base_json" | version_of)"
# Higher when every part of both is a number and the parts compare greater;
# otherwise the two must at least differ. The next patch is named when the last
# part is a number.
verdict="$(python3 - "$base_version" "$head_version" <<'PY'
import sys

base, head = sys.argv[1], sys.argv[2]
parts = lambda v: v.split(".")
numeric = all(p.isdigit() for p in parts(base) + parts(head))
if numeric:
    higher = tuple(map(int, parts(head))) > tuple(map(int, parts(base)))
else:
    higher = head != base
last = parts(base)[-1]
following = ".".join([*parts(base)[:-1], str(int(last) + 1)]) if last.isdigit() else "a higher version"
print(("ok" if higher else "low") + " " + following)
PY
)"
if [ "${verdict%% *}" != ok ]; then
  echo "skill-version: the skill changed, but $plugin is $head_version and the base is at $base_version. Bump it to ${verdict#* }." >&2
  exit 1
fi
echo "skill-version: $base_version -> $head_version"
