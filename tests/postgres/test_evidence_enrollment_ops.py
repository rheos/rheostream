"""``core.evidence_enrollment.create``, ``.rotate`` and ``.revoke`` through the real
dispatch path, and the ``rheo evidence`` operator commands (spec § Architecture →
System Components, item 5; FR 1, FR 2; AC 2 partial).

Seams under test:

- **The bridge token's scope.** ``create`` mints a ``kind='cli'`` token whose snapshot
  is exactly ``["core.evidence.ingest"]`` and whose purpose is
  ``internal_analysis``; presented, it cannot dispatch any other operation. (Prompt 6
  proves the ingest end; ``core.evidence.ingest`` is not registered yet.)
- **The target-account rule.** A session acts for its own account; a token for its
  own account (a ``cli_full`` token of member B revoking member A's enrollment, even
  naming A in the payload, answers ``not_found`` and changes nothing); an operator
  names the account.
- **Token actors cannot create or rotate** (``NON_TOKEN_ISSUABLE``).
- **Rotate keeps the id.** The old token is revoked and matches no active
  enrollment; the new one does; a pending row recorded under the enrollment before
  the rotate still verifies with ``LocalEvidenceAuthority``.
- **Both partial-rotate failures fail closed.**
- **The CLI output contract**: the raw token alone, or exactly one JSON line.

Every fingerprint and text is synthetic. Every enrollment and evidence row is removed
in teardown (#238's rule); the control-plane tokens are left, since ``rheo doctor``
reads only the tokens of active enrollments.
"""

import json
import secrets
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from conftest import ClusterSession
from harness.evidence import EvidenceWorkspace, enable_recording, probe_registry
from harness.registry import add_member
from rheo_app_cli.main import main
from rheo_contracts import ContextPurpose, Role, WorkspaceContext
from rheo_contracts.source_units import (
    AuthorityGrant,
    SanitizedEvidence,
    TrustedSourceUnit,
)
from rheo_core.boundary import context_for_operator
from rheo_core.boundary.context import MEMBERSHIP_MISSING, TOKEN_REVOKED, Refusal
from rheo_core.boundary.factories import context_from_session, context_from_token
from rheo_core.evidence import enrollment as enrollment_module
from rheo_core.evidence.enrollment import (
    ENROLLMENT_CREATE,
    ENROLLMENT_EXISTS,
    ENROLLMENT_REVOKE,
    ENROLLMENT_ROTATE,
)
from rheo_core.evidence.local_authority import (
    ENROLLMENT_INACTIVE,
    LOCAL_PRODUCER_KIND,
    LocalEvidenceAuthority,
)
from rheo_core.evidence.record import NewEvidence, record_evidence
from rheo_core.operations import (
    OPERATION_NOT_PERMITTED,
    WORKSPACE_STATUS,
    OperationOutcome,
    dispatch,
    register_core_operations,
)
from rheo_core.operations.core_ops import TOKEN_ISSUE
from rheo_core.refs.resolver import NOT_FOUND
from rheo_core.sessions import create_session, mint_host_secret, switch_workspace
from rheo_core.storage.backend import StorageRefusal
from rheo_core.storage.control_plane import (
    AccessTokenRow,
    get_access_token,
    list_access_token_operations,
)
from rheo_core.storage.evidence_enrollment_tables import (
    STATE_ACTIVE,
    STATE_REVOKED,
    evidence_enrollment,
)
from rheo_core.storage.evidence_tables import evidence_unit
from sqlalchemy import delete, inspect, select

pytestmark = pytest.mark.postgres

HOST = "example.test"
BRIDGE_SNAPSHOT = frozenset({"core.evidence.ingest"})
TURN = "Remember that the spare tent pegs are in the green crate."


@pytest.fixture(autouse=True)
def registrations() -> None:
    register_core_operations()


