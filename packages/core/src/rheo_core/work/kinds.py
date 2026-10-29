"""The job-kind registry: which ``kind`` strings a worker may run, and as what.

A plain class with no module-level instance. ``worker_loop`` takes a
:class:`JobKindRegistry` as a parameter, mirroring ``dispatch(..., registry=REGISTRY)``
in ``operations/dispatch.py``: a test builds its own and there is no global to reset
between cases. Production's one process-wide instance is built and populated in
``apps/worker/src/rheo_app_worker/main.py``, not here.

**An unknown kind is a terminal failure, never a silent skip**, and so is a registered
kind whose stored ``input`` fails its model. Neither can ever succeed on a later
attempt, so retrying one burns the whole budget and then files it under ``failed`` with
a misleading error. This module raises :class:`JobKindUnknown` for the first case and
lets pydantic's own ``ValidationError`` out of the caller's
``input_model.model_validate(...)`` for the second; ``work/loop.py`` turns both into
one ``finish_failed`` with a named error.

*The deploy-skew hazard that comes with that, recorded rather than deferred silently.*
Release one ships every job kind in the same image as the worker, so a kind the worker
does not know is a kind nothing enqueues. Once module-provided kinds exist, a worker
that has not yet been upgraded would terminally fail work a newer core enqueued, and
the run that introduces them has to revisit this rule.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING
from uuid import UUID

from pydantic import BaseModel

from rheo_core.storage.backend import HandlerUnitOfWork, UnitOfWork
from rheo_core.work.cancellation import CancellationToken

if TYPE_CHECKING:
    from rheo_core.events.consumers import ConsumerRegistry

JobHandler = Callable[[HandlerUnitOfWork, BaseModel, CancellationToken], None]
"""``(uow, payload, token) -> None``: what one job kind actually does.

Three parameters and **no clock**. The handler calls ``token.checkpoint()`` with no
argument and the token supplies the loop's own ``now`` from the clock it captured,
which is what keeps a handler from reaching for ``datetime.now(UTC)`` inside a run
leased under an injected one.

``uow`` is the **sealed** view, ``HandlerUnitOfWork``, not a raw ``UnitOfWork``: the
handler's effects and the row's ``succeeded`` write commit together, in the loop's one
transaction, and a handler calling ``commit()`` is precisely what would separate them.
The seal refuses ``commit``, ``rollback`` and ``__enter__``.

**The contract a handler must be written to:** a handler that runs longer than
``loop.LEASE_SECONDS`` without calling ``checkpoint()`` is **treated as dead** — its
lease is stolen and its work rolled back at finish. Checkpointing is not optional for
long work.
"""


class ContinuationMode(StrEnum):
    """Why the loop is running a continuation kind this time.

    ``run`` may perform the effect at most once. ``cancel_requested`` and
    ``budget_exhausted_recovery`` reconcile only: they never start a new effect.
    """

    RUN = "run"
    CANCEL_REQUESTED = "cancel_requested"
    BUDGET_EXHAUSTED_RECOVERY = "budget_exhausted_recovery"


class ContinuationDisposition(StrEnum):
    """The closed set a continuation returns; the loop maps each to one job write.

    ``succeeded`` and ``handled_terminal`` end the job ``succeeded``, ``retry``
    requeues it with backoff, ``failed_terminal`` ends it ``failed`` and
    ``cancelled_terminal`` ends it ``cancelled``. The continuation has already written
    every related row in the same transaction.
    """

    SUCCEEDED = "succeeded"
    HANDLED_TERMINAL = "handled_terminal"
    RETRY = "retry"
    FAILED_TERMINAL = "failed_terminal"
    CANCELLED_TERMINAL = "cancelled_terminal"


@dataclass(frozen=True, slots=True)
class ContinuationLease:
    """What the loop hands a continuation about the job it leased."""

    workspace_id: UUID
    job_id: UUID
    owner: str
    payload: dict[str, object]
    attempts: int
    max_attempts: int
    mode: ContinuationMode
    now: datetime
    consumers: "ConsumerRegistry | None" = None


@dataclass(frozen=True, slots=True)
class ContinuationResult:
    """A disposition and the short text the job row records with it."""

    disposition: ContinuationDisposition
    detail: str | None = None


ContinuationHandler = Callable[[UnitOfWork, ContinuationLease], ContinuationResult]
"""``(uow, lease) -> result``: a core continuation kind.

Unlike :data:`JobHandler` it receives the raw unit of work, because it owns the
settlement of its own durable rows, and the lease facts (attempts, budget, mode),
because it, not the loop, decides what a cancelled or exhausted lease means. It must
not commit; the loop writes the job's terminal or retry state in the same
transaction and commits only when that write applied."""


class JobKindUnknown(Exception):
    """Raised by :meth:`JobKindRegistry.lookup` for a ``kind`` nobody registered."""


class JobKindRegistry:
    """``kind -> (input model, handler)``, and nothing else.

    Registration is deliberately last-writer-wins rather than a refusal: the one
    production instance is populated once at import of the composition root, and a
    test that re-registers a kind inside a single case is expressing intent rather
    than colliding with another caller. The one refusal is across the two maps: a
    kind is either ordinary or a continuation, never both.
    """

    __slots__ = ("_continuations", "_kinds")

    def __init__(self) -> None:
        self._kinds: dict[str, tuple[type[BaseModel], JobHandler]] = {}
        self._continuations: dict[str, ContinuationHandler] = {}

    def register_continuation(self, kind: str, handler: ContinuationHandler) -> None:
        """Declare a core-only continuation kind.

        Kept apart from :meth:`register` so ordinary handlers keep their ``None``
        contract and a module cannot register one: the name must be ``core.``.
        """
        if not isinstance(kind, str) or not kind.startswith("core."):
            raise ValueError("a continuation kind is a core-owned 'core.' kind")
        if kind in self._kinds:
            raise ValueError(f"{kind!r} is already an ordinary job kind")
        self._continuations[kind] = handler

    def continuation_for(self, kind: str) -> ContinuationHandler | None:
        """The continuation registered for ``kind``, or ``None``."""
        return self._continuations.get(kind)

    def continuation_names(self) -> frozenset[str]:
        return frozenset(self._continuations)

    def register(
        self, kind: str, input_model: type[BaseModel], handler: JobHandler
    ) -> None:
        """Declare that ``kind`` is run by ``handler`` with ``input_model``.

        A kind already registered as a continuation is refused: the loop looks
        continuations up first, so this handler would be silently shadowed.
        """
        if not isinstance(kind, str) or not kind:
            raise ValueError("a job kind is a non-empty string")
        if kind in self._continuations:
            raise ValueError(f"{kind!r} is already a continuation kind")
        self._kinds[kind] = (input_model, handler)

    def names(self) -> frozenset[str]:
        """The registered kind strings. Empty until something calls :meth:`register`."""
        return frozenset(self._kinds)

    def lookup(self, kind: str) -> tuple[type[BaseModel], JobHandler]:
        """The model and handler for ``kind``, or :class:`JobKindUnknown`."""
        found = self._kinds.get(kind)
        if found is None:
            raise JobKindUnknown(f"no handler is registered for job kind {kind!r}")
        return found
