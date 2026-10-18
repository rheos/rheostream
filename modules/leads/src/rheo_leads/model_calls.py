"""The model call log: one row per model call Leads makes, for the Scoring view.

A sweep that asks the model wraps each call in :meth:`Tracker.call`. The row is
written as ``running`` in its own short unit before the call starts, so the Scoring
view can say what the worker is doing, and finished in another short unit when the
call ends: ``answered`` when the model answered (and the caller could use it),
``deferred`` when the account's usage limit refused it, ``failed`` otherwise, with the
fixed failure word. Token counts come from the adapter's usage report through the
runner's ``on_usage``; a run that reports none leaves them empty.

**Content-free.** A row holds a purpose word, a job's identifier, the model setting's
value, two times, two counts and an outcome word. No task, answer, posting text or
adapter detail reaches it.

**Bounded.** Starting a call also deletes rows older than :data:`RETENTION`, so the
log keeps a rolling window without a job of its own.
"""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Final
from uuid import UUID

from rheo_contracts import WorkspaceContext
from rheo_core.refs import uuid7
from rheo_core.runtime.oneshot import (
    OneShotCredentialNotOwned,
    OneShotDeferred,
    OneShotFailed,
)
from rheo_core.settings import resolve
from rheo_core.settings.storage_source import TransactionBoundOverrideSource
from rheo_core.storage.routing import open_unit_of_work
from sqlalchemy import delete, insert, update

from rheo_leads.storage import scoring as s

RETENTION: Final = timedelta(days=90)
SCORING: Final = "scoring"
CREDENTIAL_NOT_OWNED: Final = "credential_not_owned"
UNEXPECTED: Final = "unexpected"
"""The failure word for a call that ended in an exception nobody expected."""


class Call:
    """What the caller reports about one call while it runs."""

    def __init__(self) -> None:
        self.input_tokens: int | None = None
        self.output_tokens: int | None = None
        self.unusable_kind: str | None = None

    def usage(self, input_tokens: int, output_tokens: int) -> None:
        """The runner's ``on_usage``."""
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens

    def unusable(self, kind: str) -> None:
        """The model answered, but the answer could not be used: a failed call."""
        self.unusable_kind = kind


class Tracker:
    """Records the model calls made for one job (or none) in one workspace."""

    def __init__(
        self,
        ctx: WorkspaceContext,
        opportunity_id: UUID | None,
        clock: Callable[[], datetime],
    ) -> None:
        self._ctx = ctx
        self._opportunity_id = opportunity_id
        self._clock = clock

    @contextmanager
    def call(self, purpose: str, model_id_key: str) -> Iterator[Call]:
        identifier = uuid7()
        started = self._clock()
        table = s.model_call
        with open_unit_of_work(self._ctx) as uow:
            uow.connection.execute(
                delete(table).where(table.c.started_at < started - RETENTION)
            )
            settings = resolve(
                workspace_id=self._ctx.workspace_id,
                source=TransactionBoundOverrideSource(
                    uow, workspace_id=self._ctx.workspace_id
                ),
            )
            uow.connection.execute(
                insert(table).values(
                    id=identifier,
                    purpose=purpose,
                    opportunity_id=self._opportunity_id,
                    model_id=settings.get_str(model_id_key).strip() or "default",
                    started_at=started,
                    outcome="running",
                )
            )
            uow.commit()
        call = Call()
        outcome: str = "failed"
        kind: str | None = UNEXPECTED
        try:
            yield call
            outcome, kind = (
                ("failed", call.unusable_kind)
                if call.unusable_kind is not None
                else ("answered", None)
            )
        except OneShotDeferred as exc:
            outcome, kind = "deferred", exc.kind
            raise
        except OneShotFailed as exc:
            kind = exc.kind
            raise
        except OneShotCredentialNotOwned:
            kind = CREDENTIAL_NOT_OWNED
            raise
        finally:
            with open_unit_of_work(self._ctx) as uow:
                uow.connection.execute(
                    update(table)
                    .where(table.c.id == identifier)
                    .values(
                        finished_at=max(self._clock(), started),
                        input_tokens=call.input_tokens,
                        output_tokens=call.output_tokens,
                        outcome=outcome,
                        failure_kind=kind,
                    )
                )
                uow.commit()


class NoTracking:
    """A tracker that records nothing: for callers outside a sweep, and tests."""

    @contextmanager
    def call(self, purpose: str, model_id_key: str) -> Iterator[Call]:
        yield Call()


NO_TRACKING: Final = NoTracking()
CallTracker = Tracker | NoTracking
