"""Finance's reminders reach Current in process (RS-6c, ADR-0025, ADR-0027).

Both modules installed and enabled through core's real operations, Current's real
handlers through ``call_in_transaction``, the worker's real visit where named. The
reminders come from a patched ``desired_tickets`` and payee registry: synthetic labels,
round synthetic amounts, keys ``finance:alpha:<period>``, ``finance:beta:<period>``
and ``finance:gamma:<period>``. The approval goes through the real
``finance.sink.approve`` under the binding, so it pins the in-process digest. A spy
around ``call_in_transaction`` records each call and whether the connection is at the
session role before and after it.

RS-6c, kept:

- one sync creates exactly two items with the pair and provenance ``agent``; a second
  sync makes no call and changes nothing; every call starts and ends at the session
  role, and so does the next session;
- an item that already holds a key's pair, with no sink row, is folded into;
- ``ModuleNotReady`` under a call is ``current_unavailable`` and the job still
  succeeds; the tick in one worker visit syncs, and a second visit creates nothing new;
- a sink row pointing at an item with another source pair is never changed (now set
  aside, ``partial``); the control with the pair is;
- no ``finance.`` name in the loaded registries or the token sets.

A refused key is set aside (#548):

- D4: a real refusal (a retired area) of a new key's create leaves alpha's update
  committed and acknowledged, beta with no item, audit or event and a ``post_pending``
  claim carrying ``refused``; a rerun calls only beta's create; once the area is
  restored beta is created once and its code cleared.
- D5: a refusal after alpha's real update rolls back alpha's key alone (item, audit,
  events and the row's acknowledging columns as they were) while beta is created.
- D6: a row pointing at a foreign item is set aside on every sync, and the other keys
  sync once and then make no call; the foreign item never changes.
- D7: through a worker visit, a planted refusal on beta's create alone: the job
  succeeds, the sync's row is ``ok=false, failed``, the tick is recorded ``ok=true``,
  alpha's item exists and the planted detail is kept nowhere.
- D8: a global code (``ModuleNotReady``) after alpha's real create rolls every key
  back: no item, row, audit row or event.
- D9: an item erased through Current's approved deletion is filed once more, under a
  new ref, at the next sync that reads it (a changed period); a third sync calls
  nothing. For a settled period it is forgotten and never created again.
- D10: after two worker visits over D6's setup, the owner's ``finance.job.health``
  is ``failed_due_job`` and names alpha with ``not_owned``, never synced; after D4's
  last sync it names no key.
- D11: in D4 and D5 every call starts at the session role, the first after a key
  rollback included, and so does the next session.
"""

from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Literal
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
from rheo_core.refs.resolver import RESOLVERS, register_resolver
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
from rheo_finance.jobs.service import HealthOut, ReminderError
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
GAMMA = f"finance:gamma:{PERIOD}"
PAYEES = frozenset({"Sample Utility", "Sample Card"})
PLANTED = "synthetic planted detail"
SETTINGS_TEXT = (
    '[finance.reminders]\nmin_due_date = "2063-01-01"\n'
    "[finance.sinks.current]\nenabled = true\n"
)
# A row's acknowledging columns: what a key that was set aside must leave as it was.
ACKNOWLEDGING = (
    "state",
    "external_id",
    "last_title",
    "last_due_on",
    "last_priority",
    "last_status",
    "last_success_at",
    "settled_at",
    "create_title",
    "create_body",
    "create_due_on",
    "create_priority",
)


