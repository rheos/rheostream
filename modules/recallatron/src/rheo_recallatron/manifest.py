"""Recallatron's ``ModuleManifest``: the schema half, declared.

Every one of the manifest's twenty-four fields is written out here, including those
whose consumers no run has built — they are present and empty rather than omitted, so
the run that builds a consumer reads a declaration somebody chose rather than a default
nobody did.

**What this revision fills in, and what stays empty.** The storage declaration names
the two Postgres extensions its tables need, ``configuration_schema`` carries the
module's settings keys, ``record_types`` declares the two addressable tables and
carries the memory's own owned-delete pair, and ``operations``/``resolvers`` carry the
read half, the write half, the two lifecycle changes, the two service-only entity
reads, the service-only ``recallatron.memory.dedup_candidates`` read and the owner's
long-running ``recallatron.embedding.rebuild``. ``events`` declares both ratified event
types and ``audit_sink`` is installed, because the first ``mutate`` operation now
exists and neither is optional for one: dispatch refuses every non-``READ`` call whose
module has no sink. ``deletion_participants`` carries the other half of an erasure,
``jobs`` and ``schedules`` carry the retention sweep and the daily row that enqueues it
(``jobs`` also carries ``recallatron.embed``, the after-commit embed job a memory write
enqueues, and ``recallatron.embedding_rebuild``, the one job the rebuild operation
enqueues; nothing schedules either), and ``tools`` carries § A10's seven memory tools,
and ``export`` now carries the real exporter/importer pair with the JSON Schema that
describes what they move.
``subscriptions`` stays empty, and that is deliberate: no consumer exists in 1a1, and
a declaration whose implementation is a later prompt's would be a promise the loader
registers and nothing keeps.

**Why the settings key is not declared in this file.** This module now imports its own
operations, which import the eligibility service, which has to read the retention key
to answer anything — so a key declared here would close an import loop. It lives in
``configuration.py``, which imports nothing from this package, and is declared on the
manifest from there.
"""

from importlib import metadata
from typing import Final

from rheo_contracts import CONTRACT_VERSION
from rheo_core.audit.core_sink import CORE_AUDIT_SINK
from rheo_core.modules.manifest import (
    DeletionParticipant,
    ExportDeclaration,
    JobKind,
    ModuleManifest,
    RecordType,
    Schedule,
    StorageDeclaration,
)

from rheo_recallatron.configuration import (
    DENSE_FLOOR_PERCENT_SPEC,
    EMBED_JOB_KIND,
    EMBED_MAX_ATTEMPTS,
    EMBEDDING_BATCH_SIZE_SPEC,
    EMBEDDING_PROVIDER_SPEC,
    ENTITY_RECORD_TYPE,
    MEMORY_RECORD_TYPE,
    MODULE_ID,
    OVERFETCH_MULTIPLIER_SPEC,
    REBUILD_JOB_KIND,
    REBUILD_MAX_ATTEMPTS,
    RETENTION_DAYS_SPEC,
    RETENTION_EXPIRE_BY_AGE_SPEC,
    RETRIEVAL_STRATEGY_SPEC,
)
from rheo_recallatron.eligibility import LIFECYCLE_ROLES
from rheo_recallatron.embedding.job import EmbedJobPayload, run_embed_job
from rheo_recallatron.embedding.rebuild import (
    EmbeddingRebuildPayload,
    run_embedding_rebuild_job,
)
from rheo_recallatron.events import EVENTS
from rheo_recallatron.export import (
    EXPORT_FORMAT_VERSION,
    export_memory_records,
    import_memory_records,
)
from rheo_recallatron.lifecycle import (
    DELETION_PARTICIPANT_TYPES,
    authorize_memory_delete,
    delete_owned_memory,
    on_record_deleted,
)
from rheo_recallatron.operations import OPERATIONS
from rheo_recallatron.resolvers import resolve_memory
from rheo_recallatron.retention import (
    MEMORY_RETENTION_SWEEP,
    SWEEP_CRON,
    SWEEP_MAX_ATTEMPTS,
    SWEEP_SCHEDULE_NAME,
    MemoryRetentionSweepPayload,
    run_memory_retention_sweep,
)
from rheo_recallatron.tools import TOOLS

