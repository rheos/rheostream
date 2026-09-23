"""Recallatron export format v1: the writer, the reader, and the two-pass restore.

Architecture § A12's module-bridge half. Six of the module's eight tables travel:
memory, purposes, entities, mentions, links and receipts. The other two deliberately do
not. ``memory_embedding`` holds vectors, which ``recallatron.embedding.rebuild``
recreates from the titles and bodies that do travel, so shipping one would be shipping
a cache. ``embedding_state`` only stamps which embed-input version those vectors were
made under: a restored workspace has no vectors and no stamp, and its first rebuild
writes both. ``search_tsv`` is likewise
absent because it is a generated column: the DDL recomputes it from the title and body
that do travel, and a transported copy could only ever disagree with the expression
that owns it.

**The exporter never sees a connection it could re-read the world from.** It receives
the sealed snapshot view the collector opened — one read-only repeatable-read
transaction, one fixed evaluation instant, one settings source bound to that same
transaction — and reads everything through it. That is what makes "every category
describes one committed instant" a property of the seam rather than of this file's
manners.

**The refusal in here is diagnosable, and it is the only thing that stops an export.**
When a workspace has turned age-based expiry on and holds workspace-audience content
already past its window at the snapshot instant, there is no honest artifact to write:
the rows are readable in the archive and unreadable in the workspace. So the exporter
refuses, and the refusal carries a bounded content-free count plus the sweep
schedule's own state, because "wait for the next batch" and "the sweep is stuck" are
different remedies and an operator cannot otherwise tell them apart. There is no
force-export override: one would defeat the guarantee the refusal exists to keep.

**The importer is two passes with somebody else's work in between.** Pass one inserts
parents with their self-references null, then entities, associations and receipts;
core then imports its own deletion evidence; only then does pass two resolve the
self-references and validate every marked-ancestry edge against that evidence. The
ordering is not an optimisation — a legitimately expired predecessor is *absent* by
construction, and the only thing that distinguishes it from a missing one is the
expiry record that has to already be there.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Final
from uuid import UUID

from rheo_contracts import RecordRef, RecordRefMalformed
from rheo_core.deletion import RETENTION_EXPIRY
from rheo_core.deletion.records import DeletionRecordRow, list_deletion_records
from rheo_core.operations.refusals import OperationRefused
from rheo_core.refs.resolver import UnitOfWork
from rheo_core.work.schedules import ScheduleOutcome, schedule_outcome

from rheo_recallatron.configuration import (
    AUTOMATIC_BOUND_PURPOSE,
    AUTOMATIC_PRODUCER_KINDS,
    MEMORY_RECORD_TYPE,
    MODULE_ID,
)
from rheo_recallatron.eligibility import retention_in
from rheo_recallatron.refusals import RETENTION_UNAVAILABLE
from rheo_recallatron.retention import (
    MEMORY_RETENTION_SWEEP,
    SWEEP_SCHEDULE_NAME,
)
from rheo_recallatron.storage import tables as t
from rheo_recallatron.storage.repository import (
    MemoryEntityRow,
    MemoryLinkRow,
    MemoryMentionRow,
    MemoryPurposeRow,
    MemoryRow,
    SourceReceiptRow,
    count_expired_memory_roots,
    export_memory_entity_rows,
    export_memory_link_rows,
    export_memory_mention_rows,
    export_memory_purpose_rows,
    export_memory_rows,
    export_source_receipt_rows,
    insert_memory,
    insert_memory_entity,
    insert_memory_link,
    insert_memory_mention,
    insert_memory_purpose,
    insert_source_receipt,
    set_memory_successor,
)

if TYPE_CHECKING:  # pragma: no cover - see the note below on why this is not runtime
    from rheo_core.exports.artifact import ExportSnapshot

# ``ExportSnapshot`` is imported for typing only. The export package's own
# ``__init__`` pulls in the operation module, which pulls in the operation registry
# package, and importing that chain from a module manifest at load time depends on
# which of the two the interpreter reached first. Nothing here needs the class at
# runtime — three attributes are read off the value it is handed — so the annotation
# is a string and the import edge does not exist.

EXPORT_FORMAT_VERSION: Final = 1

EXPORT_REQUIRES_RETENTION_SWEEP: Final = "export_requires_retention_sweep"
"""Workspace-audience content is already expired at the snapshot instant (§ A12).

No artifact is written. The remedy is the sweep, not a flag: A9's trusted bounded
catch-up removes the counted rows and a retry opens a new snapshot over a workspace
that no longer holds them.
"""

EXPIRED_DIAGNOSTIC_LIMIT: Final = 1000
"""How far the refusal's diagnostic count will count, and no further.