def _ticket(
    key: str,
    *,
    due: date = date(2063, 2, 15),
    amount: Decimal = Decimal("50.00"),
    status: Literal["open", "done"] = "open",
) -> DesiredTicket:
    label = "Sample Utility" if ":alpha:" in key else "Sample Card"
    return DesiredTicket(
        payee_label=label,
        amount=amount,
        due_date=due,
        period=PERIOD,
        title=f"{label} due",
        body="synthetic",
        source_ref=key,
        status=status,
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


def _outbox(rig: Rig, kind: str | None = None) -> int:
    outbox = work_tables.outbox_event
    clause = outbox.c.type.like("current.%") if kind is None else outbox.c.type == kind
    with rig.engine().connect() as connection:
        return int(connection.execute(select(func.count()).where(clause)).scalar_one())


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


def _row(rig: Rig, key: str) -> dict[str, Any]:
    return next(row for row in _rows(rig) if row["source_ref"] == key)


def _acknowledging(row: Mapping[str, Any]) -> dict[str, Any]:
    return {name: row[name] for name in ACKNOWLEDGING}


def _health(rig: Rig) -> HealthOut:
    """``finance.job.health`` dispatched by the owner, as ``finance jobs health``."""
    ctx = context_over_core(rig.operator, Role.OWNER, model_client=None)
    out = finance_dispatch("finance.job.health", {}, Role.OWNER, ctx=ctx)
    assert isinstance(out, HealthOut)
    return out


def _assert_session_role(rig: Rig) -> None:
    """D11: every call began at the session role, the first after a key rollback
    included; every call that returned ended there; so does the next session."""
    assert rig.spy.calls
    assert all(before is True for _, before, _ in rig.spy.calls)
    assert all(after in {True, None} for _, _, after in rig.spy.calls)
    with open_unit_of_work(rig.operator) as uow:
        assert _session_user(uow.connection)


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


# --- D4, D5, D8, D11: a refused key is set aside (#548) -------------------------------


def test_a_key_refused_for_itself_is_set_aside_and_the_rest_commit(rig: Rig) -> None:
    rig.desired[:] = [_ticket(ALPHA)]
    rig.approve()
    assert rig.sync() == current.SyncResult("synced")
    rig.core("current.area.update", {"key": "bills", "retired": True})
    rig.desired[:] = [_ticket(ALPHA, due=date(2063, 2, 20)), _ticket(BETA)]
    # The approval pins the spec, which has not changed, so the gate stays open.
    assert rig.sync() == current.SyncResult("partial", "refused")
    # Alpha's update committed with its acknowledgement.
    items = _items(rig)
    assert [i["source_ref"] for i in items] == [ALPHA]
    assert items[0]["due_on"] == date(2063, 2, 20)
    alpha = _row(rig, ALPHA)
    assert alpha["last_due_on"] == date(2063, 2, 20)
    assert alpha["last_error_code"] is None
    # Beta's refused create left no item, audit or event, and a claim with its code.
    assert _audit(rig, "current.item.create") == 1
    assert _outbox(rig, "current.item.created") == 1
    beta = _row(rig, BETA)
    assert (beta["state"], beta["external_id"], beta["last_error_code"]) == (
        "post_pending",
        None,
        "refused",
    )
    _assert_session_role(rig)
    # A rerun calls nothing for alpha, and beta is refused again.
    calls = len(rig.spy.calls)
    assert rig.sync() == current.SyncResult("partial", "refused")
    assert [name for name, _, _ in rig.spy.calls[calls:]] == ["current.item.create"]
    assert len(_items(rig)) == 1
    # The area restored, beta is created once and its code cleared.
    rig.core("current.area.update", {"key": "bills", "retired": False})
    assert rig.sync() == current.SyncResult("synced")
    assert [(i["source_kind"], i["source_ref"]) for i in _items(rig)] == [
        (current.SOURCE_KIND, ALPHA),
        (current.SOURCE_KIND, BETA),
    ]
    assert _row(rig, BETA)["last_error_code"] is None
    assert _audit(rig, "current.item.create") == 2
    assert _health(rig).reminder_errors == []


def test_a_refusal_after_a_real_update_rolls_back_that_key_alone(rig: Rig) -> None:
    rig.desired[:] = [_ticket(ALPHA)]
    rig.approve()
    assert rig.sync() == current.SyncResult("synced")
    item_before = _items(rig)[0]
    row_before = _acknowledging(_row(rig, ALPHA))
    created = _outbox(rig, "current.item.created")
    assert created == 1
    rig.desired[:] = [
        _ticket(ALPHA, amount=Decimal("60.00"), status="done"),
        _ticket(BETA),
    ]
    rig.spy.fail = lambda name, _index: (
        OperationRefused("synthetic_state")
        if name == "current.item.set_status"
        else None
    )
    rig.spy.after_real = True
    calls = len(rig.spy.calls)
    assert rig.sync() == current.SyncResult("partial", "refused")
    names = [name for name, _, _ in rig.spy.calls[calls:]]
    # Alpha's update really ran (the spy kept its after-role) before its status
    # change was refused; then beta was created.
    update = next(c for c in rig.spy.calls[calls:] if c[0] == "current.item.update")
    assert update[2] is True
    assert "current.item.set_status" in names and names[-1] == "current.item.create"
    # Alpha's item, its audit and events, and its row are as they were.
    assert next(i for i in _items(rig) if i["source_ref"] == ALPHA) == item_before
    assert _audit(rig, "current.item.update") == 0
    assert _audit(rig, "current.item.set_status") == 0
    assert _outbox(rig, "current.item.completed") == 0
    alpha = _row(rig, ALPHA)
    assert _acknowledging(alpha) == row_before
    assert alpha["last_error_code"] == "refused"
    # Beta created and acknowledged beside it.
    assert _outbox(rig, "current.item.created") == created + 1
    beta = _row(rig, BETA)
    assert (beta["state"], beta["last_error_code"]) == ("open", None)
    assert beta["external_id"] is not None
    _assert_session_role(rig)


def test_a_global_code_after_a_real_create_rolls_back_every_key(rig: Rig) -> None:
    rig.approve()
    rig.spy.fail = lambda name, index: (
        ModuleNotReady("module_unavailable", PLANTED)
        if name == "current.item.create" and index == 1
        else None
    )
    assert rig.sync() == current.SyncResult("failed", "current_unavailable")
    # Alpha's create really returned before beta's call stopped the sync.
    assert [(name, after) for name, _, after in rig.spy.calls] == [
        ("current.item.create", True),
        ("current.item.create", None),
    ]
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
    # Beta's create only: alpha's goes through.
    rig.spy.fail = lambda name, index: (
        OperationRefused("area_retired", PLANTED)
        if name == "current.item.create" and index == 1
        else None
    )
    assert _visit(rig) == {"state": "succeeded", "last_error": None}
    assert _job_runs(rig, "finance.reminders_sync") == [(False, "failed")]
    assert [ok for ok, _ in _job_runs(rig, "finance.tick")] == [True]
    assert PLANTED not in _everything_as_text(rig)
    assert [i["source_ref"] for i in _items(rig)] == [ALPHA]
    assert _row(rig, ALPHA)["last_error_code"] is None
    assert _row(rig, BETA)["last_error_code"] == "refused"


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


def _alpha_row_on_an_item(rig: Rig, kind: str) -> str:
    """A sink row for alpha pointing at a new item of source kind ``kind``."""
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
    assert isinstance(ref, str)
    return ref


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("email", current.SyncResult("partial", "not_owned")),
        (current.SOURCE_KIND, current.SyncResult("synced")),
    ],
    ids=["foreign", "control"],
)
def test_a_row_pointing_at_a_foreign_item_never_changes_it(
    rig: Rig, kind: str, expected: current.SyncResult
) -> None:
    rig.desired[:] = [_ticket(ALPHA)]
    rig.approve()
    _alpha_row_on_an_item(rig, kind)
    revision = _items(rig)[0]["revision"]
    assert rig.sync() == expected
    after = _items(rig)[0]["revision"]
    if expected.state == "synced":
        assert after > revision and _audit(rig, "current.item.update") == 1
    else:
        assert after == revision and _audit(rig, "current.item.update") == 0
        assert [name for name, _, _ in rig.spy.calls] == ["current.item.get"]


