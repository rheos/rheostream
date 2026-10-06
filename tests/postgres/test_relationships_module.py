"""Synthetic Relationships behavior through real installation, dispatch and approval."""

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
from conftest import ClusterSession, MakeWorkspace
from harness.modules import (
    LoadedSurfaces,
    install_and_enable_module,
    loaded_probe_modules,
)
from rheo_contracts import RecordRef, Role, WorkspaceContext
from rheo_core.boundary import context_for_harness, context_for_operator
from rheo_core.boundary.factories import context_for_event_consumer
from rheo_core.deletion.tables import deletion_record
from rheo_core.events import ConsumerRegistry
from rheo_core.operations import dispatch, register_core_operations
from rheo_core.operations.dispatch import OperationOutcome
from rheo_core.operations.refusals import OperationRefused
from rheo_core.refs import uuid7
from rheo_core.refs.resolver import register_resolver
from rheo_core.settings import resolver as settings_resolver
from rheo_core.settings.schema import REGISTRY, SettingsRegistry
from rheo_core.storage.backend import HandlerUnitOfWork
from rheo_core.storage.routing import open_unit_of_work
from rheo_core.storage.work_tables import outbox_event
from rheo_relationships import MANIFEST
from rheo_relationships.configuration import SETTINGS
from rheo_relationships.contracts import ResolveInput
from rheo_relationships.matching import resolve_or_create
from rheo_relationships.resolvers import resolve_party
from rheo_relationships.storage import tables as t
from sqlalchemy import func, inspect, select

pytestmark = pytest.mark.postgres


@dataclass
class Relationships:
    cluster: ClusterSession
    workspace: UUID
    ctx: WorkspaceContext
    surfaces: LoadedSurfaces
    consumers: ConsumerRegistry

    def call(self, name: str, **payload: Any) -> OperationOutcome:
        return dispatch(
            self.ctx,
            name if name.startswith("core.") else "relationships." + name,
            payload,
            registry=self.surfaces.operations,
            consumers=self.consumers,
        )

    def ok(self, name: str, **payload: Any) -> Any:
        outcome = self.call(name, **payload)
        assert outcome.ok, outcome
        return outcome.result

    def create(self, name: str = "Example Person", kind: str = "person") -> Any:
        return self.ok("party.create", kind=kind, display_name=name)

    def get(self, reference: str) -> Any:
        return self.ok("party.get", ref=reference)

    def resolve(self, namespace: str = "example-form", **kwargs: Any) -> Any:
        payload = {
            "source_namespace": namespace,
            "occurred_at": datetime.now(UTC).isoformat(),
            "evidence_ref": f"fixture.observation:{uuid7()}",
            **kwargs,
        }
        # Use the trusted internal consumer boundary, not a fabricated service role.
        # It grants no public operations and does not simulate transport authentication.
        with open_unit_of_work(self.ctx) as uow:
            ctx = context_for_event_consumer(
                self.workspace, uow, request_id=self.ctx.request_id
            )
            assert isinstance(ctx, WorkspaceContext) and ctx.role is Role.SERVICE
            result = resolve_or_create(
                ctx,
                HandlerUnitOfWork(uow, consumers=self.consumers),
                ResolveInput.model_validate(payload),
            )
            uow.commit()
            return result

    def merge(self, source: str, target: str) -> Any:
        return self.ok(
            "party.merge",
            source_ref=source,
            target_ref=target,
            source_revision=self.get(source).revision,
            target_revision=self.get(target).revision,
            evidence="Synthetic review",
        )

    def count(self, table: Any) -> int:
        with open_unit_of_work(self.ctx) as uow:
            return int(
                uow.connection.execute(
                    select(func.count()).select_from(table)
                ).scalar_one()
            )


@pytest.fixture
def relationships(
    cluster: ClusterSession,
    workspace: UUID,
    owner_account_id: UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Relationships]:
    with loaded_probe_modules(monkeypatch, "relationships") as surfaces:
        settings = SettingsRegistry()
        for spec in REGISTRY.specs():
            settings.register(spec, origin=REGISTRY.origin_of(spec.key))
        for spec in SETTINGS:
            settings.register(spec, origin="relationships")
        monkeypatch.setattr(settings_resolver, "REGISTRY", settings)
        register_core_operations()
        register_core_operations(surfaces.operations)
        register_resolver(
            "relationships", "party", resolve_party, origin="relationships"
        )
        operator = context_for_operator(workspace)
        assert isinstance(operator, WorkspaceContext)
        install_and_enable_module(cluster.backend, operator, workspace, "relationships")
        ctx = context_for_harness(workspace, owner_account_id, Role.OWNER)
        assert isinstance(ctx, WorkspaceContext)
        yield Relationships(cluster, workspace, ctx, surfaces, ConsumerRegistry())


