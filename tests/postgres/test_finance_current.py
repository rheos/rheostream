"""Finance's reminders reach Current in process (RS-6c, ADR-0025).

Both modules installed and enabled through core's real operations, Current's real
handlers through ``call_in_transaction``, the worker's real visit for D4 and D7. The
reminders come from a patched ``desired_tickets`` and payee registry: synthetic labels,
round synthetic amounts, keys ``finance:alpha:<period>`` and ``finance:beta:<period>``.
The approval goes through the real ``finance.sink.approve`` under the binding, so it
pins the in-process digest. A spy around ``call_in_transaction`` records each call and
whether the connection is at the session role before and after it.

- D1: one sync creates exactly two items with the pair and provenance ``agent``; a
  second sync makes no call and changes nothing.
- D2: an item that already holds a key's pair, with no sink row, is folded into.
- D3: a real refusal (a retired area) after an update rolls back the whole sync.
- D3b: a refusal after a real create rolls that create back too.
- D4: a planted refusal detail is kept nowhere; through a worker visit the sync's row
  is ``ok=false, failed`` and the tick is recorded; ``ModuleNotReady`` under a call is
  ``current_unavailable`` and the job still succeeds.
- D5: a sink row pointing at an item with another source pair is never changed; the
  control with the pair is.
- D6: every call starts and ends at the session role, and so does the next session.
- D7: the tick in one worker visit syncs, and a second visit creates nothing new.
- D8: no ``finance.`` name in the loaded registries or the token sets.
"""

from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from conftest import ClusterSession
from harness.modules import (
    LoadedSurfaces,
    _advancing_clock,
    install_and_enable_module,
    loaded_probe_modules,
)
from pydantic import BaseModel
from rheo_contracts import Role as CoreRole
from rheo_contracts import WorkspaceContext
from rheo_core.boundary import context_for_harness, context_for_operator
from rheo_core.boundary.factories import context_for_scheduled_job
from rheo_core.events import ConsumerRegistry
from rheo_core.modules.loader import loaded_manifests
from rheo_core.modules.readiness import ModuleNotReady
from rheo_core.operations import dispatch, register_core_operations, transaction
from rheo_core.operations.refusals import OperationRefused
from rheo_core.settings import resolver as settings_resolver
from rheo_core.settings.schema import REGISTRY as SETTINGS
from rheo_core.settings.schema import SettingsRegistry, ValueType, encode_text
from rheo_core.storage import work_tables
from rheo_core.storage.data_root import Purpose, workspace_dir_for
from rheo_core.storage.repositories import upsert_workspace_setting
from rheo_core.storage.routing import open_unit_of_work
from rheo_core.storage.work_index import DueWorkspace
from rheo_core.tokens import sets as token_sets
from rheo_core.work.jobs import enqueue_job
from rheo_core.work.kinds import JobKindRegistry
from rheo_core.work.loop import visit_workspace
from rheo_current.storage import tables as current_tables
from rheo_finance import catalogue, db
from rheo_finance.contracts import Role
from rheo_finance.jobs import registry as job_registry
from rheo_finance.operations import dispatch as finance_dispatch
from rheo_finance.reminders.desired import DesiredOut, DesiredTicket
from rheo_finance.reminders.store import PostgresReminderRepository
from rheo_finance.sinks import current
from rheo_finance.sinks.ops import Preview
from rheo_finance.sinks.spec import spec_digest
from rheo_finance.storage.schema import job_run, sink_ticket
from rheo_finance.wiring import current as wiring
from rheo_finance.wiring.core import context_over_core
from rheo_finance.wiring.manifest import JOBS_ENABLED_KEY, MANIFEST
from rheo_finance.wiring.tick_job import TICK_JOB_KIND, TickPayload, finance_tick_job
from sqlalchemy import Connection, Engine, func, select, text

pytestmark = pytest.mark.postgres

PERIOD = "bill-2063-02"
ALPHA = f"finance:alpha:{PERIOD}"
BETA = f"finance:beta:{PERIOD}"
PAYEES = frozenset({"Sample Utility", "Sample Card"})
PLANTED = "synthetic planted detail"
SETTINGS_TEXT = (
    '[finance.reminders]\nmin_due_date = "2063-01-01"\n'
    "[finance.sinks.current]\nenabled = true\n"
)