def test_a_key_refused_on_every_sync_never_blocks_the_others(rig: Rig) -> None:
    rig.desired[:] = [_ticket(ALPHA), _ticket(BETA), _ticket(GAMMA)]
    rig.approve()
    foreign = _alpha_row_on_an_item(rig, "email")
    revision = next(i for i in _items(rig) if i["source_kind"] == "email")["revision"]
    assert rig.sync() == current.SyncResult("partial", "not_owned")
    ours = [i["source_ref"] for i in _items(rig) if i["source_kind"] != "email"]
    assert ours == [BETA, GAMMA]
    calls = len(rig.spy.calls)
    started = datetime.now(UTC)
    assert rig.sync() == current.SyncResult("partial", "not_owned")
    ended = datetime.now(UTC)
    # Only alpha's read: beta and gamma are synced and make no call.
    assert [name for name, _, _ in rig.spy.calls[calls:]] == ["current.item.get"]
    ours = [i["source_ref"] for i in _items(rig) if i["source_kind"] != "email"]
    assert ours == [BETA, GAMMA]
    held = next(i for i in _items(rig) if i["source_kind"] == "email")
    assert held["revision"] == revision
    assert _audit(rig, "current.item.update") == 0
    alpha = _row(rig, ALPHA)
    assert alpha["external_id"] == foreign
    assert alpha["last_error_code"] == "not_owned"
    assert started <= alpha["last_attempt_at"] <= ended