def test_relationships_install_without_other_modules(
    relationships: Relationships,
) -> None:
    assert relationships.ctx.enabled_modules == frozenset({"relationships"})
    assert MANIFEST.dependencies == ()
    with open_unit_of_work(relationships.ctx) as uow:
        names = set(inspect(uow.connection).get_table_names(schema="relationships"))
        assert names == {table.name for table in t.metadata.tables.values()} | {
            "alembic_version_relationships"
        }
    party = relationships.create()
    assert party.kind == "person"
    assert len(MANIFEST.operations) == 15 and len(MANIFEST.tools) == 7


def test_relationships_verified_identity_is_namespace_bound(
    relationships: Relationships,
) -> None:
    one = relationships.resolve(verified_email="Member@example.com")
    two = relationships.resolve(verified_email="member@EXAMPLE.COM")
    other = relationships.resolve("other-source", verified_email="member@example.com")
    assert two.party_ref == one.party_ref and two.outcome == "linked_email"
    assert other.party_ref != one.party_ref
    assert relationships.count(t.party) == 2
    rows = relationships.ok("review_candidate.list").items
    assert [r["hint_kind"] for r in rows] == ["email_verified_other_source"]


def test_relationships_weak_hints_create_three_candidates(
    relationships: Relationships,
) -> None:
    hints = {
        "name": "Example Person",
        "phone": "555-0100",
        "organization_domain": "example.org",
    }
    one = relationships.resolve(hints=hints)
    two = relationships.resolve(hints=hints)
    assert one.party_ref != two.party_ref
    assert relationships.count(t.party) == 3
    assert {
        r["hint_kind"] for r in relationships.ok("review_candidate.list").items
    } == {"name", "phone", "domain"}
    assert relationships.count(t.review_candidate) == 3
    assert relationships.get(one.party_ref).canonical_ref == one.party_ref


def test_relationships_subject_precedence_refuses_conflicting_identities(
    relationships: Relationships,
) -> None:
    first = relationships.resolve(external_subject_id="person-1")
    second = relationships.resolve(verified_email="other@example.com")
    with pytest.raises(OperationRefused, match="identity_conflict"):
        relationships.resolve(
            external_subject_id="person-1", verified_email="other@example.com"
        )
    assert (
        relationships.get(first.party_ref).canonical_ref
        != relationships.get(second.party_ref).canonical_ref
    )


def test_relationships_concurrent_resolution_creates_one_identity(
    relationships: Relationships,
) -> None:
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda _: relationships.resolve(verified_email="shared@example.com"),
                range(2),
            )
        )
    assert results[0].party_ref == results[1].party_ref
    assert relationships.count(t.party) == 1
    assert relationships.count(t.contact_point) == 1


def test_relationships_retired_contacts_and_alias_names_do_not_match(
    relationships: Relationships,
) -> None:
    party = relationships.create("Old Example")
    contact = relationships.ok(
        "contact_point.add",
        party_ref=party.ref,
        revision=party.revision,
        kind="phone",
        value="555-0100",
    )
    relationships.ok(
        "contact_point.retire",
        party_ref=party.ref,
        revision=contact.revision,
        contact_point_id=str(contact.id),
    )
    survivor = relationships.create("Different Name")
    relationships.merge(party.ref, survivor.ref)
    result = relationships.resolve(hints={"name": "Old Example", "phone": "555-0100"})
    assert result.review_candidate_refs == []


def test_relationships_rejection_suppresses_only_the_same_hint(
    relationships: Relationships,
) -> None:
    one = relationships.resolve(
        verified_email="first@example.com", hints={"name": "Example Person"}
    )
    two = relationships.resolve(hints={"name": "Example Person", "phone": "555-0100"})
    candidate = relationships.ok("review_candidate.list").items[0]
    relationships.ok(
        "review_candidate.resolve",
        candidate_ref=f"relationships.review_candidate:{candidate['id']}",
        decision="reject",
    )
    linked = relationships.resolve(
        verified_email="first@example.com",
        hints={"name": "Example Person", "phone": "555-0100"},
    )
    assert linked.party_ref == one.party_ref and linked.outcome == "linked_email"
    assert one.party_ref != two.party_ref
    rows = relationships.ok("review_candidate.list").items
    assert [r["hint_kind"] for r in rows] == ["phone"]
    assert relationships.count(t.review_candidate) == 2