Bounded twice over on purpose. An unbounded ``COUNT(*)`` inside a refusal path is a
full scan of the table the export just gave up on; and an exact figure over a large
workspace edges towards a population count, which is not what a content-free
diagnostic is for. A result equal to this limit means "at least this many", which is
all an operator needs to tell one stuck row from a backlog.
"""

ARTIFACT_INVALID: Final = "recallatron_artifact_invalid"
"""One refusal state for every way this module's half of an artifact can be wrong.

One rather than a vocabulary of twenty, because the audience is an operator restoring
an archive and the actionable part is the ``detail`` — which line, and what about it
— rather than a machine-readable taxonomy of malformed-artifact species that nothing
branches on.
"""

_RECORD_KEY: Final = "record"
MEMORY_RECORD: Final = "memory"
PURPOSE_RECORD: Final = "memory_purpose"
ENTITY_RECORD: Final = "memory_entity"
MENTION_RECORD: Final = "memory_mention"
LINK_RECORD: Final = "memory_link"
RECEIPT_RECORD: Final = "source_receipt"

_KIND_ORDER: Final = (
    MEMORY_RECORD,
    PURPOSE_RECORD,
    ENTITY_RECORD,
    MENTION_RECORD,
    LINK_RECORD,
    RECEIPT_RECORD,
)
"""The order the categories are emitted in, fixed rather than incidental.

Parents before the rows that reference them, and the same order every time: the
artifact's bytes are compared against a restore of themselves, so an emission order
that depended on anything but this tuple would make the digest depend on it too.
"""

_ERASED_REASON: Final = t.INVALIDATION_REASONS[0]
"""``source_deleted``, read off the table's vocabulary rather than written again.

