"""Module manifests and the entry-point loader: what a distribution declares about
itself, and the allowlist that decides whether it loads at all.

- ``manifest.py`` — ``ModuleManifest`` as a frozen pydantic model, its ten
  declaration types, and its ``WebContribution`` with the ``WebSurface`` and five
  entry types inside it. Not in ``rheo_contracts``, which may not import
  ``UnitOfWork`` (F3).
- ``loader.py`` — ``discovered()`` over the ``rheo.modules`` entry-point group,
  ``allowed_module_ids()`` over the ``modules.installed`` setting,
  ``load_entry_point()`` (one entry point through the three load gates),
  ``load_modules()``,
  ``loaded_manifests()``, ``loaded_in_dependency_order()`` and ``module_surfaces()``.

**No lifecycle.** Install, enable and disable are phase 2's; nothing in release one can
install a module, and the ``core.module_state`` row a workspace needs is written by
hand (run 0v's finding F15). Nothing here writes one.
"""

from rheo_core.modules.loader import (
    ENTRY_POINT_GROUP,
    allowed_module_ids,
    discovered,
    load_entry_point,
    load_modules,
    loaded_in_dependency_order,
    loaded_manifests,
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
    FormDeclaration,
    FrozenMap,
    HealthCheck,
    Importer,
    JobKind,
    ManifestInvalid,
    ModuleManifest,
    NavigationEntry,
    RecordType,
    RecordView,
    Schedule,
    SearchProvider,
    Sensitivity,
    SensitivityTier,
    StorageDeclaration,
    WebContribution,
    WebRoute,
    WebSurface,
)

# The field marker a module's event and output models declare a tier with, published
# beside ``SensitivityTier`` so a module author writes both from the one package that
# declares the module contract (``rheo_core/redaction/tiers.py`` carries the rule).
from rheo_core.redaction.tiers import Tiered as Tiered

# Re-exported explicitly (``X as X``), for the contract reason ``events/consumers.py``
# re-exports ``HandlerUnitOfWork``: a module distribution may not import the core's
# storage package — ``tests/postgres/test_module_storage_ownership.py`` scans
# ``modules/`` for exactly that — and a module whose provider caches a model artifact
# has to *reach* the data root to put it there. So the package that declares the
# module contract publishes the one storage name a module needs, and nothing else from
# the storage package is published here.
from rheo_core.storage.data_root import model_cache_dir as model_cache_dir

__all__ = [
    "ENTRY_POINT_GROUP",
    "ConnectorBinding",
    "DeletionHandler",
    "DeletionParticipant",
    "Dependency",
    "EventDeclaration",
    "ExportDeclaration",
    "Exporter",
    "FormDeclaration",
    "FrozenMap",
    "HealthCheck",
    "Importer",
    "JobKind",
    "ManifestInvalid",
    "ModuleManifest",
    "NavigationEntry",
    "RecordType",
    "RecordView",
    "Schedule",
    "SearchProvider",
    "Sensitivity",
    "SensitivityTier",
    "StorageDeclaration",
    "Tiered",
    "WebContribution",
    "WebRoute",
    "WebSurface",
    "allowed_module_ids",
    "discovered",
    "load_entry_point",
    "load_modules",
    "loaded_in_dependency_order",
    "loaded_manifests",
    "model_cache_dir",
    "module_surfaces",
    "reset_surfaces",
]
