"""CLI-only developer tooling: the scaffold and the capture-directory helper.

Nothing here is imported by a generated plugin at runtime; that is the
placement rule (plugin tooling spec, 2026-09-11): public API a plugin uses
lives at the top level of the package (``graftpunk.testing``, the request
helpers on ``CommandContext``), and tooling only ``gp`` runs lives here.
"""