The one invalidation reason an artifact may not carry content for: a memory marked
with it was erased, and an export that shipped its title and body would be a restore
path that revives erased content.
"""

_SUPERSEDED_REASON: Final = t.INVALIDATION_REASONS[2]
_ACTIVE_STATE: Final = t.RECEIPT_STATES[0]
_TERMINAL_STATES: Final = t.RECEIPT_STATES[1:]
_ORIGIN_DERIVED: Final = t.MEMORY_ORIGINS[1]
_RELATION_DERIVED_FROM: Final = t.LINK_RELATIONS[0]
_REPRESENTATION_MEMORY, _REPRESENTATION_ENTITY = t.REPRESENTATION_TYPES
_QUALIFIED_MEMORY: Final = f"{MODULE_ID}.{MEMORY_RECORD_TYPE}"


class ArtifactRowInvalid(OperationRefused):
    """This module's half of an artifact does not validate. Raised, never returned.

    An ``OperationRefused`` rather than a bare exception so a restore job's failure
    reads as a named refusal rather than a handler crash, and so the state is one an
    operator can look up.
    """

    def __init__(self, detail: str) -> None:
        super().__init__(ARTIFACT_INVALID, detail)


@dataclass(frozen=True, slots=True)
class UnsweptContent:
    """The content-free diagnostic that travels with the export refusal (§ A12).

    Two facts, because two questions have to be answerable. ``expired_root_count``
    (with ``count_is_bounded`` saying whether it saturated
    :data:`EXPIRED_DIAGNOSTIC_LIMIT`) says how much is in the way; ``schedule`` says
    what the thing that would remove it is doing, and names the schedule and job ids
    an operator would otherwise correlate by hand.

    **Neither figure is per-caller.** The count is of workspace-audience roots past
    the snapshot's own horizon, full stop — not of the roots this caller could read.
    A count narrowed by the caller would be a derivative of their own visibility, and
    two operators comparing notes would see different numbers for one workspace; worse,
    the difference between them would itself disclose what the other could see.
    """

    expired_root_count: int
    count_is_bounded: bool
    schedule: ScheduleOutcome


class ExportRequiresRetentionSweep(OperationRefused):
    """§ A12's refusal, carrying :class:`UnsweptContent`.

    The diagnostic rides on the exception as typed attributes rather than being
    formatted into ``detail`` and parsed back out: ``detail`` is prose for an operator,
    and a caller that needs the number should read the number.
    """

    def __init__(self, diagnostic: UnsweptContent) -> None:
        at_least = "at least " if diagnostic.count_is_bounded else ""
        schedule = diagnostic.schedule
        named = "absent" if schedule.schedule_id is None else str(schedule.schedule_id)
        super().__init__(
            EXPORT_REQUIRES_RETENTION_SWEEP,
            f"{at_least}{diagnostic.expired_root_count} workspace-audience memories "
            f"are past this workspace's retention window and have not been swept; "
            f"the sweep schedule is {named}, next due {schedule.next_run_at}, and "
            f"its most recent job {schedule.job_id} is {schedule.job_state} after "
            f"{schedule.job_attempts} of {schedule.job_max_attempts} attempts",
        )
        self.diagnostic = diagnostic


# --- the exporter ---------------------------------------------------------------------


def _unswept(snapshot: "ExportSnapshot") -> None:
    """Refuse when this snapshot holds content the sweep should already have removed.

    The gate and the window are read through the snapshot's **own** settings source,
    so the policy applied is the policy this committed view holds, and the horizon is
    computed from ``snapshot_at`` rather than from a clock sampled here — resampling
    would evaluate retention at an instant the artifact does not describe.

    With the gate off there is no horizon and this is unreachable by construction,
    which is the common case: Recallatron does not expire memories by age unless a
    workspace asks it to. Member-audience rows are never counted at any age (§ A9) —
    the sweep may never remove them, so counting them would make export permanently
    impossible for a workspace that turned expiry on and holds one old private memory.
    """
    policy = retention_in(snapshot.overrides.workspace_overrides(snapshot.workspace_id))
    if policy is None:
        raise OperationRefused(
            RETENTION_UNAVAILABLE,
            "this workspace states no usable retention policy",
        )
    horizon = policy.horizon(snapshot.snapshot_at)
    if horizon is None:
        return
    count = count_expired_memory_roots(
        snapshot.connection, horizon=horizon, limit=EXPIRED_DIAGNOSTIC_LIMIT
    )
    if count == 0:
        return
    raise ExportRequiresRetentionSweep(
        UnsweptContent(
            expired_root_count=count,
            count_is_bounded=count == EXPIRED_DIAGNOSTIC_LIMIT,
            schedule=schedule_outcome(
                snapshot.connection,
                module_id=MODULE_ID,
                name=SWEEP_SCHEDULE_NAME,
                job_kind=MEMORY_RETENTION_SWEEP,
            ),
        )
    )


def _memory_line(row: MemoryRow) -> dict[str, object]:
    return {
        _RECORD_KEY: MEMORY_RECORD,
        "id": row.id,
        "kind": row.kind,
        "title": row.title,
        "body": row.body,
        "audience_kind": row.audience_kind,
        "audience_id": row.audience_id,
        "confidence": row.confidence,
        "occurred_at": row.occurred_at,
        "recorded_at": row.recorded_at,
        "recorded_by_kind": row.recorded_by_kind,
        "recorded_by_id": row.recorded_by_id,
        "origin": row.origin,
        "revision": row.revision,
        "corrected_at": row.corrected_at,
        "superseded_by_id": row.superseded_by_id,
        "invalidated_at": row.invalidated_at,
        "invalidation_reason": row.invalidation_reason,
        "source_namespace": row.source_namespace,
        "external_source_key": row.external_source_key,
    }


def _entity_line(row: MemoryEntityRow) -> dict[str, object]:
    return {
        _RECORD_KEY: ENTITY_RECORD,
        "id": row.id,
        "kind": row.kind,
        "name": row.name,
        "normalized_name": row.normalized_name,
        "ref": row.ref,
        "created_at": row.created_at,
        "source_namespace": row.source_namespace,
        "external_source_key": row.external_source_key,
    }


def _receipt_line(row: SourceReceiptRow) -> dict[str, object]:
    return {
        _RECORD_KEY: RECEIPT_RECORD,
        "representation_type": row.representation_type,
        "source_namespace": row.source_namespace,
        "external_source_key": row.external_source_key,
        "record_id": row.record_id,
        "payload_digest": row.payload_digest,
        "state": row.state,
        "producer_kind": row.producer_kind,
        "authority_id": row.authority_id,
        "principal_account_id": row.principal_account_id,
        "audience_kind": row.audience_kind,
        "audience_id": row.audience_id,
        "bound_purpose": row.bound_purpose,
        "source_recorded_at": row.source_recorded_at,
        "source_expires_at": row.source_expires_at,
    }


def export_memory_records(
    snapshot: "ExportSnapshot",
) -> Sequence[Mapping[str, object]]:
    """Every exportable Recallatron row, deterministically ordered, or a refusal.

    The refusal comes first, before a single row is read: A12 asks for no successful
    artifact, and building the category and then discarding it would read the whole
    workspace to throw it away.
    """
    _unswept(snapshot)
    connection = snapshot.connection
    lines: list[Mapping[str, object]] = []
    lines.extend(_memory_line(row) for row in export_memory_rows(connection))
    lines.extend(
        {
            _RECORD_KEY: PURPOSE_RECORD,
            "memory_id": row.memory_id,
            "purpose": row.purpose,
        }
        for row in export_memory_purpose_rows(connection)
    )
    lines.extend(_entity_line(row) for row in export_memory_entity_rows(connection))
    lines.extend(
        {
            _RECORD_KEY: MENTION_RECORD,
            "memory_id": row.memory_id,
            "entity_id": row.entity_id,
            "role": row.role,
        }
        for row in export_memory_mention_rows(connection)
    )
    lines.extend(
        {
            _RECORD_KEY: LINK_RECORD,
            "memory_id": row.memory_id,
            "ref": row.ref,
            "relation": row.relation,
            "created_at": row.created_at,
            "supersession_lineage": row.supersession_lineage,
        }
        for row in export_memory_link_rows(connection)
    )
    lines.extend(_receipt_line(row) for row in export_source_receipt_rows(connection))
    return tuple(lines)


# --- the importer ---------------------------------------------------------------------


def _uuid(value: object, field: str) -> UUID:
    try:
        return UUID(str(value))
    except (TypeError, ValueError):
        raise ArtifactRowInvalid(f"{field} is not an identifier") from None


def _maybe_uuid(value: object, field: str) -> UUID | None:
    return None if value is None else _uuid(value, field)


def _instant(value: object, field: str) -> datetime:
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        raise ArtifactRowInvalid(f"{field} is not a timestamp") from None


def _maybe_instant(value: object, field: str) -> datetime | None:
    return None if value is None else _instant(value, field)


def _text(value: object) -> str | None:
    return None if value is None else str(value)


def _memory_of(row: Mapping[str, object]) -> MemoryRow:
    return MemoryRow(
        id=_uuid(row["id"], "memory id"),
        kind=str(row["kind"]),
        title=str(row["title"]),
        body=str(row["body"]),
        audience_kind=str(row["audience_kind"]),
        audience_id=_maybe_uuid(row["audience_id"], "memory audience"),
        confidence=None if row["confidence"] is None else float(str(row["confidence"])),
        occurred_at=_maybe_instant(row["occurred_at"], "occurred_at"),
        recorded_at=_instant(row["recorded_at"], "recorded_at"),
        recorded_by_kind=str(row["recorded_by_kind"]),
        recorded_by_id=_maybe_uuid(row["recorded_by_id"], "recorded_by_id"),
        origin=str(row["origin"]),
        revision=int(str(row["revision"])),
        corrected_at=_maybe_instant(row["corrected_at"], "corrected_at"),
        superseded_by_id=_maybe_uuid(row["superseded_by_id"], "superseded_by_id"),
        invalidated_at=_maybe_instant(row["invalidated_at"], "invalidated_at"),
        invalidation_reason=_text(row["invalidation_reason"]),
        source_namespace=_text(row["source_namespace"]),
        external_source_key=_text(row["external_source_key"]),
    )


def _entity_of(row: Mapping[str, object]) -> MemoryEntityRow:
    return MemoryEntityRow(
        id=_uuid(row["id"], "entity id"),
        kind=str(row["kind"]),
        name=str(row["name"]),
        normalized_name=str(row["normalized_name"]),
        ref=_text(row["ref"]),
        created_at=_instant(row["created_at"], "entity created_at"),
        source_namespace=_text(row["source_namespace"]),
        external_source_key=_text(row["external_source_key"]),
    )


def _receipt_of(row: Mapping[str, object]) -> SourceReceiptRow:
    digest = str(row["payload_digest"])
    try:
        payload_digest = bytes.fromhex(digest)
    except ValueError:
        raise ArtifactRowInvalid("a receipt digest is not hexadecimal") from None
    return SourceReceiptRow(
        representation_type=str(row["representation_type"]),
        source_namespace=str(row["source_namespace"]),
        external_source_key=str(row["external_source_key"]),
        record_id=_maybe_uuid(row["record_id"], "receipt record id"),
        payload_digest=payload_digest,
        state=str(row["state"]),
        producer_kind=str(row["producer_kind"]),
        authority_id=_uuid(row["authority_id"], "receipt authority"),
        principal_account_id=_maybe_uuid(
            row["principal_account_id"], "receipt principal"
        ),
        audience_kind=str(row["audience_kind"]),
        audience_id=_maybe_uuid(row["audience_id"], "receipt audience"),
        bound_purpose=_text(row["bound_purpose"]),
        source_recorded_at=_maybe_instant(
            row["source_recorded_at"], "source_recorded_at"
        ),
        source_expires_at=_maybe_instant(row["source_expires_at"], "source_expires_at"),
    )


@dataclass(frozen=True, slots=True)
class _Parsed:
    """One artifact's Recallatron half, decoded and bucketed, before any insert."""

    memories: tuple[MemoryRow, ...]
    purposes: tuple[MemoryPurposeRow, ...]
    entities: tuple[MemoryEntityRow, ...]
    mentions: tuple[MemoryMentionRow, ...]
    links: tuple[MemoryLinkRow, ...]
    receipts: tuple[SourceReceiptRow, ...]


