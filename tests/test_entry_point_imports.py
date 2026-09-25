"""Every process entry point imports in a fresh interpreter (#142).

The core package has import cycles that complete only when ``rheo_core.operations``
is imported first. In-process tests never see that: by the time any test runs, the
suite has already imported ``rheo_core.operations``, so an entry point that starts
its imports from the wrong end passes every test and then dies at ``python -m`` in
production. ``rheo_app_worker.main`` did exactly that from 2026-09-17 until #142.

Each module below is the thing a process or the image's smoke check actually
imports. A fresh ``python -c "import <module>"`` per module is the only way to
exercise the order a real start uses.
"""

import subprocess
import sys

import pytest

ENTRY_POINTS = (
    "rheo_app_worker.main",
    "rheo_app_core.main",
    "rheo_app_core.serve",
    "rheo_app_cli.main",
    "rheo_app_mcp.transport",
)


@pytest.mark.parametrize("module", ENTRY_POINTS)
def test_entry_point_imports_in_a_fresh_interpreter(module: str) -> None:
    completed = subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr[-2000:]