def _ticket(key: str, *, due: date = date(2063, 2, 15)) -> DesiredTicket:
    label = "Sample Utility" if ":alpha:" in key else "Sample Card"
    return DesiredTicket(
        payee_label=label,
        amount=Decimal("50.00"),
        due_date=due,
        period=PERIOD,
        title=f"{label} due",
        body="synthetic",
        source_ref=key,
        status="open",
        wake=None,
        severity="normal",
    )


@dataclass
class Spy:
    """Each call's name and whether the connection is at the session role before and
    after it; ``fail`` may raise in place of (or after) the real call."""

    calls: list[tuple[str, bool, bool | None]] = field(default_factory=list)
    fail: Callable[[str, int], BaseException | None] | None = None
    after_real: bool = False


@dataclass
class Rig:
    cluster: ClusterSession
    workspace: UUID
    operator: WorkspaceContext
    owner: WorkspaceContext
    surfaces: LoadedSurfaces
    consumers: ConsumerRegistry
    desired: list[DesiredTicket]
    spy: Spy

    def engine(self) -> Engine:
        row = self.cluster.registry_row(self.workspace)
        return self.cluster.backend.pools.engine_for(row.database_name)

    def core(self, name: str, payload: Mapping[str, object]) -> Any:
        outcome = dispatch(
            self.owner,
            name,
            payload,
            registry=self.surfaces.operations,
            consumers=self.consumers,
        )
        assert outcome.state == "succeeded", outcome
        return outcome.result

    def approve(self) -> None:
        catalogue.load_all()
        with wiring.bind_reminders(self.operator, None):
            ctx = context_over_core(self.operator, Role.OWNER, model_client=None)
            preview = finance_dispatch(
                "finance.sink.preview", {"sink": "current"}, Role.OWNER, ctx=ctx
            )
            assert isinstance(preview, Preview)
            digest = preview.digest
            finance_dispatch(
                "finance.sink.approve",
                {
                    "sink": "current",
                    "digest_prefix": digest[:8],
                    "spec_digest": spec_digest(current.IN_PROCESS_SPEC),
                },
                Role.OWNER,
                ctx=ctx,
            )

    def sync(self) -> current.SyncResult:
        core_ctx = context_for_scheduled_job(self.workspace)
        assert isinstance(core_ctx, WorkspaceContext)
        with wiring.bind_reminders(core_ctx, self.consumers):
            ctx = context_over_core(core_ctx, Role.SERVICE, model_client=None)
            return current.sync_reminders(ctx)


def _session_user(connection: Connection) -> bool:
    return bool(
        connection.execute(text("SELECT current_user = session_user")).scalar_one()
    )


def _install_spy(monkeypatch: pytest.MonkeyPatch, spy: Spy) -> None:
    real = wiring.call_in_transaction

    def spying(
        ctx: WorkspaceContext, uow: Any, name: str, payload: Mapping[str, object]
    ) -> BaseModel:
        before = _session_user(uow.connection)
        index = len(spy.calls)
        spy.calls.append((name, before, None))
        planned = spy.fail(name, index) if spy.fail is not None else None
        if planned is not None and not spy.after_real:
            raise planned
        output = real(ctx, uow, name, payload)
        if planned is not None:
            raise planned
        spy.calls[index] = (name, before, _session_user(uow.connection))
        return output

    monkeypatch.setattr(wiring, "call_in_transaction", spying)


def _layout(workspace: UUID) -> db.FinanceLayout:
    layout = db.layout_under(workspace_dir_for(workspace, Purpose.UPLOADS))
    for directory in layout.directories:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    (layout.data / "settings.toml").write_text(SETTINGS_TEXT, encoding="utf-8")
    return layout