def test_relationships_merge_unmerge_restores_contacts_and_aliases(
    relationships: Relationships,
) -> None:
    a = relationships.resolve(verified_email="alpha@example.com")
    b = relationships.resolve(verified_email="beta@example.com")
    c = relationships.resolve(external_subject_id="third")
    first = relationships.merge(c.party_ref, a.party_ref)
    second = relationships.merge(a.party_ref, b.party_ref)
    assert relationships.get(c.party_ref).canonical_ref == b.party_ref
    assert (
        relationships.call(
            "party.unmerge", merge_record_ref=first.merge_record_ref
        ).state
        == "survivor_merged_onward"
    )
    relationships.ok("party.unmerge", merge_record_ref=second.merge_record_ref)
    assert relationships.get(c.party_ref).canonical_ref == a.party_ref
    relationships.ok("party.unmerge", merge_record_ref=first.merge_record_ref)
    assert relationships.get(c.party_ref).canonical_ref == c.party_ref
    assert (
        relationships.resolve(verified_email="alpha@example.com").party_ref
        == a.party_ref
    )
    assert (
        relationships.resolve(verified_email="beta@example.com").party_ref
        == b.party_ref
    )
    assert relationships.resolve(external_subject_id="third").party_ref == c.party_ref


def test_relationships_affiliation_history_and_collision_refusal(
    relationships: Relationships,
) -> None:
    a = relationships.create("First Example")
    b = relationships.create("Second Example")
    organization = relationships.create("Example Organization", "organization")
    first = relationships.ok(
        "affiliation.add",
        person_ref=a.ref,
        organization_ref=organization.ref,
        revision=a.revision,
        valid_from="2024-01-01",
    )
    relationships.ok(
        "affiliation.end",
        person_ref=a.ref,
        revision=first.revision,
        affiliation_id=str(first.id),
        valid_to="2024-12-31",
    )
    relationships.ok(
        "affiliation.add",
        person_ref=b.ref,
        organization_ref=organization.ref,
        revision=b.revision,
        valid_from="2024-01-01",
    )
    result = relationships.call(
        "party.merge",
        source_ref=a.ref,
        target_ref=b.ref,
        source_revision=relationships.get(a.ref).revision,
        target_revision=relationships.get(b.ref).revision,
    )
    assert result.state == "affiliation_conflict"
    assert relationships.count(t.merge_record) == 0
    assert relationships.count(t.affiliation) == 2


def test_relationships_stale_writes_and_cross_party_contact_refused(
    relationships: Relationships,
) -> None:
    a = relationships.create()
    b = relationships.create()
    contact = relationships.ok(
        "contact_point.add",
        party_ref=a.ref,
        revision=a.revision,
        kind="email",
        value="example@example.com",
    )
    assert (
        relationships.call(
            "party.update", ref=a.ref, revision=a.revision, display_name="Changed"
        ).state
        == "record_stale"
    )
    assert (
        relationships.call(
            "contact_point.retire",
            party_ref=b.ref,
            revision=b.revision,
            contact_point_id=str(contact.id),
        ).state
        == "not_found"
    )


def test_relationships_workspace_isolation(
    relationships: Relationships, make_workspace: MakeWorkspace, owner_account_id: UUID
) -> None:
    other = make_workspace()
    operator = context_for_operator(other)
    assert isinstance(operator, WorkspaceContext)
    install_and_enable_module(
        relationships.cluster.backend, operator, other, "relationships"
    )
    ctx = context_for_harness(other, owner_account_id, Role.OWNER)
    assert isinstance(ctx, WorkspaceContext)
    second = Relationships(
        relationships.cluster,
        other,
        ctx,
        relationships.surfaces,
        relationships.consumers,
    )
    a = relationships.resolve(verified_email="member@example.com")
    b = second.resolve(verified_email="member@example.com")
    assert a.party_ref != b.party_ref
    assert relationships.count(t.party) == second.count(t.party) == 1
    assert second.call("party.get", ref=a.party_ref).state == "not_found"


