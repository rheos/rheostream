"""Recallatron's ``ModuleManifest``: the schema half, declared.

Every one of the manifest's twenty-four fields is written out here, including those
whose consumers no run has built — they are present and empty rather than omitted, so
the run that builds a consumer reads a declaration somebody chose rather than a default
nobody did.

**What this revision fills in, and what stays empty.** The storage declaration now
names the two Postgres extensions its tables need, ``configuration_schema`` carries the
module's one settings key, and ``record_types`` declares the two addressable tables a
resolver will answer for. ``operations``, ``tools``, ``events``, ``resolvers``,
``deletion_participants``, ``export`` and ``audit_sink`` are still empty or
unimplemented, and that is deliberate: this is the schema, and a declaration whose
implementation is a later prompt's would be a promise the loader registers and nothing
keeps. ``audit_sink`` in particular stays ``None`` until a ``mutate`` operation exists
to need one — see the note at the field.
"""

from importlib import metadata
from typing import Final, NoReturn

from rheo_contracts import CONTRACT_VERSION, Role
from rheo_core.modules.manifest import (
    ExportDeclaration,
    ModuleManifest,
    RecordType,
    StorageDeclaration,
)
from rheo_core.settings import KeySpec, Scope, ValueType

MODULE_ID: Final = "recallatron"
DISTRIBUTION: Final = "rheo-recallatron"

RETENTION_DAYS_KEY: Final = f"{MODULE_ID}.retention.days"
"""How long a memory stays readable in a workspace, in days.

``explicit_per_workspace`` because a workspace's retention is its own stated policy
from the moment the module is enabled, not an inherited deployment value it happens to
share: ``modules/operations.py:_write_enable_rows`` writes the row from the default
below, and the core's own settings-write operation moves it from there. (Named in
prose rather than spelled out, because the schema-ownership scan reads a
``<core-schema>.<name>`` literal in this tree as a foreign table reference and is
right to — this module must not name one, even in a docstring.)

**Bounded rather than floored, and the two are different tools.** A floor combines a
workspace's override with the deployment's value under a comparator — which is right
for ``approvals.max_window_seconds``, where a workspace may only shorten its own
window. Retention is not narrowable-only-downward: a workspace may legitimately choose
a longer window than its neighbour. What must not be settable at *any* layer is zero
days (every read empty the instant it commits) or a millennium, and that is what
``minimum``/``maximum`` express and a floor cannot.
"""

RETENTION_DAYS_DEFAULT: Final = 365
RETENTION_DAYS_MINIMUM: Final = 1
RETENTION_DAYS_MAXIMUM: Final = 3650


def _export_unavailable(*_args: object, **_kwargs: object) -> NoReturn:
    """Stands in for the exporter until this module has an export format to write.

    ``ExportDeclaration`` requires both callables, and ``export`` is a required
    manifest field, so a manifest without an exporter has to supply a pair. The
    record types below are now declared ``exportable``, so this pair is what a caller
    reaching for the exporter hits — loudly, naming the gap — rather than a silent
    empty archive. The real pair, and the JSON Schema beside it, are the export run's.
    """
    raise NotImplementedError(
        f"{MODULE_ID} declares no export format yet, so it has nothing to export"
    )


def _import_unavailable(*_args: object, **_kwargs: object) -> NoReturn:
    """The other half of the pair; see :func:`_export_unavailable`."""
    raise NotImplementedError(
        f"{MODULE_ID} declares no export format yet, so it has nothing to import"
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
    # Two record types, because two of the seven tables are addressable by canonical
    # reference: a ``recallatron.memory:<uuid>`` and a ``recallatron.memory_entity:
    # <uuid>``. The other five are associations keyed by a composite primary key, with
    # no independent identity to reference, so none of them is a record type and none
    # gains a synthetic id to make it one.
    #
    # ``deletable=True`` on ``memory`` alone: a memory is the thing a person asks to
    # forget, and the owned-delete registration that acts on it is the deletion
    # prompt's. An entity is pruned as a consequence of losing its last mention, which
    # is the service's own housekeeping rather than a ``core.record.delete`` target, so
    # it declares no delete roles.
    #
    # ``audience_field`` names the column carrying the record's audience. A memory's
    # audience is the ``(audience_kind, audience_id)`` pair and the *kind* is the
    # discriminator — a workspace memory has no id at all — so the kind column is what
    # the field names. An entity carries no audience: it is reachable only through a
    # mention on a memory the caller may already read, which is where the audience
    # decision was made.
    record_types=(
        RecordType(
            name="memory",
            table=f"{MODULE_ID}.memory",
            deletable=True,
            delete_roles=frozenset({Role.OWNER, Role.MEMBER}),
            exportable=True,
            audience_field="audience_kind",
        ),
        RecordType(
            name="memory_entity",
            table=f"{MODULE_ID}.memory_entity",
            deletable=False,
            delete_roles=frozenset(),
            exportable=True,
            audience_field=None,
        ),
    ),
    storage=StorageDeclaration(
        schema_name=MODULE_ID,
        migrations_path="rheo_recallatron.migrations",
        # ``vector`` supplies ``memory_embedding.vector``'s column type and ``pg_trgm``
        # supplies ``memory_title_trgm``'s operator class, so both must exist before
        # the first DDL statement runs. ``modules/operations.py:_create_extensions``
        # installs whatever is listed here, in its own step before the chain — which is
        # also why no migration in this module issues a ``CREATE EXTENSION``: the
        # extension's types land outside this module's schema, and a statement this
        # module owns must not put anything there.
        required_extensions=("vector", "pg_trgm"),
    ),
    configuration_schema=(
        KeySpec(
            key=RETENTION_DAYS_KEY,
            type=ValueType.INT,
            scope=Scope.WORKSPACE,
            floor=None,
            explicit_per_workspace=True,
            default=RETENTION_DAYS_DEFAULT,
            minimum=RETENTION_DAYS_MINIMUM,
            maximum=RETENTION_DAYS_MAXIMUM,
        ),
    ),
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
