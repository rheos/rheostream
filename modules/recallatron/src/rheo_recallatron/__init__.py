"""Recallatron: memory records (overview.md). A skeleton distribution in 1a0.

``MANIFEST`` is re-exported here because the ``rheo.modules`` entry point names
``rheo_recallatron:MANIFEST`` — the dotted target resolves against this module, not
against ``manifest.py``.
"""

from rheo_recallatron.manifest import MANIFEST

__all__ = ["MANIFEST"]