def test_relationships_approved_delete_records_each_alias(
    relationships: Relationships,
) -> None:
    source = relationships.create("Alias Example")
    target = relationships.create("Survivor Example")
    relationships.merge(source.ref, target.ref)
    assert (
        relationships.call("core.record.delete", ref=source.ref).state
        == "party_is_alias"
    )
    held = relationships.call("core.record.delete", ref=target.ref)
    assert held.state == "approval_required", held
    assert relationships.count(t.party) == 2
    done = relationships.call(
        "core.approval.approve", approval_id=str(held.approval_id)
    )
    assert done.ok, done
    assert relationships.count(t.party) == 0
    with open_unit_of_work(relationships.ctx) as uow:
        records = uow.connection.execute(select(deletion_record)).mappings().all()
        assert {f"{r['record_type']}:{r['record_id']}" for r in records} == {
            source.ref,
            target.ref,
        }
        events = (
            uow.connection.execute(
                select(outbox_event).where(outbox_event.c.type == "core.record.deleted")
            )
            .mappings()
            .all()
        )
        assert {r["subject_ref"] for r in events} == {source.ref, target.ref}
        assert (
            uow.connection.execute(select(t.merge_record.c.evidence)).scalar_one()
            is None
        )


def test_relationships_merge_failure_rolls_back_without_registry(
    relationships: Relationships,
) -> None:
    a = relationships.create("First Example")
    b = relationships.create("Second Example")
    result = dispatch(
        relationships.ctx,
        "relationships.party.merge",
        {
            "source_ref": a.ref,
            "target_ref": b.ref,
            "source_revision": a.revision,
            "target_revision": b.revision,
        },
        registry=relationships.surfaces.operations,
    )
    assert result.state == "consumers_missing"
    assert relationships.count(t.merge_record) == 0
    assert relationships.get(a.ref).canonical_ref == a.ref


def test_relationships_unmerge_leaves_new_contacts_on_survivor(
    relationships: Relationships,
) -> None:
    a = relationships.resolve(verified_email="first@example.com")
    b = relationships.create("Survivor Example")
    merged = relationships.merge(a.party_ref, b.ref)
    survivor = relationships.get(b.ref)
    relationships.ok(
        "contact_point.add",
        party_ref=b.ref,
        revision=survivor.revision,
        kind="email",
        value="new@example.com",
    )
    relationships.ok("party.unmerge", merge_record_ref=merged.merge_record_ref)
    assert [row["value"] for row in relationships.get(a.party_ref).contact_points] == [
        "first@example.com"
    ]
    assert [row["value"] for row in relationships.get(b.ref).contact_points] == [
        "new@example.com"
    ]


def test_relationships_review_accepts_newer_party_into_older(
    relationships: Relationships,
) -> None:
    a = relationships.resolve(hints={"name": "Example Person"})
    b = relationships.resolve(hints={"name": "Example Person"})
    candidate = relationships.ok("review_candidate.list").items[0]
    relationships.ok(
        "review_candidate.resolve",
        candidate_ref=f"relationships.review_candidate:{candidate['id']}",
        decision="accept",
    )
    assert relationships.get(b.party_ref).canonical_ref == a.party_ref
    assert (
        relationships.ok("review_candidate.list", state="accepted").items[0][
            "merge_record_id"
        ]
        is not None
    )


def test_relationships_delete_approval_is_revision_bound(
    relationships: Relationships,
) -> None:
    party = relationships.create()
    held = relationships.call("core.record.delete", ref=party.ref)
    assert held.state == "approval_required"
    relationships.ok(
        "party.update",
        ref=party.ref,
        revision=party.revision,
        display_name="Updated Example",
    )
    outcome = relationships.call(
        "core.approval.approve", approval_id=str(held.approval_id)
    )
    assert outcome.ok and outcome.result.state == "refused", outcome
    assert relationships.count(t.party) == 1
    assert relationships.count(deletion_record) == 0