@pytest.fixture(autouse=True)
def clean_evidence_rows(cluster: ClusterSession, workspace: UUID) -> Iterator[None]:
    """Remove this test's enrollment and evidence rows, so no enrollment with a
    revoked or expiring token outlives it for ``rheo doctor`` (#238's rule)."""
    yield
    database = cluster.registry_row(workspace).database_name
    engine = cluster.backend.pools.engine_for(database)
    inspector = inspect(engine)
    with engine.begin() as connection:
        if inspector.has_table("evidence_unit", schema="core"):
            connection.execute(delete(evidence_unit))
        if inspector.has_table("evidence_enrollment", schema="core"):
            connection.execute(delete(evidence_enrollment))


# --- helpers ----------------------------------------------------------------------


def _fingerprint() -> str:
    return secrets.token_hex(32)


def _operator(workspace_id: UUID) -> WorkspaceContext:
    ctx = context_for_operator(workspace_id)
    assert isinstance(ctx, WorkspaceContext), ctx
    return ctx


def _session(workspace_id: UUID, account_id: UUID) -> WorkspaceContext:
    session_row = create_session(account_id)
    secret = mint_host_secret(session_row.id, HOST)
    assert switch_workspace(session_row.id, workspace_id) is None
    ctx = context_from_session(secret, HOST)
    assert isinstance(ctx, WorkspaceContext), ctx
    return ctx


def _cli_full(session_ctx: WorkspaceContext) -> WorkspaceContext:
    """A ``cli_full`` token issued for the session's account, presented at ``api``."""
    outcome = dispatch(
        session_ctx, TOKEN_ISSUE, {"kind": "cli", "set_name": "cli_full"}
    )
    assert outcome.ok, outcome
    ctx = context_from_token(outcome.result.value, "api")  # type: ignore[union-attr]
    assert isinstance(ctx, WorkspaceContext), ctx
    return ctx


def _create(
    ctx: WorkspaceContext, *, account_id: UUID | None = None, **fingerprints: str
) -> OperationOutcome:
    payload: dict[str, str] = {
        "machine_fingerprint": fingerprints.get("machine", _fingerprint()),
        "project_fingerprint": fingerprints.get("project", _fingerprint()),
    }
    if account_id is not None:
        payload["account_id"] = str(account_id)
    return dispatch(ctx, ENROLLMENT_CREATE, payload)


def _created(outcome: OperationOutcome) -> Any:
    assert outcome.ok, outcome
    assert outcome.result is not None
    return outcome.result


def _token(cluster: ClusterSession, token_id: UUID) -> AccessTokenRow:
    with cluster.backend.control_engine.connect() as connection:
        row = get_access_token(connection, token_id)
    assert row is not None
    return row


def _enrollment(cluster: ClusterSession, workspace: UUID, enrollment_id: UUID) -> Any:
    database = cluster.registry_row(workspace).database_name
    with cluster.backend.pools.engine_for(database).connect() as connection:
        return connection.execute(
            select(evidence_enrollment).where(evidence_enrollment.c.id == enrollment_id)
        ).one()


def _active_for_token(cluster: ClusterSession, workspace: UUID, token_id: UUID) -> int:
    """How many active enrollments hold ``token_id``: what ingest's first step reads."""
    database = cluster.registry_row(workspace).database_name
    with cluster.backend.pools.engine_for(database).connect() as connection:
        return len(
            connection.execute(
                select(evidence_enrollment.c.id).where(
                    evidence_enrollment.c.token_id == token_id,
                    evidence_enrollment.c.state == STATE_ACTIVE,
                )
            ).all()
        )


# --- create ------------------------------------------------------------------------