@pytest.fixture
def rig(
    cluster: ClusterSession,
    workspace: UUID,
    owner_account_id: UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Rig]:
    with loaded_probe_modules(monkeypatch, "current", "finance") as surfaces:
        settings = SettingsRegistry()
        for spec in SETTINGS.specs():
            settings.register(spec, origin=SETTINGS.origin_of(spec.key))
        manifests = loaded_manifests()
        for manifest in manifests.values():
            for spec in manifest.configuration_schema:
                if spec.key not in {each.key for each in settings.specs()}:
                    settings.register(spec, origin=manifest.module_id)
        monkeypatch.setattr(settings_resolver, "REGISTRY", settings)
        monkeypatch.setattr(transaction, "REGISTRY", surfaces.operations)
        register_core_operations()
        register_core_operations(registry=surfaces.operations)
        operator = context_for_operator(workspace)
        assert isinstance(operator, WorkspaceContext)
        for module_id in ("current", "finance"):
            install_and_enable_module(cluster.backend, operator, workspace, module_id)
        owner = context_for_harness(workspace, owner_account_id, CoreRole.OWNER)
        assert isinstance(owner, WorkspaceContext)
        consumers = ConsumerRegistry()
        for manifest in manifests.values():
            for subscription in manifest.subscriptions:
                consumers.register(subscription)
        desired = [_ticket(ALPHA), _ticket(BETA)]
        monkeypatch.setattr(
            current,
            "desired_tickets",
            lambda _ctx, _facts: DesiredOut(tickets=list(desired)),
        )
        monkeypatch.setattr(
            PostgresReminderRepository, "registered_payees", lambda _self: PAYEES
        )
        spy = Spy()
        _install_spy(monkeypatch, spy)
        token = db.use_layout(_layout(workspace))
        try:
            yield Rig(
                cluster,
                workspace,
                operator,
                owner,
                surfaces,
                consumers,
                desired,
                spy,
            )
        finally:
            db.release_layout(token)


# --- reading both sides ---------------------------------------------------------------


def _items(rig: Rig) -> list[dict[str, Any]]:
    item = current_tables.item
    with rig.engine().connect() as connection:
        rows = connection.execute(
            select(
                item.c.id,
                item.c.source_kind,
                item.c.source_ref,
                item.c.provenance,
                item.c.status,
                item.c.revision,
                item.c.due_on,
                item.c.updated_at,
            ).order_by(item.c.source_ref)
        ).mappings()
        return [dict(row) for row in rows]


def _audit(rig: Rig, operation: str | None = None) -> int:
    audit = work_tables.audit_record
    clause = (
        audit.c.operation_name.like("current.item.%")
        if operation is None
        else audit.c.operation_name == operation
    )
    with rig.engine().connect() as connection:
        return int(connection.execute(select(func.count()).where(clause)).scalar_one())


def _outbox(rig: Rig) -> int:
    outbox = work_tables.outbox_event
    with rig.engine().connect() as connection:
        return int(
            connection.execute(
                select(func.count()).where(outbox.c.type.like("current.%"))
            ).scalar_one()
        )


def _rows(rig: Rig) -> list[dict[str, Any]]:
    with rig.engine().connect() as connection:
        rows = connection.execute(
            select(sink_ticket).order_by(sink_ticket.c.source_ref)
        ).mappings()
        return [dict(row) for row in rows]


def _snapshot(rig: Rig) -> tuple[object, ...]:
    return (_items(rig), _audit(rig), _outbox(rig), _rows(rig))


def _creates(rig: Rig) -> int:
    return sum(1 for name, _, _ in rig.spy.calls if name == "current.item.create")


# --- D1, D6: dedup and the session role -----------------------------------------------


def test_one_sync_creates_each_item_once_and_a_rerun_changes_nothing(
    rig: Rig,
) -> None:
    rig.approve()
    assert rig.sync() == current.SyncResult("synced")
    items = _items(rig)
    assert [(i["source_kind"], i["source_ref"]) for i in items] == [
        (current.SOURCE_KIND, ALPHA),
        (current.SOURCE_KIND, BETA),
    ]
    assert {i["provenance"] for i in items} == {"agent"}
    rows = _rows(rig)
    assert [r["state"] for r in rows] == ["open", "open"]
    assert all(r["external_id"] for r in rows)
    assert _audit(rig, "current.item.create") == 2
    before = _snapshot(rig)
    calls = len(rig.spy.calls)
    assert rig.sync() == current.SyncResult("synced")
    assert len(rig.spy.calls) == calls
    # Items, audit, outbox and the sink rows alike: the rerun writes nothing.
    assert _snapshot(rig) == before
    assert _audit(rig, "current.item.create") == 2
    # D6: every call ran at the session role, before and after, and so does the
    # next session.
    assert rig.spy.calls
    assert all(b is True and a is True for _, b, a in rig.spy.calls)
    with open_unit_of_work(rig.operator) as uow:
        assert _session_user(uow.connection)