def _parsed(rows: Sequence[Mapping[str, object]]) -> _Parsed:
    buckets: dict[str, list[Mapping[str, object]]] = {kind: [] for kind in _KIND_ORDER}
    for row in rows:
        kind = str(row.get(_RECORD_KEY))
        if kind not in buckets:
            raise ArtifactRowInvalid(f"unknown record kind {kind!r}")
        buckets[kind].append(row)
    return _Parsed(
        memories=tuple(_memory_of(row) for row in buckets[MEMORY_RECORD]),
        purposes=tuple(
            MemoryPurposeRow(
                memory_id=_uuid(row["memory_id"], "purpose memory"),
                purpose=str(row["purpose"]),
            )
            for row in buckets[PURPOSE_RECORD]
        ),
        entities=tuple(_entity_of(row) for row in buckets[ENTITY_RECORD]),
        mentions=tuple(
            MemoryMentionRow(
                memory_id=_uuid(row["memory_id"], "mention memory"),
                entity_id=_uuid(row["entity_id"], "mention entity"),
                role=_text(row["role"]),
            )
            for row in buckets[MENTION_RECORD]
        ),
        links=tuple(
            MemoryLinkRow(
                memory_id=_uuid(row["memory_id"], "link memory"),
                ref=str(row["ref"]),
                relation=str(row["relation"]),
                created_at=_instant(row["created_at"], "link created_at"),
                supersession_lineage=bool(row["supersession_lineage"]),
            )
            for row in buckets[LINK_RECORD]
        ),
        receipts=tuple(_receipt_of(row) for row in buckets[RECEIPT_RECORD]),
    )


