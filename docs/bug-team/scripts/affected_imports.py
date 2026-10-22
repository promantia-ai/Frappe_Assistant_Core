"""affected_imports.py <app dir> <changed module paths...>: prints the code modules (basenames) that
use a changed module, directly or through other modules, by their real import statements.

Shared plumbing that everything imports (the plugin and tool registries, generic helpers) is not
followed, or every test would be selected; tests that name a changed module are still found."""

import ast
import os
import sys

PLUMBING = {
    "__init__", "plugin", "plugins", "utils", "api", "base", "base_tool", "tool_registry", "hooks",
    "helpers", "core", "tools", "constants", "savepoint", "tool_category_detector", "audit_trail",
}

app, changed = sys.argv[1], [os.path.splitext(os.path.basename(p))[0] for p in sys.argv[2:]]
pkg = os.path.join(app, "frappe_assistant_core")

imports = {}  # module basename -> basenames it imports
for root, _dirs, files in os.walk(pkg):
    if "/tests" in root or "node_modules" in root:
        continue
    for f in files:
        if not f.endswith(".py"):
            continue
        try:
            tree = ast.parse(open(os.path.join(root, f), encoding="utf-8").read())
        except (SyntaxError, UnicodeDecodeError):
            continue
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                names.update((node.module or "").split("."))
                names.update(a.name for a in node.names)
            elif isinstance(node, ast.Import):
                for a in node.names:
                    names.update(a.name.split("."))
        imports.setdefault(f[:-3], set()).update(names)

# One level: the changed modules and the modules that import them directly. The import modules
# import each other in a long chain, so following every level selects nearly every test; the
# phase-end full runs cover what is further away.
seen = {c for c in changed if c not in PLUMBING}
for name in list(seen):
    seen.update(m for m, names in imports.items() if name in names and m not in PLUMBING)
print(" ".join(sorted(seen)))