def test_relationships_alias_participant_failure_rolls_back_everything(
    relationships: Relationships, monkeypatch: pytest.MonkeyPatch
) -> None:
    from rheo_core.deletion.registry import (
        NOTHING_REMOVED,
        OWNED_DELETIONS,
        Disposition,
        RemovedMemories,
    )
    from rheo_core.operations.refusals import OperationRefused
    from rheo_core.storage.backend import UnitOfWork

    source = relationships.create("Alias Example")
    target = relationships.create("Survivor Example")
    relationships.merge(source.ref, target.ref)
    seen = []

    def participant(
        ctx: WorkspaceContext,
        uow: UnitOfWork,
        reference: RecordRef,
        *,
        disposition: Disposition,
    ) -> RemovedMemories:
        seen.append(reference.format())
        if reference.format() == source.ref:
            raise OperationRefused("fixture_failure", "synthetic alias failure")
        return NOTHING_REMOVED

    monkeypatch.setattr(
        OWNED_DELETIONS, "_participants", list(OWNED_DELETIONS._participants)
    )
    OWNED_DELETIONS.register_participant(
        "relationships", ("relationships.party",), participant
    )
    held = relationships.call("core.record.delete", ref=target.ref)
    outcome = relationships.call(
        "core.approval.approve", approval_id=str(held.approval_id)
    )
    assert outcome.state == "fixture_failure", outcome
    assert source.ref in seen
    assert relationships.count(t.party) == 2
    assert relationships.count(deletion_record) == 0
    assert relationships.get(source.ref).canonical_ref == target.ref


def test_relationships_export_restore_preserves_unmerge(
    relationships: Relationships, make_workspace: MakeWorkspace, owner_account_id: UUID
) -> None:
    from types import SimpleNamespace

    from pydantic_core import to_jsonable_python
    from rheo_relationships.export import export_records, import_records

    a = relationships.resolve(verified_email="original@example.com")
    b = relationships.create("Target Example")
    merged = relationships.merge(a.party_ref, b.ref)
    with open_unit_of_work(relationships.ctx) as uow:
        rows = to_jsonable_python(
            export_records(SimpleNamespace(connection=uow.connection))
        )
    other = make_workspace()
    operator = context_for_operator(other)
    assert isinstance(operator, WorkspaceContext)
    install_and_enable_module(
        relationships.cluster.backend, operator, other, "relationships"
    )
    ctx = context_for_harness(other, owner_account_id, Role.OWNER)
    assert isinstance(ctx, WorkspaceContext)
    with open_unit_of_work(ctx) as uow:
        finish = import_records(uow, rows)
        finish()
        uow.commit()
    restored = Relationships(
        relationships.cluster,
        other,
        ctx,
        relationships.surfaces,
        relationships.consumers,
    )
    assert restored.get(a.party_ref).canonical_ref == b.ref
    restored.ok("party.unmerge", merge_record_ref=merged.merge_record_ref)
    assert (
        restored.resolve(verified_email="original@example.com").party_ref == a.party_ref
    )


@pytest.mark.parametrize(
    "invalid", ["missing_target", "foreign_type", "duplicate", "wrong_revision"]
)
def test_relationships_invalid_owned_deletion_set_rolls_back(
    relationships: Relationships, monkeypatch: pytest.MonkeyPatch, invalid: str
) -> None:
    from rheo_core.deletion import (
        OWNED_DELETIONS,
        DeleteAuthorization,
        OwnedDeletion,
        RemovedOwnedRecords,
    )
    from rheo_core.storage.backend import UnitOfWork

    party = relationships.create()
    reference = RecordRef.parse(party.ref)
    owner = OWNED_DELETIONS.owner_of(reference)
    assert owner is not None

    def invalid_delete(
        ctx: WorkspaceContext, uow: UnitOfWork, authorization: DeleteAuthorization
    ) -> RemovedOwnedRecords:
        owner.delete_owned(ctx, uow, authorization)
        records = (authorization,)
        if invalid == "missing_target":
            records = ()
        elif invalid == "foreign_type":
            records = (
                authorization,
                DeleteAuthorization(
                    RecordRef(module="fixture", record_type="party", id=uuid7()), 1
                ),
            )
        elif invalid == "duplicate":
            records = (authorization, authorization)
        else:
            records = (DeleteAuthorization(reference, authorization.revision + 1),)
        return RemovedOwnedRecords(records=records)

    monkeypatch.setattr(OWNED_DELETIONS, "_owners", dict(OWNED_DELETIONS._owners))
    OWNED_DELETIONS._owners[("relationships", "party")] = OwnedDeletion(
        "relationships", "party", owner.authorize_delete, invalid_delete
    )
    held = relationships.call("core.record.delete", ref=party.ref)
    outcome = relationships.call(
        "core.approval.approve", approval_id=str(held.approval_id)
    )
    assert outcome.state == "deletion_result_invalid"
    assert relationships.count(t.party) == 1
    assert relationships.count(deletion_record) == 0