def test_operator_enroll_mints_an_ingest_only_bridge_token(
    cluster: ClusterSession, workspace: UUID, owner_account_id: UUID
) -> None:
    created = _created(_create(_operator(workspace), account_id=owner_account_id))

    token = _token(cluster, created.token_id)
    assert token.kind == "cli"
    assert token.issued_from == "operator"
    assert token.purpose == ContextPurpose.INTERNAL_ANALYSIS.value
    assert token.account_id == owner_account_id
    assert token.expires_at == created.expires_at
    with cluster.backend.control_engine.connect() as connection:
        assert list_access_token_operations(connection, token.id) == BRIDGE_SNAPSHOT
    row = _enrollment(cluster, workspace, created.enrollment_id)
    assert row.token_id == created.token_id
    assert row.account_id == owner_account_id
    assert row.state == STATE_ACTIVE
    assert row.purpose == ContextPurpose.INTERNAL_ANALYSIS.value


def test_session_enroll_is_for_the_sessions_own_account(
    cluster: ClusterSession, workspace: UUID, owner_account_id: UUID
) -> None:
    """A payload ``account_id`` naming someone else is ignored for a session."""
    member_id = add_member(
        cluster.backend, workspace, Role.MEMBER, display_name="member-enroll"
    )
    created = _created(
        _create(_session(workspace, member_id), account_id=owner_account_id)
    )
    token = _token(cluster, created.token_id)
    assert token.issued_from == "session"
    assert token.account_id == member_id
    assert (
        _enrollment(cluster, workspace, created.enrollment_id).account_id == member_id
    )


def test_the_bridge_token_can_dispatch_nothing_but_ingest(
    cluster: ClusterSession, workspace: UUID, owner_account_id: UUID
) -> None:
    """Scope at the dispatch level, not only the stored snapshot."""
    created = _created(_create(_operator(workspace), account_id=owner_account_id))
    ctx = context_from_token(created.value, "api")
    assert isinstance(ctx, WorkspaceContext), ctx
    assert ctx.operation_set == BRIDGE_SNAPSHOT
    assert ctx.principal.bound_purpose is ContextPurpose.INTERNAL_ANALYSIS
    assert dispatch(ctx, WORKSPACE_STATUS, {}).state == OPERATION_NOT_PERMITTED
    revoke = {"enrollment_id": str(created.enrollment_id)}
    assert dispatch(ctx, ENROLLMENT_REVOKE, revoke).state == OPERATION_NOT_PERMITTED
    assert _enrollment(cluster, workspace, created.enrollment_id).state == STATE_ACTIVE


def test_operator_enroll_needs_a_member_account(
    cluster: ClusterSession, workspace: UUID
) -> None:
    from rheo_core.storage.control_plane import insert_account

    with cluster.backend.control_engine.begin() as connection:
        stranger = insert_account(connection, display_name="not-a-member").id
    outcome = _create(_operator(workspace), account_id=stranger)
    assert outcome.state == MEMBERSHIP_MISSING


def test_a_cli_full_token_cannot_create(
    cluster: ClusterSession, workspace: UUID, owner_account_id: UUID
) -> None:
    token_ctx = _cli_full(_session(workspace, owner_account_id))
    assert ENROLLMENT_CREATE not in token_ctx.operation_set  # type: ignore[operator]
    assert _create(token_ctx).state == OPERATION_NOT_PERMITTED


def test_a_second_active_enrollment_for_the_pair_is_refused(
    cluster: ClusterSession, workspace: UUID, owner_account_id: UUID
) -> None:
    ctx = _operator(workspace)
    machine, project = _fingerprint(), _fingerprint()
    _created(
        _create(ctx, account_id=owner_account_id, machine=machine, project=project)
    )
    again = _create(ctx, account_id=owner_account_id, machine=machine, project=project)
    assert again.state == ENROLLMENT_EXISTS


# --- revoke ------------------------------------------------------------------------


def test_revoke_revokes_the_token_then_the_row(
    cluster: ClusterSession, workspace: UUID, owner_account_id: UUID
) -> None:
    ctx = _operator(workspace)
    created = _created(_create(ctx, account_id=owner_account_id))
    outcome = dispatch(
        ctx,
        ENROLLMENT_REVOKE,
        {
            "enrollment_id": str(created.enrollment_id),
            "account_id": str(owner_account_id),
        },
    )
    assert outcome.ok, outcome
    assert _token(cluster, created.token_id).revoked_at is not None
    row = _enrollment(cluster, workspace, created.enrollment_id)
    assert row.state == STATE_REVOKED and row.revoked_at is not None
    assert context_from_token(created.value, "api") == Refusal(TOKEN_REVOKED)


