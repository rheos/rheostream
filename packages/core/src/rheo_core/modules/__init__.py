"""Module manifests and the entry-point loader: what a distribution declares about
itself, and the allowlist that decides whether it loads at all.

- ``manifest.py`` — ``ModuleManifest`` as a frozen pydantic model, its ten
  declaration types and its ``WebSurface``. Not in ``rheo_contracts``, which may not
  import ``UnitOfWork`` (F3).
- ``loader.py`` — ``discovered()`` over the ``rheo.modules`` entry-point group,
  ``allowed_module_ids()`` over ``RHEO_MODULES``, ``load_modules()``, and
  ``module_surfaces()``.

**No lifecycle.** Install, enable and disable are phase 2's; nothing in release one can
install a module, and the ``core.module_state`` row a workspace needs is written by
hand (run 0v's finding F15). Nothing here writes one.
"""

from rheo_core.modules.loader import (
    ALLOWLIST_VARIABLE,
    ENTRY_POINT_GROUP,
    allowed_module_ids,
    discovered,
    load_modules,
    module_surfaces,
    reset_surfaces,
)
from rheo_core.modules.manifest import (
    ConnectorBinding,
    DeletionHandler,
    DeletionParticipant,
    Dependency,
    EventDeclaration,
    ExportDeclaration,
    Exporter,
    HealthCheck,
    Importer,
    JobKind,
    ManifestInvalid,
    ModuleManifest,
    RecordType,
    Schedule,
    SensitivityTier,
    StorageDeclaration,
    WebSurface,
)

__all__ = [
    "ALLOWLIST_VARIABLE",
    "ENTRY_POINT_GROUP",
    "ConnectorBinding",
    "DeletionHandler",
    "DeletionParticipant",
    "Dependency",
    "EventDeclaration",
    "ExportDeclaration",
    "Exporter",
    "HealthCheck",
    "Importer",
    "JobKind",
    "ManifestInvalid",
    "ModuleManifest",
    "RecordType",
    "Schedule",
    "SensitivityTier",
    "StorageDeclaration",
    "WebSurface",
    "allowed_module_ids",
    "discovered",
    "load_modules",
    "module_surfaces",
    "reset_surfaces",
]
