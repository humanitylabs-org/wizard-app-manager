# Files canonical installation

Files already fills the required `files` slot during `wam bootstrap` (fresh and
alongside). It must remain a bundled default, not an optional post-install repair.
The default repository fallback allows bootstrap to resolve Files even when the
public index cannot resolve the package.

The matching Files recipe declares `view.path: /files/` and starts unmodified
File Browser with `FB_BASEURL=/files`. App Manager's default gateway publishes
`/files/` on the installed shell and every existing private shell alias, preserving
the prefix to the Files front. MCP remains box-only at `/mcp` on the Files port.
The resulting registry URL is relative `/files/`, satisfying the shell's existing
same-origin Open policy. No frame CSP relaxation or Store special-case is needed.
An older pinned recipe declaring `/` keeps its previous port route rather than
being silently moved to an unsupported prefix. Explicit owner/catalog routes
continue to take precedence.

## Release gate

A main merge is not an update of the public installer. A release snapshot must pin
both the new App Manager revision and `oscarhenrycollins/files`' matching recipe
commit before stable installs obtain this behavior. Keep historical release JSON
and `releases/stable` untouched. Existing boxes require the normal approved
installer/update path; reloading shell assets alone cannot update the recipe.
The Files repository's existing main-push workflow reindexes the app catalog;
that is separate from publishing a Wizard OS release or advancing stable.

## Checks

Run `python -m pytest tests/test_files_default.py tests/test_links.py
 tests/test_gateway_shell_aliases.py` (one command, without the newline).
The Files repo also carries packaging-contract tests. Real acceptance should use
an empty media/database and the actual recipe, verify `/files/`, prefix assets,
`/files/health`, box-only `/mcp`, restart persistence, then click Store Open in both
shells. Keep writes confined to the disposable test app, never the owner's Files.
