"""Delivery: merged sightings become intake deliveries in the work transaction.

Each merged sighting is classified once (discovery, refresh or skip), compared with
the posting's stored field digests, and, when it is worth delivering, accepted
through ``leads.intake.accept_delivery`` under an event id that is never reused:

    ``<posting_id>.<seq>.<first 16 hex of the canonical body digest>``

The body digest leaves out ``observed_at`` and ``proposal_observed_at`` (both are
stamped at response time), so identical sightings at one sequence number share an
id. The per-posting sequence number comes from ``state.next_seq``.

Stored field digests are text: ``name=<16 hex>`` pairs sorted by name and joined by
``;`` (:data:`DIGEST_PAIR_SEPARATOR`). Field names are lower-case letters and
underscores and digests are hex, so neither ``;`` nor ``=`` can occur inside one.

Each acceptance runs in its own savepoint. The caller holds the workspace lifecycle
lock before the first one, so rolling a savepoint back never releases it.
"""

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Final
from uuid import UUID

from rheo_contracts import WorkspaceContext
from rheo_core.operations.refusals import OperationRefused
from rheo_core.operations.transaction import call_in_transaction
from rheo_core.refs.resolver import UnitOfWork

from rheo_leads.contracts import AcceptedDelivery
from rheo_leads.jobfeed import state
from rheo_leads.jobfeed.collect import OVER_CAP_BAND, Sighting
from rheo_leads.jobfeed.install import POSTING_ID
from rheo_leads.jobfeed.normalize import (
    COMPARABLE_FIELDS,
    EVENT_ID_SEPARATOR,
    NormalizedPosting,
    posting_id_valid,
)
from rheo_leads.jobfeed.presets import DISCOVERY_CAP
from rheo_leads.pipeline_intake import tracked_opportunity
from rheo_leads.references import ref

ACCEPT_OPERATION: Final = "leads.intake.accept_delivery"
DIGEST_LENGTH: Final = 16
DIGEST_PAIR_SEPARATOR: Final = ";"
DIGEST_NAME_SEPARATOR: Final = "="
# Both are stamped at response time, so neither may make two sightings differ.
TIME_FIELDS: Final = ("observed_at", "proposal_observed_at")
_HEX: Final = frozenset("0123456789abcdef")

Body = dict[str, str | int]


def _short_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:DIGEST_LENGTH]


