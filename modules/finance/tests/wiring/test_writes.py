"""The two owner writes as core sees them (#639, ADR-0030). No database.

- W1: exactly ``report_paid`` and ``unreport_paid``, each MUTATE for the owner, owner-
  only in finance, on the owner connection, audited with no subject, with no tool, and
  accepted by core's registry.
- W2: the adapter over a recording connection and a synthetic layout: every caller but
  the owner's own browser session is refused before anything runs; for that session
  the role is ``finance_owner``, set and reset around the handler, under the ledger
  writer lock with the adapter's wait, the layout bound and released; a failure leaves
  as its fixed code (else ``finance_write_failed``), nothing chained; core's own
  refusals pass unchanged.

Values are synthetic.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Annotated, Any

import pytest
from pydantic import BaseModel, ConfigDict
from rheo_contracts import (
    Actor,
    ActorKind,
    Audience,
    AudienceKind,
    AuditSpec,
    Idempotency,
    Role,
    SafetyClass,
)
from rheo_core.modules.readiness import ModuleNotReady
from rheo_core.operations.refusals import OperationRefused
from rheo_core.operations.registry import OperationRegistry
from sqlalchemy import text

from rheo_finance import catalogue, db
from rheo_finance.contracts import Role as FinanceRole
from rheo_finance.contracts import SafetyClass as FinanceSafetyClass
from rheo_finance.contracts import Tiered
from rheo_finance.decision import DecisionRefused
from rheo_finance.operations import OperationSpec
from rheo_finance.reported import ReportIn
from rheo_finance.tier import Tier
from rheo_finance.wiring import writes
from rheo_finance.wiring.manifest import MANIFEST

SENTINEL = "synthetic refusal detail"


def _ctx(actor: ActorKind, audience: AudienceKind | None, role: Role) -> Any:
    return SimpleNamespace(
        actor=Actor(kind=actor, id=uuid.UUID(int=1)),
        audience=None
        if audience is None
        else Audience(kind=audience, id=uuid.UUID(int=1)),
        role=role,
    )


OWNER_SESSION = (ActorKind.ACCOUNT, AudienceKind.SESSION, Role.OWNER)


# --- W1: what is declared ------------------------------------------------------------


def test_w1_exactly_the_two_owner_writes() -> None:
    specs = catalogue.load_all()
    declared = [(d, h) for d, h in MANIFEST.operations if d.name in writes.WRITE_NAMES]
    assert [d.name for d, _ in declared] == list(writes.WRITE_NAMES)
    registry = OperationRegistry()
    for declaration, handler in declared:
        spec = specs[declaration.name]
        assert spec.safety_class is FinanceSafetyClass.MUTATE
        assert (spec.owner_only, spec.connection, spec.tool_name) == (
            True,
            "owner",
            None,
        )
        assert spec.input_model is ReportIn
        assert declaration.safety_class is SafetyClass.MUTATE
        assert declaration.roles == frozenset({Role.OWNER})
        assert declaration.idempotency is Idempotency.NONE
        assert declaration.audit == AuditSpec(subject_field=None)
        assert declaration.input_model is ReportIn
        registry.register(declaration, handler, origin="finance", explicit_only=True)
    # Every other finance MUTATE stays with the command.
    mutating = {
        name
        for name, spec in specs.items()
        if spec.safety_class is not FinanceSafetyClass.READ
    }
    core_names = {d.name for d, _ in MANIFEST.operations}
    assert mutating & core_names == set(writes.WRITE_NAMES)


# --- W2: the adapter -----------------------------------------------------------------


class RecordingConnection:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, statement: object, parameters: object = None) -> None:
        self.statements.append(str(statement))


class _Out(BaseModel):
    model_config = ConfigDict(frozen=True)
    obligation_ref: Annotated[str, Tiered(Tier.PUBLIC)]


SET = f"SET LOCAL ROLE {db.OWNER_ROLE}"
RESET = "RESET ROLE"
MARK = "SELECT 'synthetic handler mark'"
LOCK = "LOCK"
UNLOCK = "UNLOCK"


class _Rig:
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.layout = db.layout_under(tmp_path / "uploads")
        for directory in self.layout.directories:
            directory.mkdir(parents=True, exist_ok=True)
        self.connection = RecordingConnection()
        self.waits: list[float] = []
        self.raises: BaseException | None = None
        self.roles: list[FinanceRole] = []
        monkeypatch.setattr(writes, "layout_for", lambda _ctx: self.layout)

        @contextmanager
        def lock(connection: Any, *, wait_seconds: float) -> Iterator[None]:
            self.waits.append(wait_seconds)
            connection.execute(LOCK)
            yield
            connection.execute(UNLOCK)

        monkeypatch.setattr(db, "writer_lock", lock)

    def handler(self, ctx: Any, uow: Any, data: ReportIn) -> BaseModel:
        self.roles.append(ctx.role)
        assert db.bound_layout() == self.layout
        uow.execute(text(MARK))
        if self.raises is not None:
            raise self.raises
        return _Out(obligation_ref=data.obligation_ref)

    def run(self, ctx: Any) -> BaseModel:
        spec = OperationSpec(
            name="finance.planted.write",
            input_model=ReportIn,
            output_model=_Out,
            handler=self.handler,
            safety_class=FinanceSafetyClass.MUTATE,
            roles=frozenset({FinanceRole.OWNER}),
            connection="owner",
            owner_only=True,
            description="A synthetic write for the adapter test.",
        )
        uow: Any = SimpleNamespace(connection=self.connection)
        return writes.write_handler(spec)(ctx, uow, ReportIn(obligation_ref="x"))


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[_Rig]:
    assert db.bound_layout() is None
    yield _Rig(tmp_path, monkeypatch)
    assert db.bound_layout() is None


def test_w2_the_owners_session_writes_as_owner_under_the_lock(rig: _Rig) -> None:
    out = rig.run(_ctx(*OWNER_SESSION))
    assert rig.connection.statements == [SET, LOCK, MARK, UNLOCK, RESET]
    assert rig.waits == [writes.WAIT_SECONDS]
    assert rig.roles == [FinanceRole.OWNER]
    assert out.model_dump() == {"obligation_ref": "x"}


@pytest.mark.parametrize(
    "caller",
    [
        (ActorKind.ACCOUNT, AudienceKind.SESSION, Role.MEMBER),
        (ActorKind.TOKEN, AudienceKind.SESSION, Role.OWNER),
        (ActorKind.TOKEN, None, Role.OWNER),
        (ActorKind.ACCOUNT, None, Role.OWNER),
        (ActorKind.OPERATOR, None, Role.OWNER),
    ],
    ids=["member", "token-session", "token", "account-no-session", "operator"],
)
def test_w2_every_other_caller_is_refused_before_anything_runs(
    rig: _Rig, caller: tuple[ActorKind, AudienceKind | None, Role]
) -> None:
    with pytest.raises(OperationRefused) as caught:
        rig.run(_ctx(*caller))
    refused = caught.value
    assert (refused.state, refused.detail) == (writes.OWNER_SESSION_ONLY,) * 2
    assert rig.connection.statements == []
    assert rig.roles == []


@pytest.mark.parametrize(
    ("exc", "code"),
    [
        (DecisionRefused(SENTINEL), "finance_operation_refused"),
        (db.WriterBusy(SENTINEL), "finance_writer_busy"),
        (ValueError(SENTINEL), "finance_write_failed"),
    ],
    ids=["refused", "busy", "unmapped"],
)
def test_w2_a_failure_leaves_as_its_code_alone(
    rig: _Rig, exc: BaseException, code: str
) -> None:
    rig.raises = exc
    with pytest.raises(OperationRefused) as caught:
        rig.run(_ctx(*OWNER_SESSION))
    refused = caught.value
    assert (refused.state, refused.detail) == (code, code)
    assert SENTINEL not in str(refused)
    assert refused.__cause__ is None
    assert refused.__context__ is None
    # No reset after a failure: core's rollback ends the transaction and the role.
    assert rig.connection.statements == [SET, LOCK, MARK]


def test_w2_cores_own_refusals_pass_unchanged(rig: _Rig) -> None:
    rig.raises = ModuleNotReady("finance")
    with pytest.raises(ModuleNotReady):
        rig.run(_ctx(*OWNER_SESSION))
