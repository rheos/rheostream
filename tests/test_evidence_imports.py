"""The evidence package and the sweep that calls it import in a fresh interpreter.

``rheo_core.work`` imports ``rheo_core.work.schedules`` at package import, and the
sweep calls ``rheo_core.evidence.retention``. If ``schedules`` imported the evidence
package at module level, the evidence package's own imports of ``rheo_core.work``
would find it half-initialised. In-process tests never see that, because the suite
has already imported everything by the time a test runs; a fresh
``python -c "import <module>"`` per module, the ``test_entry_point_imports`` shape,
reds on the cycle in seconds instead of at worker start.
"""

import subprocess
import sys

import pytest

MODULES = (
    "rheo_core.work.schedules",
    "rheo_core.evidence.retention",
    "rheo_core.evidence",
)


@pytest.mark.parametrize("module", MODULES)
def test_evidence_module_imports_in_a_fresh_interpreter(module: str) -> None:
    completed = subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr[-2000:]
    assert completed.stderr == ""