def test_health_names_the_key_set_aside_through_the_worker(
    rig: Rig, reminders_only: None
) -> None:
    rig.desired[:] = [_ticket(ALPHA), _ticket(BETA), _ticket(GAMMA)]
    rig.approve()
    _alpha_row_on_an_item(rig, "email")
    for _ in range(2):
        assert _visit(rig) == {"state": "succeeded", "last_error": None}
    out = _health(rig)
    assert out.code == "failed_due_job"
    assert out.reminder_errors == [
        ReminderError(key=ALPHA, code="not_owned", last_success_at=None)
    ]
    assert out.more == 0


# --- D9: an item erased in Current (#548) ---------------------------------------------


def _erase(rig: Rig, ref: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """The owner's approved deletion of the item ``ref`` through core.

    The approval's ``RecordStateGuard`` resolves the item on the process-wide resolver
    table, which ``loaded_probe_modules`` leaves alone. Without Current's resolvers
    there the hold records no revision and the guard refuses, so the approval ends
    ``refused`` and the item stays. A copy of the table holds them for this test only,
    registered before the hold, as every other rig that drives a deletion does.
    """
    monkeypatch.setattr(RESOLVERS, "_resolvers", dict(RESOLVERS._resolvers))
    manifest = loaded_manifests()["current"]
    for name, resolver in manifest.resolvers:
        register_resolver(manifest.module_id, name, resolver, origin=manifest.module_id)
    held = dispatch(
        rig.owner,
        "core.record.delete",
        {"ref": ref},
        registry=rig.surfaces.operations,
        consumers=rig.consumers,
    )
    assert held.state == "approval_required", held
    approved = rig.core("core.approval.approve", {"approval_id": str(held.approval_id)})
    # A guard refusal still answers ``succeeded``; the approval's own state says.
    assert approved.state == "executed", approved


@pytest.mark.parametrize("status", ["open", "done"])
def test_an_item_erased_in_current_is_filed_again_or_forgotten(
    rig: Rig, status: Literal["open", "done"], monkeypatch: pytest.MonkeyPatch
) -> None:
    rig.desired[:] = [_ticket(ALPHA)]
    rig.approve()
    assert rig.sync() == current.SyncResult("synced")
    old = _row(rig, ALPHA)["external_id"]
    assert old is not None
    _erase(rig, old, monkeypatch)
    assert _items(rig) == []
    creates = _audit(rig, "current.item.create")
    # A changed period makes the sync read the stored item; an acknowledged,
    # unchanged key makes no call.
    rig.desired[:] = [
        _ticket(ALPHA, status="done")
        if status == "done"
        else _ticket(ALPHA, due=date(2063, 2, 20))
    ]
    assert rig.sync() == current.SyncResult("synced")
    row = _row(rig, ALPHA)
    if status == "done":
        # A settled period is never created again.
        assert _items(rig) == []
        assert (row["state"], row["external_id"], row["last_status"]) == (
            "done",
            None,
            "done",
        )
        assert _audit(rig, "current.item.create") == creates
        return
    items = _items(rig)
    assert [(i["source_kind"], i["source_ref"]) for i in items] == [
        (current.SOURCE_KIND, ALPHA)
    ]
    assert row["external_id"] is not None and row["external_id"] != old
    assert row["state"] == "open" and row["last_error_code"] is None
    assert _audit(rig, "current.item.create") == creates + 1
    # A third sync calls nothing and files nothing more.
    calls = len(rig.spy.calls)
    assert rig.sync() == current.SyncResult("synced")
    assert len(rig.spy.calls) == calls
    assert len(_items(rig)) == 1
    assert _audit(rig, "current.item.create") == creates + 1


# --- D8: package sets -----------------------------------------------------------------


def test_no_finance_name_reaches_the_registries_or_the_token_sets(rig: Rig) -> None:
    named = rig.surfaces.operations.names() | rig.surfaces.tools.names()
    sets = token_sets.read_only() | token_sets.agent_default() | token_sets.cli_full()
    assert not {name for name in named | sets if name.startswith("finance")}
    assert "current.item.create" in rig.surfaces.operations.names()