def _source_identities(parsed: "_Parsed") -> tuple[tuple[str, str, str], ...]:
    """Every ``(representation_type, namespace, key)`` a content row in this artifact
    holds.

    The tuple a receipt's own primary key is made of, built from the *content* side,
    so "does a live representation hold this key" and "is this key unique" are two
    questions asked of one derivation rather than of two that could drift.
    """
    held: list[tuple[str, str, str]] = []
    for memory in parsed.memories:
        if memory.source_namespace is not None and (
            memory.external_source_key is not None
        ):
            held.append(
                (
                    _REPRESENTATION_MEMORY,
                    memory.source_namespace,
                    memory.external_source_key,
                )
            )
    for entity in parsed.entities:
        if entity.source_namespace is not None and (
            entity.external_source_key is not None
        ):
            held.append(
                (
                    _REPRESENTATION_ENTITY,
                    entity.source_namespace,
                    entity.external_source_key,
                )
            )
    return tuple(held)


def _live_representations(parsed: "_Parsed") -> dict[tuple[str, str, str], UUID]:
    """The same identities, mapped to the row that holds each one."""
    by_key: dict[tuple[str, str, str], UUID] = {}
    for memory in parsed.memories:
        if memory.source_namespace is not None and (
            memory.external_source_key is not None
        ):
            by_key[
                (
                    _REPRESENTATION_MEMORY,
                    memory.source_namespace,
                    memory.external_source_key,
                )
            ] = memory.id
    for entity in parsed.entities:
        if entity.source_namespace is not None and (
            entity.external_source_key is not None
        ):
            by_key[
                (
                    _REPRESENTATION_ENTITY,
                    entity.source_namespace,
                    entity.external_source_key,
                )
            ] = entity.id
    return by_key


def _validate_structure(parsed: _Parsed) -> dict[UUID, set[str]]:
    """A12 step 1's pair, FK, uniqueness and forbidden-content checks.

    Everything here is decidable from the artifact alone, before a single row is
    written, which is what "validates the entire artifact before activation" asks
    for. Returns the purpose sets, because the receipt rules below need them and
    rebuilding them a second time is how two readers come to disagree.
    """
    memories = {row.id: row for row in parsed.memories}
    if len(memories) != len(parsed.memories):
        raise ArtifactRowInvalid("two memory rows share one identifier")
    entities = {row.id: row for row in parsed.entities}
    if len(entities) != len(parsed.entities):
        raise ArtifactRowInvalid("two entity rows share one identifier")

    for memory in parsed.memories:
        if memory.invalidation_reason == _ERASED_REASON:
            raise ArtifactRowInvalid(
                f"memory {memory.id} is erased and carries content; no restore path "
                "reconstructs erased content"
            )

    keys: set[tuple[str, str, str]] = set()
    for held in _source_identities(parsed):
        if held in keys:
            raise ArtifactRowInvalid(f"two {held[0]} rows share one source key")
        keys.add(held)

    purposes: dict[UUID, set[str]] = {}
    for purpose in parsed.purposes:
        if purpose.memory_id not in memories:
            raise ArtifactRowInvalid(
                f"a purpose names absent memory {purpose.memory_id}"
            )
        if purpose.purpose in purposes.setdefault(purpose.memory_id, set()):
            raise ArtifactRowInvalid(
                f"memory {purpose.memory_id} repeats purpose {purpose.purpose!r}"
            )
        purposes[purpose.memory_id].add(purpose.purpose)
    missing = sorted(str(identity) for identity in memories if identity not in purposes)
    if missing:
        raise ArtifactRowInvalid(f"memories carry no purpose: {missing}")

    for mention in parsed.mentions:
        if mention.memory_id not in memories:
            raise ArtifactRowInvalid(
                f"a mention names absent memory {mention.memory_id}"
            )
        if mention.entity_id not in entities:
            raise ArtifactRowInvalid(
                f"a mention names absent entity {mention.entity_id}"
            )

    for link in parsed.links:
        if link.memory_id not in memories:
            raise ArtifactRowInvalid(f"a link names absent memory {link.memory_id}")
        try:
            reference = RecordRef.parse(link.ref)
        except (RecordRefMalformed, TypeError):
            raise ArtifactRowInvalid(
                f"link reference {link.ref!r} is not a canonical record reference"
            ) from None
        if link.supersession_lineage and (
            link.relation != _RELATION_DERIVED_FROM
            or f"{reference.module}.{reference.record_type}" != _QUALIFIED_MEMORY
        ):
            raise ArtifactRowInvalid(
                f"marked ancestry on {link.ref!r} is not a memory provenance edge"
            )
    return purposes