def test_relationships_affiliation_review_uses_original_occurrence(
    relationships: Relationships,
) -> None:
    organization = relationships.create("Example Company", "organization")
    person = relationships.resolve(
        occurred_at="2023-05-06T12:00:00Z",
        hints={"organization_name": "Example Company"},
    )
    rows = relationships.ok("review_candidate.list").items
    assert len(rows) == 1 and rows[0]["kind"] == "affiliation"
    relationships.ok(
        "review_candidate.resolve",
        candidate_ref=person.review_candidate_refs[0],
        decision="accept",
    )
    affiliation = relationships.get(person.party_ref).affiliations[0]
    assert str(affiliation["valid_from"]) == "2023-05-06"
    assert affiliation["organization_id"] == RecordRef.parse(organization.ref).id


def test_relationships_name_candidates_are_capped_and_no_self_match(
    relationships: Relationships,
) -> None:
    for _ in range(12):
        relationships.create("Example Person")
    person = relationships.resolve(
        verified_email="unique@example.com", hints={"name": "Example Person"}
    )
    assert len(person.review_candidate_refs) == 10
    again = relationships.resolve(
        verified_email="unique@example.com", hints={"name": "Example Person"}
    )
    assert again.party_ref == person.party_ref
    assert set(again.review_candidate_refs) == set(person.review_candidate_refs)
    assert relationships.count(t.review_candidate) == 10


def test_relationships_find_by_normalized_domain_and_bad_domain_refusal(
    relationships: Relationships,
) -> None:
    organization = relationships.create("Example Company", "organization")
    relationships.ok(
        "contact_point.add",
        party_ref=organization.ref,
        revision=organization.revision,
        kind="url",
        value="https://example.org/about",
    )
    found = relationships.ok("party.find", query="https://EXAMPLE.ORG/contact")
    assert [row["ref"] for row in found.items] == [organization.ref]
    result = relationships.call(
        "contact_point.add",
        party_ref=organization.ref,
        revision=relationships.get(organization.ref).revision,
        kind="url",
        value="http://[",
    )
    assert result.state == "input_invalid"
    assert relationships.count(t.contact_point) == 1


def test_relationships_tools_roundtrip_canonical_refs_and_hold_delete(
    relationships: Relationships,
) -> None:
    from rheo_core.operations.tool_facade import call_registered_tool

    a = relationships.create("First Example")
    b = relationships.create("Second Example")

    def tool(name: str, **payload: Any) -> Any:
        return call_registered_tool(
            relationships.ctx,
            "relationships_" + name,
            payload,
            consumers=relationships.consumers,
            tools=relationships.surfaces.tools,
            registry=relationships.surfaces.operations,
        )

    read = tool("get_party", ref=a.ref)
    assert read.ok, read
    merged = tool(
        "merge_parties",
        source_ref=a.ref,
        target_ref=b.ref,
        source_revision=a.revision,
        target_revision=b.revision,
    )
    assert merged.ok, merged
    assert relationships.get(a.ref).canonical_ref == b.ref
    held = tool("delete_party", ref=b.ref)
    assert held.state == "approval_required", held
    invalid = tool("delete_party", ref=f"fixture.party:{uuid7()}")
    assert invalid.state == "input_invalid", invalid
    declaration = next(x for x in MANIFEST.tools if x.name == "relationships_get_party")
    assert (
        declaration.input_model.model_json_schema()["properties"]["ref"]["type"]
        == "string"
    )


def test_relationships_accounts_cannot_assert_verified_source_identity(
    relationships: Relationships,
) -> None:
    from harness.registry import add_member

    account = add_member(
        relationships.cluster.backend,
        relationships.workspace,
        Role.MEMBER,
        display_name="Example Member",
    )
    member = context_for_harness(relationships.workspace, account, Role.MEMBER)
    assert isinstance(member, WorkspaceContext)
    payload = {
        "source_namespace": "example-form",
        "verified_email": "claimed@example.com",
        "external_subject_id": "claimed-subject",
        "evidence_ref": f"fixture.observation:{uuid7()}",
        "occurred_at": datetime.now(UTC).isoformat(),
    }
    for ctx in (relationships.ctx, member):
        outcome = dispatch(
            ctx,
            "relationships.party.resolve_or_create",
            payload,
            registry=relationships.surfaces.operations,
            consumers=relationships.consumers,
        )
        assert outcome.state == "role_not_permitted"
        with (
            open_unit_of_work(ctx) as uow,
            pytest.raises(OperationRefused, match="role_not_permitted"),
        ):
            resolve_or_create(ctx, uow, ResolveInput.model_validate(payload))
    assert relationships.count(t.party) == relationships.count(t.contact_point) == 0
    assert (
        relationships.resolve(verified_email="claimed@example.com").outcome == "created"
    )


