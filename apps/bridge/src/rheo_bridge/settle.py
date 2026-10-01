"""Post or hold, settle and close the bridge worker's collected batches."""

from __future__ import annotations

import errno
import os
import sqlite3
import stat
from collections import Counter
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Final, assert_never

from rheo_bridge import config as bridge_config
from rheo_bridge import keys, state, transcript
from rheo_bridge.client import (
    IngestClient,
    IngestGap,
    IngestRecord,
    IngestRefused,
    IngestResponse,
    IngestTransportError,
)
from rheo_bridge.transcript import Missing, Ok, Refused

if TYPE_CHECKING:
    from rheo_bridge.worker import SessionBatch

HOLD_BASE_SECONDS: Final = 15 * 60
HOLD_CAP_SECONDS: Final = 6 * 60 * 60
TOKEN_EXPIRY_WARNING: Final = timedelta(days=14)
_GONE_ERRNOS: Final = frozenset({errno.ELOOP, errno.ENOENT, errno.ENXIO})
_OPEN_FLAGS: Final = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK


class _Outcome(Enum):
    PROGRESS = "progress"  # a cursor moved or a session closed
    SETTLED = "settled"  # answered (or nothing to ask), but nothing moved
    HELD = "held"
    REFUSED = "refused"


@dataclass(frozen=True)
class _PassContext:
    connection: sqlite3.Connection
    config: bridge_config.Config
    machine_key: bytes
    projects_root: Path
    client: IngestClient
    now: datetime
    # The hold as the run found it; the drain loops only when its step was 0.
    hold: state.Hold


@dataclass(frozen=True)
class _Answer:
    accepted: frozenset[str]
    gapped: frozenset[str]
    # accepted, gapped or dropped: the server holds the key for good.
    acknowledged: frozenset[str]


_NO_ANSWER: Final = _Answer(frozenset(), frozenset(), frozenset())
_ENROLLMENT_REFUSALS: Final = frozenset({"enrollment_inactive", "enrollment_mismatch"})
_REFUSAL_BY_STATUS: Final = {
    401: "token_rejected",
    403: "token_not_permitted",
    422: "input_invalid",
}


def token_expiring(config: bridge_config.Config, now: datetime) -> bool:
    """Whether the token expires within 14 days of ``now``, or already has.

    An expiry that does not parse, or none stored yet, counts as expiring, so
    a person looks.
    """
    if config.token_expires_at is None:
        return True
    try:
        expires = datetime.fromisoformat(config.token_expires_at)
    except ValueError:
        return True
    if expires.tzinfo is None or expires.utcoffset() is None:
        expires = expires.replace(tzinfo=UTC)
    return expires - now <= TOKEN_EXPIRY_WARNING


def _items(batch: SessionBatch) -> list[IngestRecord | IngestGap]:
    """The batch's keyed items in submission order: the source gap first."""
    lines = [candidate.item for candidate in batch.items]
    return lines if batch.source_gap is None else [batch.source_gap, *lines]


def _probe(batches: list[SessionBatch]) -> SessionBatch | None:
    """A held worker's one-item batch: the oldest unacknowledged item.

    Only a session's first item is eligible, because the cursor cannot pass a
    later one while an earlier one is still unacknowledged. The batch returned
    is cut down to that item, so its range ends where the item's line ends.
    """
    chosen: SessionBatch | None = None
    chosen_at: datetime | None = None
    for batch in batches:
        items = _items(batch)
        if items and (chosen_at is None or items[0].recorded_at < chosen_at):
            chosen, chosen_at = batch, items[0].recorded_at
    if chosen is None:
        return None
    if chosen.source_gap is not None:
        return replace(chosen, items=(), skips=(), end_offset=chosen.start_offset)
    head = chosen.items[0]
    return replace(
        chosen,
        items=(head,),
        skips=tuple(s for s in chosen.skips if s.end_offset <= head.end_offset),
        end_offset=head.end_offset,
    )


def _refusal_reason(refusal: IngestRefused) -> str:
    if refusal.error_code in _ENROLLMENT_REFUSALS:
        return refusal.error_code
    # The client admits no other status, so the fallback is never reached.
    return _REFUSAL_BY_STATUS.get(refusal.http_status, "input_invalid")