def _validate_receipts(parsed: _Parsed, purposes: Mapping[UUID, set[str]]) -> None:
    """A12 step 2's receipt rules, against the content rows in the same artifact.

    Three groups of rule, and each exists because breaking it restores a lie. An
    active receipt whose key or id disagrees with the row it names would let a replay
    return somebody else's memory. A terminal receipt sitting beside a live row would
    say a source was refused and also accepted. And an automatic active row whose
    origin, purpose, audience or clock was rewritten on the way through would have
    acquired provenance the acceptance path would never have given it.
    """
    memories = {row.id: row for row in parsed.memories}
    entities = {row.id: row for row in parsed.entities}
    live = _live_representations(parsed)

    seen: set[tuple[str, str, str]] = set()
    for receipt in parsed.receipts:
        identity = (
            receipt.representation_type,
            receipt.source_namespace,
            receipt.external_source_key,
        )
        if identity in seen:
            raise ArtifactRowInvalid(f"two receipts share the identity {identity}")
        seen.add(identity)
        if receipt.state == _ACTIVE_STATE:
            if receipt.record_id is None:
                raise ArtifactRowInvalid("an active receipt names no record")
            if live.get(identity) != receipt.record_id:
                raise ArtifactRowInvalid(
                    f"active receipt {identity} disagrees with the row it names"
                )
            if receipt.representation_type == _REPRESENTATION_MEMORY:
                _validate_automatic(receipt, memories[receipt.record_id], purposes)
            elif receipt.record_id not in entities:
                raise ArtifactRowInvalid(
                    f"active receipt {identity} names an absent entity"
                )
            continue
        if receipt.state not in _TERMINAL_STATES:
            raise ArtifactRowInvalid(f"receipt {identity} has state {receipt.state!r}")
        if identity in live:
            raise ArtifactRowInvalid(
                f"terminal receipt {identity} accompanies a live representation"
            )
        if receipt.record_id is not None and (
            receipt.record_id in memories or receipt.record_id in entities
        ):
            raise ArtifactRowInvalid(
                f"terminal receipt {identity} names a present record"
            )


def _validate_automatic(
    receipt: SourceReceiptRow, memory: MemoryRow, purposes: Mapping[UUID, set[str]]
) -> None:
    """§ A5's automatic-acceptance shape, re-checked on the way in.

    Only for the two automatic producer kinds: ``migration`` is the third receipt
    producer and is deliberately exempt, because it may be unbound and preserves each
    imported memory's own ratified purposes under its separately verified contract.
    """
    if receipt.producer_kind not in AUTOMATIC_PRODUCER_KINDS:
        return
    if memory.origin != _ORIGIN_DERIVED:
        raise ArtifactRowInvalid(
            f"automatic memory {memory.id} claims origin {memory.origin!r}"
        )
    if receipt.bound_purpose != AUTOMATIC_BOUND_PURPOSE:
        raise ArtifactRowInvalid(
            f"automatic receipt for {memory.id} binds {receipt.bound_purpose!r}"
        )
    if purposes.get(memory.id) != {AUTOMATIC_BOUND_PURPOSE}:
        raise ArtifactRowInvalid(
            f"automatic memory {memory.id} does not carry exactly its bound purpose"
        )
    if (receipt.audience_kind, receipt.audience_id) != (
        memory.audience_kind,
        memory.audience_id,
    ):
        raise ArtifactRowInvalid(
            f"automatic memory {memory.id} widens its receipt's audience"
        )
    if receipt.source_recorded_at is None or memory.recorded_at != (
        receipt.source_recorded_at
    ):
        raise ArtifactRowInvalid(
            f"automatic memory {memory.id} does not carry its original source clock"
        )