@pytest.mark.parametrize(
    "hints",
    [
        {"organization_domain": "http://["},
        {"phone": "n/a"},
        {"phone": "+---"},
        {"email": "   "},
        {
            "name": "   ",
            "organization_name": "  ",
            "organization_domain": "http://[",
            "phone": "n/a",
            "email": "  ",
        },
    ],
)
def test_relationships_bad_weak_hints_do_not_block_verified_identity(
    relationships: Relationships, hints: dict[str, str]
) -> None:
    first = relationships.resolve(verified_email="valid@example.com", hints=hints)
    linked = relationships.resolve(verified_email="valid@example.com", hints=hints)
    assert first.party_ref == linked.party_ref
    assert linked.outcome == "linked_email"
    assert relationships.count(t.party) == relationships.count(t.contact_point) == 1
    assert linked.review_candidate_refs == []


def test_relationships_unmerge_publishes_references_atomically(
    relationships: Relationships, monkeypatch: pytest.MonkeyPatch
) -> None:
    import rheo_relationships.merge as merge_module

    a = relationships.create("First Example")
    b = relationships.create("Second Example")
    merged = relationships.merge(a.ref, b.ref)
    original = merge_module.publish

    def fail(*args: Any, **kwargs: Any) -> None:
        raise OperationRefused("fixture_failure", "synthetic event failure")

    monkeypatch.setattr(merge_module, "publish", fail)
    assert (
        relationships.call(
            "party.unmerge", merge_record_ref=merged.merge_record_ref
        ).state
        == "fixture_failure"
    )
    assert relationships.get(a.ref).canonical_ref == b.ref
    monkeypatch.setattr(merge_module, "publish", original)
    relationships.ok("party.unmerge", merge_record_ref=merged.merge_record_ref)
    with open_unit_of_work(relationships.ctx) as uow:
        event = (
            uow.connection.execute(
                select(outbox_event).where(
                    outbox_event.c.type == "relationships.party.unmerged"
                )
            )
            .mappings()
            .one()
        )
        assert event["data"] == {
            "survivor_ref": b.ref,
            "merged_ref": a.ref,
            "merge_record_ref": merged.merge_record_ref,
        }
    assert relationships.get(a.ref).canonical_ref == a.ref


def test_relationships_review_tool_returns_refs_usable_without_reconstruction(
    relationships: Relationships,
) -> None:
    from rheo_core.operations.tool_facade import call_registered_tool

    relationships.resolve(hints={"name": "Example Person"})
    relationships.resolve(hints={"name": "Example Person"})
    listed = call_registered_tool(
        relationships.ctx,
        "relationships_list_review",
        {},
        consumers=relationships.consumers,
        tools=relationships.surfaces.tools,
        registry=relationships.surfaces.operations,
    )
    assert listed.ok, listed
    row = listed.result.items[0]
    assert row["party"]["display_name"] == "Example Person"
    assert RecordRef.parse(row["party_ref"]).record_type == "party"
    accepted = call_registered_tool(
        relationships.ctx,
        "relationships_resolve_review",
        {"candidate_ref": row["candidate_ref"], "decision": "accept"},
        consumers=relationships.consumers,
        tools=relationships.surfaces.tools,
        registry=relationships.surfaces.operations,
    )
    assert accepted.ok, accepted


def test_relationships_import_bad_score_is_artifact_invalid(
    relationships: Relationships,
) -> None:
    from types import SimpleNamespace

    from pydantic_core import to_jsonable_python
    from rheo_relationships.export import export_records, import_records

    relationships.resolve(hints={"name": "Example Person"})
    relationships.resolve(hints={"name": "Example Person"})
    with open_unit_of_work(relationships.ctx) as uow:
        rows = to_jsonable_python(
            export_records(SimpleNamespace(connection=uow.connection))
        )
        candidate = next(
            row
            for row in rows
            if row["record_type"] == "relationships.review_candidate"
        )
        candidate["score"] = "not-a-number"
        with pytest.raises(OperationRefused, match="artifact_invalid"):
            import_records(uow, rows)
