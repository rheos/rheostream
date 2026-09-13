"""``MANIFEST``: what the ``rheo.modules`` entry point loads to.

``modules/spike/pyproject.toml`` publishes this object under the entry-point name
``spike``, which is also :data:`~rheo_spike.operations.MODULE_ID`. The loader matches
``RHEO_MODULES`` against the *name* before importing anything, then refuses a manifest
whose ``module_id`` disagrees with it — so the two must stay equal, and
``test_the_entry_point_name_is_the_module_id`` pins that rather than leaving it to
care.

Nothing is registered by importing this module. ``load_modules()`` validates the
manifest and then registers its operations and its resolver through the shipped
``OperationRegistry.register`` / ``ResolverRegistry.register`` with ``origin =
"spike"``, and installs :data:`~rheo_spike.audit.SINK` under that same module id.
"""

from importlib.metadata import PackageNotFoundError, version
from typing import Final

from rheo_core.modules import ModuleManifest, WebSurface

from rheo_spike.audit import SINK
from rheo_spike.operations import (
    MODULE_ID,
    NOTE_RECORD_TYPE,
    OPERATIONS,
    resolve_note,
)
from rheo_spike.records import create_schema

DISTRIBUTION: Final = "rheo-spike"


def _package_version() -> str:
    """The installed distribution's version, or ``"0"`` when it is not installed.

    The fallback is for a checkout running straight from ``src`` with no dist-info;
    the version only ever lands in a ``core.module_state`` row, and a missing one is
    not worth an import-time crash in a throwaway module.
    """
    try:
        return version(DISTRIBUTION)
    except PackageNotFoundError:
        return "0"


MANIFEST: Final = ModuleManifest(
    module_id=MODULE_ID,
    package_version=_package_version(),
    schema_name=MODULE_ID,
    create_schema=create_schema,
    operations=OPERATIONS,
    resolvers=((NOTE_RECORD_TYPE, resolve_note),),
    web=WebSurface(surface="spike", host="spike", path="/spike"),
    audit_sink=SINK,
)