def _expiry_evidence(
    records: Sequence[DeletionRecordRow],
) -> dict[UUID, DeletionRecordRow]:
    """Every expiry record for one of this module's memories, by record id."""
    return {
        record.record_id: record
        for record in records
        if record.record_type == _QUALIFIED_MEMORY and record.cause == RETENTION_EXPIRY
    }


def _successor_of(
    identity: UUID,
    memories: Mapping[UUID, MemoryRow],
    expired: Mapping[UUID, DeletionRecordRow],
) -> UUID | None:
    """One hop along a supersession chain, whether the row is present or expired.

    A present row hops through its own ``superseded_by_id``. An absent one hops
    through the ``retained_successor_ref`` on its expiry record, which is the whole
    reason that column exists: the row is gone and the chain still has to be walkable.
    """
    present = memories.get(identity)
    if present is not None:
        return present.superseded_by_id
    record = expired.get(identity)
    if record is None or record.retained_successor_ref is None:
        return None
    try:
        return RecordRef.parse(record.retained_successor_ref).id
    except (RecordRefMalformed, TypeError):
        raise ArtifactRowInvalid(
            f"expiry evidence for {identity} names a malformed successor"
        ) from None


def _reaches(
    start: UUID,
    target: UUID,
    memories: Mapping[UUID, MemoryRow],
    expired: Mapping[UUID, DeletionRecordRow],
) -> bool:
    """Whether a chain runs from ``start`` to ``target``, and never cycles."""
    seen: set[UUID] = set()
    current: UUID | None = start
    while current is not None:
        if current == target:
            return True
        if current in seen:
            raise ArtifactRowInvalid(f"supersession chain through {current} cycles")
        seen.add(current)
        current = _successor_of(current, memories, expired)
    return False


def _validate_successors(parsed: _Parsed) -> None:
    """A12 step 3: every nonnull successor present, distinct, and acyclic.

    A **null** pointer on a durably superseded predecessor is valid and is not
    repaired here: it is what a legitimate successor removal leaves behind, and the
    row stays noncurrent because its invalidation says so rather than because a
    pointer says so.
    """
    memories = {row.id: row for row in parsed.memories}
    for row in parsed.memories:
        successor = row.superseded_by_id
        if successor is None:
            continue
        if successor == row.id:
            raise ArtifactRowInvalid(f"memory {row.id} supersedes itself")
        if successor not in memories:
            raise ArtifactRowInvalid(
                f"memory {row.id} names absent successor {successor}"
            )
        if _reaches(successor, row.id, memories, {}):
            raise ArtifactRowInvalid(f"supersession chain through {row.id} cycles")


def _validate_ancestry(
    parsed: _Parsed,
    purposes: Mapping[UUID, set[str]],
    records: Sequence[DeletionRecordRow],
) -> None:
    """A12 step 4, both branches, against the deletion evidence core just imported.

    The present branch and the absent branch are genuinely different rules and the
    difference is the point. A **present** predecessor must be durably superseded and
    must reach the replacement, and the replacement must carry all of its ordinary
    restrictions — both rows are here, so both can be compared. An **absent**
    predecessor has no restrictions left to compare, so the trusted export's copied
    ones are authoritative and what is checked instead is that its absence is
    *accounted for*: matching expiry evidence, and a chain from it to the replacement.

    Erasure evidence aborts either way. An erasure removed the content, and an
    ancestry edge that claimed to descend from it would be a claim about something
    that is gone.
    """
    memories = {row.id: row for row in parsed.memories}
    expired = _expiry_evidence(records)
    erased = {
        record.record_id
        for record in records
        if record.record_type == _QUALIFIED_MEMORY and record.cause != RETENTION_EXPIRY
    }
    for link in parsed.links:
        if not link.supersession_lineage:
            continue
        try:
            predecessor = RecordRef.parse(link.ref).id
        except (RecordRefMalformed, TypeError):
            raise ArtifactRowInvalid(
                f"marked ancestry on {link.ref!r} is not a canonical record reference"
            ) from None
        replacement = memories[link.memory_id]
        if predecessor in erased:
            raise ArtifactRowInvalid(
                f"marked ancestry on {link.ref!r} descends from an erasure"
            )
        present = memories.get(predecessor)
        if present is not None:
            if present.invalidation_reason != _SUPERSEDED_REASON:
                raise ArtifactRowInvalid(
                    f"marked ancestry names {link.ref!r}, which is not superseded"
                )
            _validate_restrictions(present, replacement, purposes)
        elif predecessor not in expired:
            raise ArtifactRowInvalid(
                f"marked ancestry names absent {link.ref!r} with no expiry evidence"
            )
        if not _reaches(predecessor, replacement.id, memories, expired):
            raise ArtifactRowInvalid(
                f"marked ancestry on {link.ref!r} has no path to its replacement"
            )


