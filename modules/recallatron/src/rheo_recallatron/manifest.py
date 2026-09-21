"""Recallatron's ``ModuleManifest``: the schema half, declared.

Every one of the manifest's twenty-four fields is written out here, including those
whose consumers no run has built — they are present and empty rather than omitted, so
the run that builds a consumer reads a declaration somebody chose rather than a default
nobody did.

**What this revision fills in, and what stays empty.** The storage declaration names
the two Postgres extensions its tables need, ``configuration_schema`` carries the
module's one settings key, ``record_types`` declares the two addressable tables and
carries the memory's own owned-delete pair, and ``operations``/``resolvers`` carry the
read half, the write half, the two lifecycle changes and the two service-only entity
reads. ``events`` declares both ratified event types and ``audit_sink`` is installed,
because the first ``mutate`` operation now exists and neither is optional for one:
dispatch refuses every non-``READ`` call whose module has no sink.
``deletion_participants`` carries the other half of an erasure. ``tools``,
``subscriptions`` and ``export`` are still empty or unimplemented, and that is
deliberate: a declaration whose implementation is a later prompt's would be a promise
the loader registers and nothing keeps.

**Why the settings key is not declared in this file.** This module now imports its own
operations, which import the eligibility service, which has to read the retention key
to answer anything — so a key declared here would close an import loop. It lives in
``configuration.py``, which imports nothing from this package, and is declared on the
manifest from there.
"""

from importlib import metadata
from typing import Final, NoReturn

from rheo_contracts import CONTRACT_VERSION
from rheo_core.audit.core_sink import CORE_AUDIT_SINK
from rheo_core.modules.manifest import (
    DeletionParticipant,
    ExportDeclaration,
    ModuleManifest,
    RecordType,
    StorageDeclaration,
)

from rheo_recallatron.configuration import (
    ENTITY_RECORD_TYPE,
    MEMORY_RECORD_TYPE,
    MODULE_ID,
    RETENTION_DAYS_SPEC,
)
from rheo_recallatron.eligibility import LIFECYCLE_ROLES
from rheo_recallatron.events import EVENTS
from rheo_recallatron.lifecycle import (
    DELETION_PARTICIPANT_TYPES,
    authorize_memory_delete,
    delete_owned_memory,
    on_record_deleted,
)
from rheo_recallatron.operations import OPERATIONS
from rheo_recallatron.resolvers import resolve_memory

DISTRIBUTION: Final = "rheo-recallatron"


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
    # forget, and it carries the owned-delete pair the deletion coordinator calls. An
    # entity is pruned as a consequence of losing its last mention, which is the
    # service's own housekeeping rather than a deletion target, so it declares no
    # delete roles and no pair.
    #
    # ``delete_roles`` is the same frozen set the authorizer checks, not a second copy
    # of it: the declaration a reader sees and the check a caller meets cannot come
    # to disagree if there is one of them.
    #
    # ``audience_field`` names the column carrying the record's audience. A memory's
    # audience is the ``(audience_kind, audience_id)`` pair and the *kind* is the
    # discriminator — a workspace memory has no id at all — so the kind column is what
    # the field names. An entity carries no audience: it is reachable only through a
    # mention on a memory the caller may already read, which is where the audience
    # decision was made.
    record_types=(
        RecordType(
            name=MEMORY_RECORD_TYPE,
            table=f"{MODULE_ID}.{MEMORY_RECORD_TYPE}",
            deletable=True,
            delete_roles=LIFECYCLE_ROLES,
            exportable=True,
            audience_field="audience_kind",
            authorize_delete=authorize_memory_delete,
            delete_owned=delete_owned_memory,
        ),
        RecordType(
            name=ENTITY_RECORD_TYPE,
            table=f"{MODULE_ID}.{ENTITY_RECORD_TYPE}",
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
    configuration_schema=(RETENTION_DAYS_SPEC,),
    operations=OPERATIONS,
    tools=(),
    # Both ratified event types, declared together because the manifest names them
    # together. ``recorded`` is published by this run's writes and by trusted
    # acceptance; ``invalidated`` is the lifecycle closure's, and declaring only the
    # one with a publisher would leave the other's shape for that run to invent.
    events=EVENTS,
    # Empty, and correct: no consumer exists in 1a1, and a publish that reaches none
    # still writes its outbox row and completes with zero deliveries. An event nobody
    # consumes is not an error.
    subscriptions=(),
    jobs=(),
    schedules=(),
    # One resolver, for the one record type a reference can name. An entity is reached
    # only through a mention on a memory the caller may already read, so it has no
    # resolution of its own to declare. The resolver calls straight into the shared
    # eligibility function — it is not a second permission check.
    resolvers=((MEMORY_RECORD_TYPE, resolve_memory),),
    # One participant, over the one owned record type release one has for it to hook.
    # It is the other half of a memory's erasure: the owned-delete pair above removes
    # the target and the rows that hang off it, and this removes every memory that
    # depended on the reference — the ratified cascade's step 2 and step 3. Its
    # handler works from the reference rather than from the row, so it is correct
    # whether the target was a memory this module owned or, when another owned type
    # exists, somebody else's record.
    deletion_participants=(
        DeletionParticipant(
            record_types=DELETION_PARTICIPANT_TYPES, handler=on_record_deleted
        ),
    ),
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
    # The first ``mutate`` operations now exist, so the sink does too. It is the core's
    # own shared singleton rather than a writer of Recallatron's: this module has no
    # audit writer, and installing ``CORE_AUDIT_SINK`` under a second module id is the
    # documented pattern for exactly that case (``audit/core_sink.py`` — the object
    # holds no state, reads no module identity, and writes through whichever unit of
    # work it is handed). ``install_sink`` is idempotent for the *identical* object, so
    # the core's own registration and this one cannot refuse each other.
    #
    # Nothing in this module calls it. The dispatcher is the tree's one audit caller
    # (``tests/test_audit_sink.py``), which is what keeps a module unable to write its
    # own audit row — and is why "the mutation and its audit row commit together" is a
    # property of one transaction rather than of two cooperating writers.
    audit_sink=CORE_AUDIT_SINK,
)
