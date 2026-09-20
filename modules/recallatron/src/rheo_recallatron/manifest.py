"""Recallatron's ``ModuleManifest``: a real distribution that declares nothing yet.

This is the skeleton half of FR 3. Every one of the manifest's twenty-four fields is
written out here, including the eight whose consumers no run has built — they are
present and empty rather than omitted, so the run that builds a consumer reads a
declaration somebody chose rather than a default nobody did. Nothing installs or
enables this module yet: no ``modules.installed`` entry names it, so
``load_modules()`` discovers the entry point and walks past it.

**Empty on purpose, not unfinished.** The memory record types, their tables, their
operations and their audit sink are run 1a1's and 1a2's work. Adding one here would
build ahead of a requirement no criterion in this run states, and a later run would
have to unbuild it. What this file has to get right instead is the shape: an entry
point that resolves, a manifest that validates, and a storage declaration that points
at a migration chain which really exists.
"""

from importlib import metadata
from typing import Final, NoReturn

from rheo_contracts import CONTRACT_VERSION
from rheo_core.modules.manifest import (
    ExportDeclaration,
    ModuleManifest,
    StorageDeclaration,
)

MODULE_ID: Final = "recallatron"
DISTRIBUTION: Final = "rheo-recallatron"


def _export_unavailable(*_args: object, **_kwargs: object) -> NoReturn:
    """Stands in for the exporter until this module owns a record to export.

    ``ExportDeclaration`` requires both callables, and ``export`` is a required
    manifest field, so the skeleton has to supply a pair. Nothing calls either one:
    ``exports/artifact.py`` reaches a module's exporter through its record types, and
    ``record_types`` is empty here.
    """
    raise NotImplementedError(
        f"{MODULE_ID} declares no record types yet, so it has nothing to export"
    )


def _import_unavailable(*_args: object, **_kwargs: object) -> NoReturn:
    """The other half of the pair; see :func:`_export_unavailable`."""
    raise NotImplementedError(
        f"{MODULE_ID} declares no record types yet, so it has nothing to import"
    )


MANIFEST: Final = ModuleManifest(
    module_id=MODULE_ID,
    # Read from the installed distribution's own metadata rather than repeated as a
    # literal, the way ``provisioning.core_version()`` already reads ``rheo-core``'s.
    # A manifest is only ever reached through the entry point, which exists only for
    # an installed distribution, so the lookup cannot fail where this is read.
    # The bound, because "cannot drift" would be too strong a word for it: this
    # reports what is INSTALLED. Bump ``pyproject.toml``'s ``version`` and run under
    # ``uv run --no-sync`` and it still reports the previous one until a sync
    # reinstalls the distribution. That is a property of every installed-metadata
    # read rather than of this call, and it is still the better source — a literal
    # here drifts silently and permanently instead of until the next sync.
    package_version=metadata.version(DISTRIBUTION),
    core_contract_versions=(CONTRACT_VERSION,),
    dependencies=(),
    record_types=(),
    storage=StorageDeclaration(
        schema_name=MODULE_ID,
        migrations_path="rheo_recallatron.migrations",
        required_extensions=(),
    ),
    configuration_schema=(),
    operations=(),
    tools=(),
    events=(),
    subscriptions=(),
    jobs=(),
    schedules=(),
    resolvers=(),
    deletion_participants=(),
    export=ExportDeclaration(
        format_version=1,
        schema_path="export.schema.json",
        exporter=_export_unavailable,
        importer=_import_unavailable,
    ),
    web=None,
    agent_guidance=None,
    secret_scopes=(),
    connector_bindings=(),
    health_checks=(),
    contract_tests="modules/recallatron/tests",
    sensitivity={},
    # No sink, because this skeleton registers no operation for one to fire on: the
    # dispatcher resolves a sink by the operation's owning module, and ``operations``
    # is empty. Run 1a1's memory operations are ``mutate``, so 1a1 is the run that
    # must supply a real one here — ``audit_sink=CORE_AUDIT_SINK``, or a writer of its
    # own. Copying this file and leaving this line alone does not fail at load time;
    # it fails later, as ``AUDIT_SINK_MISSING`` at the first dispatch.
    audit_sink=None,
)