DISTRIBUTION: Final = "rheo-recallatron"


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
    # Two record types, because two of the eight tables are addressable by canonical
    # reference: a ``recallatron.memory:<uuid>`` and a ``recallatron.memory_entity:
    # <uuid>``. Five of the other six are keyed by a composite primary key and the
    # sixth, ``embedding_state``, is a single row keyed by a boolean. None has an
    # independent identity to reference, so none of them is a record type and none
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
    # **Two explicit rows, and enable writes both** (§ A9). The gate decides whether
    # this workspace deletes memories by age at all and defaults to ``false``; the
    # window is read only while it is true. Declaring both here is what makes every
    # workspace's retention posture a stored readable value rather than an inference
    # from an absent row — and what makes turning expiry on one settings write against
    # a schedule that already exists, with no backfill path to invent.
    #
    # The retrieval strategy is the third explicit row, for the same reason: which
    # strategy ranks a workspace's recall is its own stated value from enable onward.
    #
    # The embedding provider, the rebuild's batch size, the dense relevance floor and
    # the dense arm's over-fetch multiplier are declared and not explicit: which model
    # runs is the deployment's choice, not a per-workspace row, and a batch size,
    # floor or multiplier nobody set is correctly the package default.
    configuration_schema=(
        RETENTION_EXPIRE_BY_AGE_SPEC,
        RETENTION_DAYS_SPEC,
        RETRIEVAL_STRATEGY_SPEC,
        DENSE_FLOOR_PERCENT_SPEC,
        OVERFETCH_MULTIPLIER_SPEC,
        EMBEDDING_PROVIDER_SPEC,
        EMBEDDING_BATCH_SIZE_SPEC,
    ),
    operations=OPERATIONS,
    # § A10's seven, and no eighth. Six name this module's own operations; the
    # seventh names the owned-deletion coordinator, which the ratified module
    # contract permits precisely because its input restricts the reference to this
    # module's own record type. The two entity reads stay service-only: § A10 gives
    # them "none" in the MCP column, and a tool over them would be a second route to
    # a vocabulary an entity is only ever reached through a readable mention.
    tools=TOOLS,
    # Both ratified event types, declared together because the manifest names them
    # together. ``recorded`` is published by this run's writes and by trusted
    # acceptance; ``invalidated`` is the lifecycle closure's, and declaring only the
    # one with a publisher would leave the other's shape for that run to invent.
    events=EVENTS,
    # Empty, and correct: no consumer exists in 1a1, and a publish that reaches none
    # still writes its outbox row and completes with zero deliveries. An event nobody
    # consumes is not an error.
    subscriptions=(),
    # The retention sweep, and the daily row that enqueues it. Separate from the
    # core's own ``core.retention_sweep`` in both halves on purpose: the two retain
    # different things under different policies, and one schedule feeding both would
    # make disabling either impossible.
    #
    # ``enabled_by_default=True`` because a workspace that enables this module has
    # thereby stated a retention policy — ``core.module.enable``'s step 3 writes the
    # explicit ``retention.days`` row in the same transaction as this schedule — and a
    # retention policy nothing enforces is a promise the product does not keep.
    #
    # ``cancellable`` is ``False``: a sweep is short by construction (at most a
    # hundred roots), commits its batch and its continuation together, and has no
    # partial state a cancellation could usefully stop at. A running batch that must
    # be stopped is stopped by disabling the schedule, which ends the continuations.
    jobs=(
        JobKind(
            name=MEMORY_RETENTION_SWEEP,
            input_model=MemoryRetentionSweepPayload,
            handler=run_memory_retention_sweep,
            max_attempts=SWEEP_MAX_ATTEMPTS,
            cancellable=False,
        ),
        # The embed job and the rebuild. Neither is cancellable, on the sweep's
        # precedent: an embed is one short provider call, and a rebuild's whole walk is
        # one transaction, so a cancellation could only discard the walk, never keep
        # the batches before it.
        JobKind(
            name=EMBED_JOB_KIND,
            input_model=EmbedJobPayload,
            handler=run_embed_job,
            max_attempts=EMBED_MAX_ATTEMPTS,
            cancellable=False,
        ),
        JobKind(
            name=REBUILD_JOB_KIND,
            input_model=EmbeddingRebuildPayload,
            handler=run_embedding_rebuild_job,
            max_attempts=REBUILD_MAX_ATTEMPTS,
            cancellable=False,
        ),
    ),
    schedules=(
        Schedule(
            name=SWEEP_SCHEDULE_NAME,
            job_kind=MEMORY_RETENTION_SWEEP,
            cron=SWEEP_CRON,
            enabled_by_default=True,
        ),
    ),
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
    # The real pair, and the JSON Schema beside them in the same wheel. ``schema_path``
    # is resolved as a package resource inside the installed distribution, so it is a
    # path relative to this package rather than to any checkout.
    export=ExportDeclaration(
        format_version=EXPORT_FORMAT_VERSION,
        schema_path="export.schema.json",
        exporter=export_memory_records,
        importer=import_memory_records,
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
