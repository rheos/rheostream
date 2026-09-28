"""``record_evidence_acceptance_audit``: the success audit row for one automatic memory
acceptance, written beside ``record_operation_audit`` in ``dispatch.py`` (spec
§ Architecture "Success audit"; card acceptance item 5, the writer half).

Seams:

- with a sink installed for the given module id, the writer puts exactly one
  ``core.audit_record`` row in the caller's transaction, every column fixed by the
  writer or taken from the context and the two ids it was handed, and nothing else;
- the row is the caller's transaction's: a rollback leaves none;
- with no sink installed for that module id it answers ``False``, raises nothing,
  writes nothing and logs ``audit_row_unwritable`` naming only the label and the
  module id;
- nothing in ``packages``, ``apps`` or ``modules`` calls it yet. Prompt 10 adds the one
  real caller, ``rheo_core/evidence/service.py``, and widens :data:`EXPECTED_CALLERS`.

The ``_AuditWrite`` refactor this writer rests on is pinned by the existing suite, run
unedited: ``tests/test_audit_sink.py``, ``tests/postgres/test_audit_dispatch.py`` (whose
``test_the_success_row_names_the_actor_the_entry_and_the_request`` asserts a
dispatch-written row column by column) and ``tests/postgres/test_approvals.py``.

Reads go through ``list_audit_records``, the repository the supported read wraps: the
acceptance context carries an empty operation set, so it cannot dispatch
``core.audit.list`` itself. Every value is synthetic.
"""

import ast
import logging
from collections.abc import Iterator
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Final
from uuid import UUID

import pytest
from rheo_contracts import ContextPurpose, RecordRef, WorkspaceContext
from rheo_core.audit import AUDIT_SUCCEEDED, list_audit_records, sink_for
from rheo_core.audit.records import AuditRow
from rheo_core.boundary import context_for_evidence_acceptance
from rheo_core.operations import HARNESS_MODULE_ID, register_core_operations
from rheo_core.operations.dispatch import (
    EVIDENCE_ACCEPT_AUDIT,
    record_evidence_acceptance_audit,
)
from rheo_core.refs import uuid7
from rheo_core.storage.routing import open_unit_of_work

pytestmark = pytest.mark.postgres

_REPO_ROOT: Final = Path(__file__).resolve().parents[2]

SCAN_ROOTS: Final = ("packages", "apps", "modules")
"""The shipped tree, the same three roots ``tests/test_audit_sink.py`` scans."""

EXPECTED_CALLERS: Final[frozenset[str]] = frozenset()
"""Files under :data:`SCAN_ROOTS` that call the writer. Empty until Prompt 10, which
sets it to ``{"packages/core/src/rheo_core/evidence/service.py"}``."""

WRITER: Final = "record_evidence_acceptance_audit"

UNSINKED_MODULE_ID: Final = "test.no_sink"
"""A module id nothing installs a sink for."""

DIGEST: Final = sha256(b"a synthetic unit payload").digest()


@pytest.fixture(autouse=True)
def registrations() -> None:
    register_core_operations()


@pytest.fixture
def ctx(workspace: UUID, owner_account_id: UUID) -> WorkspaceContext:
    """The ``system``/``job`` context a drain job accepts a unit under."""
    made = context_for_evidence_acceptance(
        workspace,
        account_id=owner_account_id,
        purpose=ContextPurpose.INTERNAL_ANALYSIS,
    )
    assert isinstance(made, WorkspaceContext), made
    return made


@pytest.fixture
def subject() -> RecordRef:
    return RecordRef.parse(f"harness.note:{uuid7()}")


@pytest.fixture
def no_sink() -> Iterator[None]:
    assert sink_for(UNSINKED_MODULE_ID) is None
    yield


def _rows(ctx: WorkspaceContext) -> tuple[AuditRow, ...]:
    with open_unit_of_work(ctx) as uow:
        return list_audit_records(uow.connection, limit=500)


