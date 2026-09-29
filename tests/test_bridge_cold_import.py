"""The bridge imports cold, with no deployment environment (spec § A6, AC 14).

The worker runs from a checkout's venv on a laptop that has no ``RHEO*``
settings. Importing the bridge and the core sanitizer it reuses must open no
connection and read no deployment variable, which an in-process test cannot
show: the suite has already imported everything and set its own environment. A
fresh interpreter with every ``RHEO*`` variable removed can.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

from rheo_bridge import hook

# Prompt 12 adds ``rheo_bridge.worker`` here once it exists.
BRIDGE_MODULES = ("rheo_bridge.state", "rheo_bridge.client")


def _cold_env() -> dict[str, str]:
    # ``RHEO`` covers ``RHEO_TEST_CLUSTER_DSN`` too.
    return {
        key: value for key, value in os.environ.items() if not key.startswith("RHEO")
    }


def test_the_bridge_and_the_sanitizer_import_with_no_rheo_environment() -> None:
    env = _cold_env()
    assert "RHEO_TEST_CLUSTER_DSN" not in env
    program = (
        "".join(f"import {module}\n" for module in BRIDGE_MODULES)
        + "from rheo_core.evidence.sanitize import sanitize\n"
        + 'sanitize("hello")\n'
    )
    completed = subprocess.run(
        [sys.executable, "-c", program],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr[-2000:]
    assert completed.stderr == ""


def test_the_hook_still_imports_only_the_standard_library() -> None:
    # The regression guard from the hook's own tests, repeated now that the
    # wider package sits beside it.
    tree = ast.parse(Path(hook.__file__).read_text(encoding="utf-8"))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "hook.py must not use relative imports"
            assert node.module is not None
            roots.add(node.module.split(".")[0])
    assert roots, "parsed no imports; the check is not looking at hook.py"
    allowed = set(sys.stdlib_module_names) | {"__future__"}
    assert roots <= allowed, roots - allowed