def test_a_session_cannot_revoke_another_accounts_enrollment(
    cluster: ClusterSession, workspace: UUID, owner_account_id: UUID
) -> None:
    created = _created(_create(_operator(workspace), account_id=owner_account_id))
    member_id = add_member(
        cluster.backend, workspace, Role.MEMBER, display_name="member-revoke"
    )
    outcome = dispatch(
        _session(workspace, member_id),
        ENROLLMENT_REVOKE,
        {
            "enrollment_id": str(created.enrollment_id),
            "account_id": str(owner_account_id),
        },
    )
    assert outcome.state == NOT_FOUND
    assert _enrollment(cluster, workspace, created.enrollment_id).state == STATE_ACTIVE
    assert _token(cluster, created.token_id).revoked_at is None


def test_a_token_revokes_its_own_accounts_enrollment_only(
    cluster: ClusterSession, workspace: UUID, owner_account_id: UUID
) -> None:
    """Member B's ``cli_full`` token names member A's enrollment, and A in the
    payload: ``not_found``, nothing changes. The same token on B's own succeeds."""
    operator = _operator(workspace)
    member_a = add_member(cluster.backend, workspace, Role.MEMBER, display_name="a")
    member_b = add_member(cluster.backend, workspace, Role.MEMBER, display_name="b")
    of_a = _created(_create(operator, account_id=member_a))
    of_b = _created(_create(operator, account_id=member_b))
    token_b = _cli_full(_session(workspace, member_b))

    crossed = dispatch(
        token_b,
        ENROLLMENT_REVOKE,
        {"enrollment_id": str(of_a.enrollment_id), "account_id": str(member_a)},
    )
    assert crossed.state == NOT_FOUND
    assert _enrollment(cluster, workspace, of_a.enrollment_id).state == STATE_ACTIVE
    assert _token(cluster, of_a.token_id).revoked_at is None

    own = dispatch(
        token_b, ENROLLMENT_REVOKE, {"enrollment_id": str(of_b.enrollment_id)}
    )
    assert own.ok, own
    assert _enrollment(cluster, workspace, of_b.enrollment_id).state == STATE_REVOKED
    assert _token(cluster, of_b.token_id).revoked_at is not None


def test_revoking_a_revoked_enrollment_refuses(
    cluster: ClusterSession, workspace: UUID, owner_account_id: UUID
) -> None:
    ctx = _operator(workspace)
    created = _created(_create(ctx, account_id=owner_account_id))
    payload = {
        "enrollment_id": str(created.enrollment_id),
        "account_id": str(owner_account_id),
    }
    assert dispatch(ctx, ENROLLMENT_REVOKE, payload).ok
    assert dispatch(ctx, ENROLLMENT_REVOKE, payload).state == ENROLLMENT_INACTIVE


# --- rotate ------------------------------------------------------------------------


def _record_pending(ev: EvidenceWorkspace, enrollment_id: UUID) -> NewEvidence:
    """One pending local turn of the owner under the enrollment, as ingest writes it."""
    ctx = ev.context(ContextPurpose.INTERNAL_ANALYSIS)
    record = NewEvidence(
        producer_kind=LOCAL_PRODUCER_KIND,
        authority_id=enrollment_id,
        native_key=f"cc1:{_fingerprint()}",
        speaker_account_id=ev.owner_account_id,
        audience_kind="member",
        audience_id=ev.owner_account_id,
        purpose=ContextPurpose.INTERNAL_ANALYSIS,
        recorded_at=datetime.now(UTC),
        raw_text=TURN,
    )
    with ev.handler(probe_registry()) as uow:
        attempt = record_evidence(ctx, uow, [record], now=datetime.now(UTC))
    assert attempt.accepted == (record.native_key,), attempt
    return record


