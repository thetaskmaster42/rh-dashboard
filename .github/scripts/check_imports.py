#!/usr/bin/env python3
"""
Prove the shipping dashboard imports nothing it is not allowed to.

## Why this replaced a filename check

CI used to assert that `requirements.txt` and `pyproject.toml` did not exist.
That was only ever a *proxy* for the rule anyone actually cares about — the
code in `rh_dashboard/` pulls in nothing beyond the standard library and a
declared, deliberate few. The proxy had two failures. It could not see an
`import requests` added to a module (nothing declares it; it just breaks at
run time on a machine that lacks it), and it could not survive the project
gaining a dependency on purpose, which is exactly what DuckDB is.

So this checks the real thing instead: walk every module under `rh_dashboard/`
with `ast`, collect the top-level name of every import, and fail on anything
outside `sys.stdlib_module_names` plus ALLOWED below. It is strictly stronger
than the check it replaces, and it puts the dependency set in one place that
a reviewer can read.

`ast` rather than importing the modules: a static parse cannot execute code,
needs nothing installed, and sees imports inside functions and `TYPE_CHECKING`
blocks that a runtime probe would miss.

Run: python3 .github/scripts/check_imports.py rh_dashboard
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

# Everything `rh_dashboard/` may import beyond the standard library.
# Adding a name here is a deliberate act; that is the whole point of the file.
ALLOWED = {
    # Analytical store. Embedded, single file, no server — see CLAUDE.md.
    "duckdb",
}


def top_level_imports(path: Path) -> set[str]:
    """Every module name this file imports, reduced to its top-level package."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            # `level` > 0 is a relative import — our own package, never a dep.
            if node.level == 0 and node.module:
                names.add(node.module.split(".")[0])
    return names


def main(argv: list[str]) -> int:
    roots = [Path(a) for a in argv[1:]] or [Path("rh_dashboard")]
    stdlib = set(sys.stdlib_module_names)
    problems: list[str] = []
    seen: dict[str, set[str]] = {}

    files = sorted(f for root in roots for f in root.rglob("*.py"))
    if not files:
        print(f"no Python files under {[str(r) for r in roots]}", file=sys.stderr)
        return 1

    for f in files:
        for name in sorted(top_level_imports(f)):
            if name in stdlib or name in ALLOWED:
                seen.setdefault(name, set()).add(f.name)
                continue
            problems.append(f"{f}: imports {name!r}, which is neither standard "
                            f"library nor in the allowlist")

    external = {n: v for n, v in seen.items() if n in ALLOWED}
    print(f"checked {len(files)} file(s) under {[str(r) for r in roots]}")
    if external:
        for name, where in sorted(external.items()):
            print(f"  declared dependency {name!r} used by: {', '.join(sorted(where))}")
    else:
        print("  no non-stdlib imports at all")

    unused = ALLOWED - set(external)
    if unused:
        # Not a failure: a dependency can be declared before the code that
        # needs it lands. Worth saying out loud so it does not linger unnoticed.
        print(f"  note: allowlisted but unused: {', '.join(sorted(unused))}")

    if problems:
        print("\nFAILED:", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 1
    print("every import is stdlib or declared")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
