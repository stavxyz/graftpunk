"""Plugin scaffolding: turns a ScaffoldSpec into files, an existing repository
into a new plugin, and an existing pyproject.toml into one that declares it.

CLI-only: ``gp plugin new`` is the only caller. Nothing here is imported by
a generated plugin at runtime (plugin tooling spec, 2026-09-11).
"""
