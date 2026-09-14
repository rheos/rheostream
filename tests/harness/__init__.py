"""Test-profile harness registrations and the environment-isolation helper.

Everything registered from here carries ``origin = "test_harness"`` and is accepted
only when the resolved ``profile`` is ``test``, so 0c3's production-profile assertion
will find none of it.

``tests/`` has no ``__init__.py``, so pytest puts ``tests/`` itself on ``sys.path`` and
test modules import this package as ``harness`` (``from harness import ...``).

The three absence probes are re-exported here rather than left to
``from harness.absence import ...``, because a probe written without its mandatory
positive control must not be expressible and the shortest import is the one a later
author will reach for.
"""

import os
from pathlib import Path

import pytest

from harness.absence import (
    Covers,
    absent_attribute,
    absent_call,
    absent_token,
    covered_by_probe,
)

__all__ = [
    "RHEO_ENV_PREFIX",
    "Covers",
    "absent_attribute",
    "absent_call",
    "absent_token",
    "covered_by_probe",
    "isolate_rheo_environment",
]

RHEO_ENV_PREFIX = "RHEO_"


def isolate_rheo_environment(monkeypatch: pytest.MonkeyPatch, data_root: Path) -> None:
    """Strip every ``RHEO_*`` variable, then set the test profile and a private data
    root.

    ``RHEO_DATA_ROOT`` is pointed at ``data_root`` so nothing reads or creates the
    developer's real platform application-data directory.
    """
    for name in list(os.environ):
        if name.startswith(RHEO_ENV_PREFIX):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("RHEO_PROFILE", "test")
    monkeypatch.setenv("RHEO_DATA_ROOT", str(data_root))