def _hold(context: _PassContext) -> None:
    """Back off: ``now + 15 min * 2**step``, capped at 6 h, and step once more."""
    step = context.hold.hold_step
    seconds = min(HOLD_BASE_SECONDS * 2 ** min(step, 16), HOLD_CAP_SECONDS)
    state.set_hold(
        context.connection,
        hold_until=int(context.now.timestamp()) + seconds,
        hold_step=step + 1,
    )


def _post_and_settle(
    context: _PassContext, batches: list[SessionBatch], *, probe: bool
) -> _Outcome:
    """Step 6 and step 7 for one pass.

    A range with nothing to send (only skipped lines, or none) commits
    locally first, with no request, so it moves whatever the request's
    outcome. Everything an answer moves, in every session, commits in one
    SQLite transaction with the hold: a crash before that commit leaves the
    state as it was before the request, and the next run resends a range the
    server answers identically.
    """
    connection = context.connection
    local = [batch for batch in batches if not _items(batch)]
    moved = False
    with state.transaction(connection):
        for batch in local:
            moved = _settle(context, batch, _NO_ANSWER) or moved
    if probe:
        chosen = _probe(batches)
        posted = [] if chosen is None else [chosen]
    else:
        posted = [batch for batch in batches if _items(batch)]
    items = [item for batch in posted for item in _items(batch)]
    answer = _NO_ANSWER
    if items:
        try:
            response = context.client.post(
                keys.machine_fingerprint(context.machine_key),
                keys.enrollment_project_fingerprint(context.config.enrolled_dir),
                [item for item in items if isinstance(item, IngestRecord)],
                [item for item in items if isinstance(item, IngestGap)],
            )
        except IngestTransportError:
            _hold(context)
            return _Outcome.HELD
        except IngestRefused as refusal:
            # No resend loop: the hold turns later runs into one probe per
            # window until a person fixes the token or the enrollment.
            with state.transaction(connection):
                state.bump_ledger(
                    connection,
                    _refusal_reason(refusal),
                    at=int(context.now.timestamp()),
                )
                _hold(context)
            return _Outcome.REFUSED
        answer = _answer(response)
        if not any(item.native_key in answer.acknowledged for item in items):
            # All deferred (recording off): nothing moves.
            _hold(context)
            return _Outcome.HELD
    with state.transaction(connection):
        for batch in posted:
            moved = _settle(context, batch, answer) or moved
        if items:
            state.clear_hold(connection)
    return _Outcome.PROGRESS if moved else _Outcome.SETTLED


def _answer(response: IngestResponse) -> _Answer:
    return _Answer(
        accepted=frozenset(response.accepted),
        gapped=frozenset(response.gapped),
        acknowledged=frozenset(
            (*response.accepted, *response.gapped, *response.dropped)
        ),
    )


def _settle(context: _PassContext, batch: SessionBatch, answer: _Answer) -> bool:
    """Commit what the answer acknowledged for one session; close it if done.

    Returns whether the cursor moved or the session closed. Runs inside the
    caller's transaction.
    """
    connection = context.connection
    row = state.get_session(connection, batch.session_hash)
    if row is None:
        return False
    if batch.source_gone:
        return _settle_gone(context, batch, row, answer)
    if (
        batch.source_gap is not None
        and batch.source_gap.native_key not in answer.acknowledged
    ):
        # A replaced source: nothing read from the new file may be committed
        # before the server holds the gap for the old one.
        return False
    # The cursor stops at the start of the first unacknowledged line, and
    # every line before it, candidate or not, is passed.
    target = batch.end_offset
    for candidate in batch.items:
        if candidate.item.native_key not in answer.acknowledged:
            target = candidate.start_offset
            break
    _count_passed(context, batch, target, answer)
    moved = target != row.cursor_offset or batch.inode != row.cursor_inode
    if moved:
        state.advance_cursor(
            connection, batch.session_hash, offset=target, inode=batch.inode
        )
    if target == batch.end_offset and _settled(context, row):
        return _close_at_eof(context, row, target, batch.inode) or moved
    return moved