# --- D2: Current folds a key whose sink row was lost ----------------------------------


def test_an_item_already_holding_the_pair_is_folded_into(rig: Rig) -> None:
    held = rig.core(
        "current.item.create",
        {
            "title": "Sample Utility due",
            "area": "bills",
            "source_kind": current.SOURCE_KIND,
            "source_ref": ALPHA,
            "fingerprint": ALPHA,
        },
    )
    held_ref = held.item.ref
    rig.desired[:] = [_ticket(ALPHA)]
    rig.approve()
    assert rig.sync() == current.SyncResult("synced")
    items = _items(rig)
    assert len(items) == 1 and items[0]["source_ref"] == ALPHA
    assert [r["external_id"] for r in _rows(rig)] == [held_ref]


# --- D3, D3b: a refusal rolls the whole sync back -------------------------------------


def test_a_real_refusal_after_an_update_rolls_the_whole_sync_back(rig: Rig) -> None:
    rig.desired[:] = [_ticket(ALPHA)]
    rig.approve()
    assert rig.sync() == current.SyncResult("synced")
    rig.core("current.area.update", {"key": "bills", "retired": True})
    rig.desired[:] = [_ticket(ALPHA, due=date(2063, 2, 20)), _ticket(BETA)]
    # The approval pins the spec, which has not changed, so the gate stays open.
    before = _snapshot(rig)
    assert rig.sync() == current.SyncResult("failed", "refused")
    names = [name for name, _, _ in rig.spy.calls]
    # The update ran, and was rolled back with the refused create.
    assert "current.item.update" in names and names[-1] == "current.item.create"
    # The spy writes a call's after-role only when the real call returned, so the
    # update really ran before the refusal rolled it back.
    update = next(call for call in rig.spy.calls if call[0] == "current.item.update")
    assert update[2] is not None
    assert _snapshot(rig) == before
    assert _items(rig)[0]["due_on"] == date(2063, 2, 15)


def test_a_refusal_after_a_real_create_rolls_that_create_back(rig: Rig) -> None:
    rig.approve()
    creates: list[int] = []

    def fail(name: str, _index: int) -> BaseException | None:
        if name != "current.item.create":
            return None
        creates.append(1)
        return OperationRefused("synthetic_state") if len(creates) == 2 else None

    rig.spy.fail = fail
    rig.spy.after_real = True
    assert rig.sync() == current.SyncResult("failed", "refused")
    assert len(creates) == 2
    assert _items(rig) == [] and _rows(rig) == []
    assert _audit(rig) == 0 and _outbox(rig) == 0


# --- D4, D7: through the worker ------------------------------------------------------


def _visit(rig: Rig) -> dict[str, object]:
    with rig.engine().begin() as connection:
        spec = next(
            each
            for each in MANIFEST.configuration_schema
            if each.key == JOBS_ENABLED_KEY
        )
        upsert_workspace_setting(
            connection,
            key=JOBS_ENABLED_KEY,
            value=encode_text(spec, True),
            value_type=ValueType.BOOL,
            updated_by=None,
        )
        job_id = enqueue_job(
            connection,
            kind=TICK_JOB_KIND,
            payload={"workspace_id": str(rig.workspace)},
            now=datetime.now(UTC),
            max_attempts=3,
        )
    kinds = JobKindRegistry()
    kinds.register(TICK_JOB_KIND, TickPayload, finance_tick_job)
    visit_workspace(
        DueWorkspace(workspace_id=rig.workspace, observed_due_at=None),
        kinds=kinds,
        consumers=rig.consumers,
        backend=rig.cluster.backend,
        owner="finance-current-test",
        clock=_advancing_clock(),
        jitter=None,
    )
    with rig.engine().connect() as connection:
        job = connection.execute(
            select(work_tables.job.c.state, work_tables.job.c.last_error).where(
                work_tables.job.c.id == job_id
            )
        ).one()
    return {"state": job.state, "last_error": job.last_error}


def _job_runs(rig: Rig, job: str) -> list[tuple[bool | None, str]]:
    with rig.engine().connect() as connection:
        rows = connection.execute(
            select(job_run.c.ok, job_run.c.detail_code)
            .where(job_run.c.job == job)
            .order_by(job_run.c.seq)
        ).all()
    return [(row.ok, row.detail_code) for row in rows]


