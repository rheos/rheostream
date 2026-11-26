"""Finance's two owner writes as core operations: reported paid and its undo (#639,
ADR-0030).

``WRITE_SPECS`` names them, ``finance.obligation.report_paid`` and
``finance.obligation.unreport_paid`` (``rheo_finance.reported``), and nothing else:
every other finance write stays with the ``finance`` command. Each is declared MUTATE
for the owner alone, with no tool, so no token, package set or model reaches it, and
the web screens submit it through the form path (``forms`` in the manifest).

``write_handler`` runs one spec's handler on core's unit of work under the
workspace's layout, as ``finance_owner`` (``SET LOCAL ROLE``, reset before it returns)
and under the ledger writer lock, waited for ``WAIT_SECONDS`` at most, so a click and a
tick's derive never interleave: the derive reads the decisions when it starts. Before
anything, it refuses every caller but the workspace owner's own signed-in browser
session (``reads.owner_session``) with ``OWNER_SESSION_ONLY``. Failures leave as the
reads' do: a fixed code from ``wiring.errors``, else ``WRITE_FAILED``, nothing chained.
"""

from __future__ import annotations

from typing import Final

from pydantic import BaseModel
from rheo_contracts import (
    AuditSpec,
    Idempotency,
    OperationDeclaration,
    Role,
    SafetyClass,
    WorkspaceContext,
)
from rheo_core.modules.readiness import ModuleNotReady
from rheo_core.operations.refusals import OperationRefused
from rheo_core.operations.registry import Handler
from rheo_core.storage.backend import StorageRefusal
from rheo_core.storage.backend import UnitOfWork as CoreUnitOfWork

from rheo_finance import catalogue, db
from rheo_finance.context import UnitOfWork
from rheo_finance.contracts import Role as FinanceRole
from rheo_finance.operations import OperationSpec
from rheo_finance.wiring.core import handler_context, layout_for
from rheo_finance.wiring.errors import matching_code
from rheo_finance.wiring.reads import owner_session, owner_view

WRITE_NAMES: Final = (
    "finance.obligation.report_paid",
    "finance.obligation.unreport_paid",
)
OWNER_SESSION_ONLY: Final = "finance_owner_session_only"
WRITE_FAILED: Final = "finance_write_failed"
OUTPUT_INVALID: Final = "finance_output_invalid"
# A click waits this long for a running derive, then says the ledger is busy.
WAIT_SECONDS: Final = 10.0

PASS_THROUGH: Final[tuple[type[BaseException], ...]] = (
    ModuleNotReady,
    StorageRefusal,
)


def write_handler(spec: OperationSpec) -> Handler:
    """Core's handler for ``spec``, one of ``WRITE_NAMES``."""
    view = owner_view(spec.output_model)

    def handle(
        ctx: WorkspaceContext, uow: CoreUnitOfWork, data: BaseModel
    ) -> BaseModel:
        if not owner_session(ctx):
            raise OperationRefused(OWNER_SESSION_ONLY, OWNER_SESSION_ONLY)
        code = WRITE_FAILED
        token: db.LayoutToken | None = None
        try:
            token = db.use_layout(db.check_layout(layout_for(ctx)))
            finance_ctx = handler_context(FinanceRole.OWNER)
            connection = uow.connection
            with db.acting_as(connection, db.OWNER_ROLE):
                with db.writer_lock(connection, wait_seconds=WAIT_SECONDS):
                    out = spec.handler(finance_ctx, UnitOfWork(connection), data)
            if isinstance(out, spec.output_model):
                return view.model_validate(out)
            code = OUTPUT_INVALID
        except PASS_THROUGH:
            raise
        except Exception as exc:
            code = matching_code(exc) or WRITE_FAILED
        finally:
            if token is not None:
                db.release_layout(token)
        raise OperationRefused(code, code)

    return handle


def _spec(name: str) -> OperationSpec:
    spec = catalogue.load_all()[name]
    # Only an owner-only MUTATE spec on the owner connection is wired here.
    assert spec.owner_only and spec.connection == "owner", name
    return spec


WRITE_SPECS: Final[tuple[OperationSpec, ...]] = tuple(
    _spec(name) for name in WRITE_NAMES
)

OPERATIONS: Final[tuple[tuple[OperationDeclaration, Handler], ...]] = tuple(
    (
        OperationDeclaration(
            name=spec.name,
            safety_class=SafetyClass.MUTATE,
            roles=frozenset({Role.OWNER}),
            input_model=spec.input_model,
            output=owner_view(spec.output_model),
            idempotency=Idempotency.NONE,
            # Finance declares no record type core could resolve a subject against.
            audit=AuditSpec(subject_field=None),
        ),
        write_handler(spec),
    )
    for spec in WRITE_SPECS
)