def _count_passed(
    context: _PassContext, batch: SessionBatch, target: int, answer: _Answer
) -> None:
    """Bump the counts owed by the lines the cursor now passes."""
    connection = context.connection
    at = int(context.now.timestamp())
    counts: Counter[str] = Counter()
    for skip in batch.skips:
        if skip.end_offset <= target:
            counts[skip.reason] += 1
    for candidate in batch.items:
        entrypoint = candidate.entrypoint
        if (
            candidate.end_offset <= target
            and isinstance(candidate.item, IngestRecord)
            and candidate.item.native_key in answer.accepted
            and entrypoint is not None
            # An odd value is not counted rather than stored in the ledger.
            and state.is_client_kind(entrypoint)
        ):
            counts[state.accepted_reason(entrypoint)] += 1
    for reason, count in sorted(counts.items()):
        state.bump_ledger(connection, reason, at=at, by=count)


def _settled(context: _PassContext, row: state.SessionRow) -> bool:
    """Whether this pass's read counts as the session's settle re-read.

    [capture 5, 7] — may be revised at reconciliation. The session ended (or,
    with no ``SessionEnd``, was last seen more than ``max_pending_hours`` ago)
    at least ``settle_seconds`` before this pass read it.
    """
    now = int(context.now.timestamp())
    ended = row.ended_at
    if ended is None:
        idle = context.config.max_pending_hours * 3600
        if row.last_seen_at is None or now - row.last_seen_at <= idle:
            return False
        ended = row.last_seen_at
    return now >= ended + context.config.settle_seconds


def _close_at_eof(
    context: _PassContext, row: state.SessionRow, target: int, inode: int | None
) -> bool:
    """Rule (a): close the session when the committed cursor sits at EOF."""
    match _source_now(context, row):
        case _Present(size=size, inode=current) if size == target and current == inode:
            _close(context, row)
            return True
        case _Present() | _Gone() | _Unreadable():
            # More arrived, or the source went: a later pass deals with it.
            return False
        case Refused(reason=reason):
            _drop_refused(context, row, reason)
            return True
        case unexpected:
            assert_never(unexpected)


def _settle_gone(
    context: _PassContext,
    batch: SessionBatch,
    row: state.SessionRow,
    answer: _Answer,
) -> bool:
    """Rule (b): the source's gap came back ``gapped`` and it is still gone."""
    assert batch.source_gap is not None
    if batch.source_gap.native_key not in answer.gapped:
        return False
    match _source_now(context, row):
        case _Gone():
            _close(context, row)
            return True
        case _Present():
            # It came back: the server holds the gap, and the next pass reads
            # the file (a replacement replays it from 0 under the same gap key).
            state.set_pending_gap_key(context.connection, row.session_hash, None)
            return False
        case _Unreadable():
            return False
        case Refused(reason=reason):
            _drop_refused(context, row, reason)
            return True
        case unexpected:
            assert_never(unexpected)


def _close(context: _PassContext, row: state.SessionRow) -> None:
    state.close_session(
        context.connection, row.session_hash, at=int(context.now.timestamp())
    )


def _drop_refused(
    context: _PassContext, row: state.SessionRow, reason: transcript.RefusalReason
) -> None:
    state.bump_ledger(context.connection, reason, at=int(context.now.timestamp()))
    state.delete_session(context.connection, row.session_hash)


@dataclass(frozen=True)
class _Present:
    size: int
    inode: int


@dataclass(frozen=True)
class _Gone:
    pass


@dataclass(frozen=True)
class _Unreadable:
    pass


def _source_now(
    context: _PassContext, row: state.SessionRow
) -> _Present | _Gone | _Unreadable | Refused:
    """Re-validate a session's source at close, the way step 2 and step 3 do.

    ``Missing.path`` is never opened; an admitted path is opened with
    ``O_NOFOLLOW`` and checked on its descriptor.
    """
    if row.transcript_path is None:
        return Refused("path_escape")
    verdict = transcript.validate_source(
        row.transcript_path,
        context.config.enrolled_dir,
        projects_root=context.projects_root,
    )
    match verdict:
        case Refused():
            return verdict
        case Missing():
            return _Gone()
        case Ok(path=path):
            return _stat_source(path)
        case _:
            assert_never(verdict)


def _stat_source(path: Path) -> _Present | _Gone | _Unreadable:
    try:
        fd = os.open(path, _OPEN_FLAGS)
    except OSError as exc:
        return _Gone() if exc.errno in _GONE_ERRNOS else _Unreadable()
    try:
        info = os.fstat(fd)
    except OSError:
        return _Unreadable()
    finally:
        os.close(fd)
    if not stat.S_ISREG(info.st_mode):
        return _Gone()
    return _Present(size=info.st_size, inode=info.st_ino)
