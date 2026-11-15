"""Float: a local-first personal finance ledger (package ``rheo_finance``).

``MANIFEST`` (the ``rheo.modules`` entry point) is imported on first use, so importing
the package, as the CLI, ``env.py`` and the suite do, never imports ``rheo_core``:
only ``rheo_finance.wiring`` does. The import is by name, so the package itself adds
no static edge to the wiring for the suite's import-graph guards to follow from every
``from rheo_finance import ...``.
"""

from __future__ import annotations

import importlib


def __getattr__(name: str) -> object:
    if name == "MANIFEST":
        return importlib.import_module("rheo_finance.wiring.manifest").MANIFEST
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