def _unit(record: NewEvidence) -> TrustedSourceUnit:
    return TrustedSourceUnit(
        producer_kind=LOCAL_PRODUCER_KIND,
        authority_id=record.authority_id,
        principal_account_id=record.speaker_account_id,
        audience_kind=record.audience_kind,
        audience_id=record.audience_id,
        bound_purpose=record.purpose,
        source_recorded_at=record.recorded_at,
        source_expires_at=record.recorded_at + timedelta(hours=24),
        external_source_key=record.native_key,
        evidence=SanitizedEvidence(
            kind="note", title="Tent pegs", body="In the crate."
        ),
    )


def test_rotate_keeps_the_id_and_replaces_the_token(
    monkeypatch: pytest.MonkeyPatch,
    cluster: ClusterSession,
    workspace: UUID,
    owner_account_id: UUID,
) -> None:
    ev = EvidenceWorkspace(cluster, workspace, owner_account_id)
    enable_recording(monkeypatch, ev)
    ctx = _operator(workspace)
    old = _created(_create(ctx, account_id=owner_account_id))
    pending = _record_pending(ev, old.enrollment_id)

    new = _created(
        dispatch(
            ctx,
            ENROLLMENT_ROTATE,
            {
                "enrollment_id": str(old.enrollment_id),
                "account_id": str(owner_account_id),
            },
        )
    )

    assert new.enrollment_id == old.enrollment_id
    assert new.token_id != old.token_id
    assert _enrollment(cluster, workspace, old.enrollment_id).token_id == new.token_id
    # The old token: revoked, and no active enrollment holds it.
    assert context_from_token(old.value, "api") == Refusal(TOKEN_REVOKED)
    assert _active_for_token(cluster, workspace, old.token_id) == 0
    # The new token: presents with the ingest-only snapshot and holds the enrollment.
    new_ctx = context_from_token(new.value, "api")
    assert isinstance(new_ctx, WorkspaceContext), new_ctx
    assert new_ctx.operation_set == BRIDGE_SNAPSHOT
    assert _active_for_token(cluster, workspace, new.token_id) == 1
    # A row recorded before the rotate still verifies: authority_id is unchanged.
    with ev.unit_of_work() as uow:
        verdict = LocalEvidenceAuthority(uow).verify(
            _unit(pending), workspace_id=workspace, now=datetime.now(UTC)
        )
    assert isinstance(verdict, AuthorityGrant), verdict
    assert verdict.authority_id == old.enrollment_id


def test_a_token_cannot_rotate(
    cluster: ClusterSession, workspace: UUID, owner_account_id: UUID
) -> None:
    created = _created(_create(_operator(workspace), account_id=owner_account_id))
    token_ctx = _cli_full(_session(workspace, owner_account_id))
    outcome = dispatch(
        token_ctx, ENROLLMENT_ROTATE, {"enrollment_id": str(created.enrollment_id)}
    )
    assert outcome.state == OPERATION_NOT_PERMITTED
    assert _enrollment(cluster, workspace, created.enrollment_id).token_id == (
        created.token_id
    )