def _canonical(body: Mapping[str, object]) -> str:
    return json.dumps(
        dict(body), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def canonical_body_digest(body: Mapping[str, object]) -> str:
    """First 16 hex of the SHA-256 of the canonical JSON body, without both times."""
    return _short_digest(
        _canonical({k: v for k, v in body.items() if k not in TIME_FIELDS})
    )


def compose_event_id(posting_id: str, seq: int, digest: str) -> str:
    """``"<posting_id>.<seq>.<digest>"``. Refuses a posting id that is not exactly
    "~02" and digits (one with the separator in it would corrupt the sequence read),
    a sequence below 1 and a digest that is not 16 lower-case hex."""
    if not posting_id_valid(posting_id) or EVENT_ID_SEPARATOR in posting_id:
        raise ValueError("posting id is not a valid identifier")
    if type(seq) is not int or seq < 1:
        raise ValueError("sequence number must be a positive integer")
    if len(digest) != DIGEST_LENGTH or not set(digest) <= _HEX:
        raise ValueError("digest must be 16 lower-case hex characters")
    return EVENT_ID_SEPARATOR.join((posting_id, str(seq), digest))


def field_digests(posting: NormalizedPosting) -> dict[str, str]:
    """Per comparable field the posting carries: name to its value's short digest."""
    return {
        name: _short_digest(value)
        for name, value in posting.comparable_fields().items()
    }


def format_digests(digests: Mapping[str, str]) -> str:
    """The stored text: sorted ``name=<hex>`` pairs joined by ``;``."""
    return DIGEST_PAIR_SEPARATOR.join(
        f"{name}{DIGEST_NAME_SEPARATOR}{digests[name]}" for name in sorted(digests)
    )


def parse_digests(text: str) -> dict[str, str]:
    """Read :func:`format_digests` text. A malformed pair is ignored, so its field
    counts as never stored (a change), never as unchanged."""
    digests: dict[str, str] = {}
    for pair in text.split(DIGEST_PAIR_SEPARATOR) if text else ():
        name, separator, digest = pair.partition(DIGEST_NAME_SEPARATOR)
        if (
            separator
            and name in COMPARABLE_FIELDS
            and len(digest) == DIGEST_LENGTH
            and set(digest) <= _HEX
        ):
            digests[name] = digest
    return digests


def unchanged(carried: Mapping[str, str], stored: Mapping[str, str] | None) -> bool:
    """True when every carried field digest equals the stored one. A field the
    sighting does not carry is not a change; no stored row is always a change."""
    if stored is None:
        return False
    return all(stored.get(name) == digest for name, digest in carried.items())


def delivered_posting(sighting: Sighting) -> NormalizedPosting:
    """The posting as delivered: a description (and the detail-only hire rate) only
    from a detail read that succeeded in this run, never a feed snippet."""
    if sighting.detail_ok:
        return sighting.posting
    return replace(sighting.posting, description=None, client_hire_rate=None)


def encode_body(body: Mapping[str, object]) -> str:
    """The delivery body's text: canonical JSON with the times included."""
    return _canonical(body)


def _over_cap(posting: NormalizedPosting) -> bool:
    count = posting.proposal_count
    return count is not None and count > DISCOVERY_CAP


def discovery_eligible(sighting: Sighting) -> bool:
    """At most 50 proposals, not applied, from a discovery search, detail read ok."""
    posting = sighting.posting
    return (
        sighting.detail_ok
        and posting.purpose == "discover"
        and not posting.applied
        and posting.count_band_label != OVER_CAP_BAND
        and not _over_cap(posting)
    )


@dataclass
class DeliveryResult:
    """Content-free counts over merged sightings."""

    imported: int = 0
    refreshed: int = 0
    skipped_dup: int = 0
    filtered: int = 0
    skipped_untracked: int = 0
    errors: int = 0
    partial: bool = False


@dataclass(frozen=True)
class _Planned:
    posting: NormalizedPosting
    body: Body
    carried: dict[str, str]
    stored: dict[str, str] | None
    tracked: bool


def deliver(
    uow: UnitOfWork,
    conn_ctx: WorkspaceContext,
    *,
    connection_id: UUID,
    pipeline_id: UUID,
    sightings: Sequence[Sighting],
    now: datetime,
    checkpoint: Callable[[], None],
) -> DeliveryResult:
    """Classify and deliver ``sightings`` (one per posting id) in the caller's
    transaction, which already holds the workspace lifecycle lock.

    ``conn_ctx`` is the poll connection's own acceptance context. ``checkpoint`` is
    called between deliveries. An acceptance intake refuses rolls back its own
    savepoint only; any other exception propagates.
    """
    ids = [sighting.posting.posting_id for sighting in sightings]
    if len(set(ids)) != len(ids):
        raise ValueError("deliver takes one merged sighting per posting")
    result = DeliveryResult()
    rows = state.read_postings(uow, connection_id=connection_id, posting_ids=ids)
    planned: list[_Planned] = []
    for sighting in sightings:
        posting = delivered_posting(sighting)
        if posting.applied or _over_cap(posting):
            result.filtered += 1
        row = rows.get(posting.posting_id)
        stored = None if row is None else parse_digests(row.field_digests)
        carried = field_digests(posting)
        tracked = (
            tracked_opportunity(
                uow, pipeline_id=pipeline_id, field=POSTING_ID, value=posting.posting_id
            )
            is not None
        )
        if not discovery_eligible(sighting) and not tracked:
            result.skipped_untracked += 1
        elif unchanged(carried, stored):
            result.skipped_dup += 1
        else:
            planned.append(
                _Planned(posting, posting.to_body(), carried, stored, tracked)
            )
    sequences = state.next_seq(
        uow,
        connection_id=connection_id,
        posting_ids=[plan.posting.posting_id for plan in planned],
    )
    for index, plan in enumerate(planned):
        if index:
            checkpoint()
        _accept(uow, conn_ctx, connection_id, plan, sequences, now, result)
    return result


def _accept(
    uow: UnitOfWork,
    conn_ctx: WorkspaceContext,
    connection_id: UUID,
    plan: _Planned,
    sequences: Mapping[str, int],
    now: datetime,
    result: DeliveryResult,
) -> None:
    posting = plan.posting
    seq = sequences[posting.posting_id]
    event_id = compose_event_id(
        posting.posting_id, seq, canonical_body_digest(plan.body)
    )
    stored = plan.stored or {}
    try:
        with uow.connection.begin_nested():
            accepted = call_in_transaction(
                conn_ctx,
                uow,
                ACCEPT_OPERATION,
                {
                    "connection_ref": ref("intake_connection", connection_id),
                    "source_event_id": event_id,
                    "body": encode_body(plan.body),
                    "source_occurred_at": posting.observed_at.isoformat(),
                },
            )
            outcome = (
                accepted.outcome if isinstance(accepted, AcceptedDelivery) else None
            )
            if outcome not in {"accepted", "duplicate", "conflict"}:
                raise ValueError("unexpected acceptance outcome")
            # A conflict consumes the number but leaves the stored digests: the
            # content was not accepted. Intake's own conflict signal stays its own.
            conflict = outcome == "conflict"
            state.upsert_posting(
                uow,
                connection_id=connection_id,
                posting_id=posting.posting_id,
                seq=seq,
                merged_digests=format_digests(
                    stored if conflict else {**stored, **plan.carried}
                ),
                last_event_id=event_id,
                now=now,
                keep_digests=conflict,
            )
    except OperationRefused:
        # Nothing of this row remains, so its number is still free next run.
        result.errors += 1
        result.partial = True
        return
    if outcome == "accepted":
        if plan.tracked:
            result.refreshed += 1
        else:
            result.imported += 1
    elif outcome == "duplicate":
        result.skipped_dup += 1
    else:
        result.errors += 1
        result.partial = True