@pytest.fixture
def reminders_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        job_registry,
        "JOBS",
        {"finance.reminders_sync": job_registry.JOBS["finance.reminders_sync"]},
    )


def _everything_as_text(rig: Rig) -> str:
    with rig.engine().connect() as connection:
        parts = [
            str(list(connection.execute(select(table)).all()))
            for table in (
                sink_ticket,
                job_run,
                current_tables.item,
                current_tables.comment,
                current_tables.signal,
            )
        ]
        parts.append(
            str(list(connection.execute(select(work_tables.job.c.last_error)).all()))
        )
    return "\n".join(parts)


def test_a_refusal_through_the_worker_keeps_only_fixed_codes(
    rig: Rig, reminders_only: None
) -> None:
    rig.approve()
    rig.spy.fail = lambda name, _i: (
        OperationRefused("area_retired", PLANTED)
        if name == "current.item.create"
        else None
    )
    assert _visit(rig) == {"state": "succeeded", "last_error": None}
    assert _job_runs(rig, "finance.reminders_sync") == [(False, "failed")]
    assert _job_runs(rig, "finance.tick")
    assert PLANTED not in _everything_as_text(rig)
    assert _items(rig) == [] and _rows(rig) == []


def test_module_not_ready_under_a_call_is_unavailable_and_the_job_succeeds(
    rig: Rig, reminders_only: None
) -> None:
    rig.approve()
    rig.spy.fail = lambda _name, _i: ModuleNotReady("module_unavailable", PLANTED)
    assert rig.sync() == current.SyncResult("failed", "current_unavailable")
    assert _visit(rig) == {"state": "succeeded", "last_error": None}
    assert _job_runs(rig, "finance.reminders_sync") == [(False, "failed")]
    assert PLANTED not in _everything_as_text(rig)


def test_the_tick_syncs_through_one_worker_visit(
    rig: Rig, reminders_only: None
) -> None:
    rig.approve()
    assert _visit(rig) == {"state": "succeeded", "last_error": None}
    assert len(_items(rig)) == 2
    assert _job_runs(rig, "finance.reminders_sync") == [(True, "completed")]
    creates = _audit(rig, "current.item.create")
    assert creates == 2
    # A second sync finds nothing to do.
    assert rig.sync() == current.SyncResult("synced")
    assert len(_items(rig)) == 2 and _audit(rig, "current.item.create") == creates


# --- D5: scope ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("email", current.SyncResult("failed", "not_owned")),
        (current.SOURCE_KIND, current.SyncResult("synced")),
    ],
    ids=["foreign", "control"],
)
def test_a_row_pointing_at_a_foreign_item_never_changes_it(
    rig: Rig, kind: str, expected: current.SyncResult
) -> None:
    rig.desired[:] = [_ticket(ALPHA)]
    rig.approve()
    held = rig.core(
        "current.item.create",
        {
            "title": "Sample Utility due",
            "area": "bills",
            "source_kind": kind,
            "source_ref": ALPHA,
        },
    )
    ref = held.item.ref
    with rig.engine().begin() as connection, db.acting_as(connection, db.OWNER_ROLE):
        connection.execute(
            sink_ticket.insert().values(
                source_ref=ALPHA,
                external_id=ref,
                state="open",
                create_title="Sample Utility due",
                create_body="synthetic",
                create_due_on=date(2063, 2, 1),
                create_priority="normal",
            )
        )
    revision = _items(rig)[0]["revision"]
    assert rig.sync() == expected
    after = _items(rig)[0]["revision"]
    if expected.state == "synced":
        assert after > revision and _audit(rig, "current.item.update") == 1
    else:
        assert after == revision and _audit(rig, "current.item.update") == 0
        assert [name for name, _, _ in rig.spy.calls] == ["current.item.get"]


# --- D8: package sets -----------------------------------------------------------------


def test_no_finance_name_reaches_the_registries_or_the_token_sets(rig: Rig) -> None:
    named = rig.surfaces.operations.names() | rig.surfaces.tools.names()
    sets = token_sets.read_only() | token_sets.agent_default() | token_sets.cli_full()
    assert not {name for name in named | sets if name.startswith("finance")}
    assert "current.item.create" in rig.surfaces.operations.names()