def test_a_failed_row_update_leaves_the_new_token_matching_nothing(
    monkeypatch: pytest.MonkeyPatch,
    cluster: ClusterSession,
    workspace: UUID,
    owner_account_id: UUID,
) -> None:
    ctx = _operator(workspace)
    old = _created(_create(ctx, account_id=owner_account_id))
    minted: list[UUID] = []
    real_mint = enrollment_module._mint

    def spy_mint(*args: Any, **kwargs: Any) -> Any:
        result = real_mint(*args, **kwargs)
        minted.append(result[0])
        return result

    def broken_update(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("synthetic row-update failure")

    monkeypatch.setattr(enrollment_module, "_mint", spy_mint)
    monkeypatch.setattr(enrollment_module, "_set_token_id", broken_update)
    outcome = dispatch(
        ctx,
        ENROLLMENT_ROTATE,
        {"enrollment_id": str(old.enrollment_id), "account_id": str(owner_account_id)},
    )

    assert not outcome.ok
    (new_token_id,) = minted
    assert _active_for_token(cluster, workspace, new_token_id) == 0
    assert _enrollment(cluster, workspace, old.enrollment_id).token_id == old.token_id


def test_a_failed_old_token_revoke_leaves_the_old_token_matching_nothing(
    monkeypatch: pytest.MonkeyPatch,
    cluster: ClusterSession,
    workspace: UUID,
    owner_account_id: UUID,
) -> None:
    ctx = _operator(workspace)
    old = _created(_create(ctx, account_id=owner_account_id))

    def broken_revoke(token_id: UUID) -> None:
        raise StorageRefusal("access_token_missing", "synthetic revoke failure")

    monkeypatch.setattr(enrollment_module, "_revoke_token_if_live", broken_revoke)
    new = _created(
        dispatch(
            ctx,
            ENROLLMENT_ROTATE,
            {
                "enrollment_id": str(old.enrollment_id),
                "account_id": str(owner_account_id),
            },
        )
    )

    # The revoke failed, so the old token is still live, but it matches nothing.
    assert _token(cluster, old.token_id).revoked_at is None
    assert _active_for_token(cluster, workspace, old.token_id) == 0
    assert _active_for_token(cluster, workspace, new.token_id) == 1


# --- the operator CLI --------------------------------------------------------------


def _run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    capsys.readouterr()
    code = main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


def test_enroll_prints_only_the_raw_token(
    cluster: ClusterSession,
    capsys: pytest.CaptureFixture[str],
    workspace: UUID,
    owner_account_id: UUID,
) -> None:
    code, out, err = _run(
        capsys,
        "evidence",
        "enroll",
        "--account",
        str(owner_account_id),
        "--workspace",
        str(workspace),
        "--machine",
        _fingerprint(),
        "--project",
        _fingerprint(),
    )
    assert code == 0, err
    lines = out.splitlines()
    assert len(lines) == 1 and out == lines[0] + "\n"
    assert lines[0].startswith("rheo_cli_")
    ctx = context_from_token(lines[0], "api")
    assert isinstance(ctx, WorkspaceContext), ctx
    assert ctx.operation_set == BRIDGE_SNAPSHOT


def test_enroll_json_prints_exactly_one_line_and_revoke_takes_only_the_id(
    cluster: ClusterSession,
    capsys: pytest.CaptureFixture[str],
    workspace: UUID,
    owner_account_id: UUID,
) -> None:
    code, out, err = _run(
        capsys,
        "evidence",
        "enroll",
        "--account",
        str(owner_account_id),
        "--workspace",
        str(workspace),
        "--machine",
        _fingerprint(),
        "--project",
        _fingerprint(),
        "--json",
    )
    assert code == 0, err
    (line,) = out.splitlines()
    parsed = json.loads(line)
    assert set(parsed) == {"enrollment_id", "token", "expires_at"}
    assert parsed["token"].startswith("rheo_cli_")
    enrollment_id = UUID(parsed["enrollment_id"])

    code, out, err = _run(
        capsys,
        "evidence",
        "rotate",
        str(enrollment_id),
        "--account",
        str(owner_account_id),
        "--workspace",
        str(workspace),
        "--json",
    )
    assert code == 0, err
    (rotated,) = out.splitlines()
    assert set(json.loads(rotated)) == {"enrollment_id", "token", "expires_at"}
    assert json.loads(rotated)["enrollment_id"] == str(enrollment_id)

    code, out, err = _run(capsys, "evidence", "revoke", str(enrollment_id))
    assert code == 0, err
    assert out == ""
    assert _enrollment(cluster, workspace, enrollment_id).state == STATE_REVOKED
