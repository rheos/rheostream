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

from pydantic import BaseModel

from rheo_core.storage.backend import HandlerUnitOfWork
from rheo_core.work.cancellation import CancellationToken

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


class JobKindUnknown(Exception):
    """Raised by :meth:`JobKindRegistry.lookup` for a ``kind`` nobody registered."""


class JobKindRegistry:
    """``kind -> (input model, handler)``, and nothing else.

    Registration is deliberately last-writer-wins rather than a refusal: the one
    production instance is populated once at import of the composition root, and a
    test that re-registers a kind inside a single case is expressing intent rather
    than colliding with another caller.
    """

    __slots__ = ("_kinds",)

    def __init__(self) -> None:
        self._kinds: dict[str, tuple[type[BaseModel], JobHandler]] = {}

    def register(
        self, kind: str, input_model: type[BaseModel], handler: JobHandler
    ) -> None:
        """Declare that ``kind`` is run by ``handler`` with ``input_model``."""
        if not isinstance(kind, str) or not kind:
            raise ValueError("a job kind is a non-empty string")
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