def _validate_restrictions(
    predecessor: MemoryRow, replacement: MemoryRow, purposes: Mapping[UUID, set[str]]
) -> None:
    """Every ordinary restriction of a **present** predecessor is on its replacement."""
    if (predecessor.audience_kind, predecessor.audience_id) != (
        replacement.audience_kind,
        replacement.audience_id,
    ):
        raise ArtifactRowInvalid(
            f"replacement {replacement.id} does not keep {predecessor.id}'s audience"
        )
    if not purposes.get(predecessor.id, set()) <= purposes.get(replacement.id, set()):
        raise ArtifactRowInvalid(
            f"replacement {replacement.id} drops a purpose {predecessor.id} carried"
        )


def _validate_ledger(records: Sequence[DeletionRecordRow]) -> None:
    """A12 step 1's ``retained_successor_ref`` rule (w2), on the ledger itself.

    **An unresolved successor is not by itself a defect**, and refusing one would be a
    false positive the ratified rule exists to prevent: A expires naming B, then B is
    separately and validly erased, and B's own row and record are both genuinely gone.
    What is refused is a chain that is actually broken — a reference that is not a
    record reference at all, or one that cycles back through the evidence.
    """
    by_record = {
        record.record_id: record
        for record in records
        if record.record_type == _QUALIFIED_MEMORY
    }
    for record in by_record.values():
        if record.retained_successor_ref is None:
            continue
        try:
            successor = RecordRef.parse(record.retained_successor_ref)
        except (RecordRefMalformed, TypeError):
            raise ArtifactRowInvalid(
                f"deletion evidence for {record.record_id} names "
                f"{record.retained_successor_ref!r}, which is not a record reference"
            ) from None
        seen = {record.record_id}
        walk: UUID | None = successor.id
        while walk is not None and walk in by_record:
            if walk in seen:
                raise ArtifactRowInvalid(f"deletion evidence through {walk} cycles")
            seen.add(walk)
            reference = by_record[walk].retained_successor_ref
            if reference is None:
                walk = None
                continue
            try:
                walk = RecordRef.parse(reference).id
            except (RecordRefMalformed, TypeError):
                raise ArtifactRowInvalid(
                    f"deletion evidence for {walk} names {reference!r}, "
                    "which is not a record reference"
                ) from None


def import_memory_records(
    uow: UnitOfWork, rows: Sequence[Mapping[str, object]]
) -> Callable[[], None]:
    """A12 steps 1-4's first pass; the returned callable is the second.

    Everything decidable from the artifact alone is decided before the first insert.
    Then parents go in with their self-references null — a successor may be written
    after the predecessor that names it, and a null is the only honest intermediate
    value — followed by entities, associations and receipts. The returned continuation
    is what core calls once its own deletion evidence has landed.

    **It commits nothing.** The whole restore is one workspace transaction; ending it
    here would take "any failure leaves no imported memory or deletion evidence" with
    it.
    """
    parsed = _parsed(rows)
    purposes = _validate_structure(parsed)
    _validate_receipts(parsed, purposes)
    _validate_successors(parsed)
    connection = uow.connection
    for memory in parsed.memories:
        insert_memory(
            connection,
            MemoryRow(**{**_fields(memory), "superseded_by_id": None}),
        )
    for entity in parsed.entities:
        insert_memory_entity(connection, entity)
    for purpose in parsed.purposes:
        insert_memory_purpose(connection, purpose)
    for mention in parsed.mentions:
        insert_memory_mention(connection, mention)
    for link in parsed.links:
        insert_memory_link(connection, link)
    for receipt in parsed.receipts:
        insert_source_receipt(connection, receipt)

    def finish() -> None:
        records = list_deletion_records(connection)
        _validate_ledger(records)
        _validate_ancestry(parsed, purposes, records)
        for memory in parsed.memories:
            if memory.superseded_by_id is not None:
                set_memory_successor(connection, memory.id, memory.superseded_by_id)

    return finish


def _fields(row: MemoryRow) -> dict[str, Any]:
    """One frozen row as a keyword mapping, without reaching for ``dataclasses``.

    ``dataclasses.asdict`` deep-copies and would turn the nested values into their own
    dicts; ``__slots__`` means there is no ``__dict__`` to read. Naming the slots is
    the cheap, exact answer.
    """
    return {name: getattr(row, name) for name in MemoryRow.__slots__}