def _added(ctx: WorkspaceContext, before: tuple[AuditRow, ...]) -> list[AuditRow]:
    seen = {row.id for row in before}
    return [row for row in _rows(ctx) if row.id not in seen]


def test_the_writer_puts_one_content_free_row_in_the_callers_transaction(
    ctx: WorkspaceContext, subject: RecordRef
) -> None:
    before = _rows(ctx)
    at_least = datetime.now(UTC)

    with open_unit_of_work(ctx) as uow:
        written = record_evidence_acceptance_audit(
            ctx,
            uow,
            module_id=HARNESS_MODULE_ID,
            subject_ref=subject,
            request_digest=DIGEST,
        )
        uow.commit()

    assert written is True
    added = _added(ctx, before)
    assert len(added) == 1, added
    row = added[0]
    assert row.operation_name == EVIDENCE_ACCEPT_AUDIT == "core.evidence.accept"
    assert row.safety_class == "mutate"
    assert row.outcome == AUDIT_SUCCEEDED == "succeeded"
    assert row.operation_id is None
    assert row.actor_kind == "system"
    assert row.actor_id is None
    assert row.entry == "job"
    assert row.subject_ref == subject.format()
    assert row.request_digest == DIGEST
    assert row.occurred_at >= at_least


def test_a_rollback_of_the_callers_transaction_leaves_no_row(
    ctx: WorkspaceContext, subject: RecordRef
) -> None:
    """The writer never commits: the row lives or dies with the memory and receipt
    the caller's transaction holds."""
    before = _rows(ctx)

    with open_unit_of_work(ctx) as uow:
        assert record_evidence_acceptance_audit(
            ctx,
            uow,
            module_id=HARNESS_MODULE_ID,
            subject_ref=subject,
            request_digest=DIGEST,
        )
        uow.rollback()

    assert _added(ctx, before) == []


def test_no_sink_for_the_module_answers_false_writes_nothing_and_logs(
    ctx: WorkspaceContext,
    subject: RecordRef,
    no_sink: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    before = _rows(ctx)

    with (
        caplog.at_level(logging.ERROR, logger="rheo_core.operations"),
        open_unit_of_work(ctx) as uow,
    ):
        written = record_evidence_acceptance_audit(
            ctx,
            uow,
            module_id=UNSINKED_MODULE_ID,
            subject_ref=subject,
            request_digest=DIGEST,
        )
        uow.commit()

    assert written is False
    assert _added(ctx, before) == []
    records = [r for r in caplog.records if r.getMessage() == "audit_row_unwritable"]
    assert len(records) == 1, caplog.records
    logged = records[0]
    assert getattr(logged, "operation", None) == EVIDENCE_ACCEPT_AUDIT
    assert getattr(logged, "module_id", None) == UNSINKED_MODULE_ID
    # Content-free: neither id the caller handed over reaches the log line.
    rendered = repr(vars(logged))
    assert subject.format() not in rendered
    assert str(subject.id) not in rendered
    assert DIGEST.hex() not in rendered
    assert repr(DIGEST) not in rendered


def _callers(path: Path) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name == WRITER:
            return True
    return False


def test_nothing_in_the_shipped_tree_calls_the_writer_yet() -> None:
    """The caller pin (spec R5): the writer is not a general audit door. Prompt 10's
    ``settle_unit`` is to be its one caller; until then the set is empty."""
    callers: set[str] = set()
    parsed = 0
    for root in SCAN_ROOTS:
        for path in sorted((_REPO_ROOT / root).rglob("*.py")):
            parsed += 1
            if _callers(path):
                callers.add(path.relative_to(_REPO_ROOT).as_posix())

    # Positive controls: the scan read a real tree, and it does see a call, in this
    # file's own tests above.
    assert parsed > 50, parsed
    assert _callers(Path(__file__)) is True
    assert callers == EXPECTED_CALLERS, sorted(callers)
