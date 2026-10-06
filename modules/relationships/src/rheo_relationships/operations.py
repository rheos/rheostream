"""Operation declarations are the permission and audit surface."""

from rheo_contracts import (
    AuditSpec,
    Idempotency,
    OperationDeclaration,
    Role,
    SafetyClass,
)
from rheo_core.operations.registry import Handler

from rheo_relationships import contracts as c
from rheo_relationships import service as s
from rheo_relationships.matching import resolve_or_create
from rheo_relationships.merge import list_review, merge, resolve_review, unmerge

MEMBERS = frozenset({Role.OWNER, Role.MEMBER})
READERS = MEMBERS | {Role.SERVICE}
OPERATIONS: tuple[tuple[OperationDeclaration, Handler], ...] = tuple(
    (
        OperationDeclaration(
            name=f"relationships.{name}",
            safety_class=SafetyClass.READ if read else SafetyClass.MUTATE,
            roles=READERS
            if name
            in ("party.get", "party.find", "party.aliases", "party.resolve_or_create")
            else MEMBERS,
            input_model=input_model,
            output=output,
            idempotency=Idempotency.NONE,
            audit=None if read else AuditSpec(subject_field=subject),
        ),
        handler,
    )
    for name, input_model, output, handler, read, subject in (
        ("party.get", c.PartyInput, c.PartyOutput, s.party_get, True, None),
        ("party.find", c.FindInput, c.ItemsOutput, s.party_find, True, None),
        ("party.aliases", c.PartyInput, c.AliasesOutput, s.party_aliases, True, None),
        ("party.create", c.CreateInput, c.PartyOutput, s.party_create, False, None),
        ("party.update", c.UpdateInput, c.PartyOutput, s.party_update, False, "ref"),
        (
            "contact_point.add",
            c.AddContactInput,
            c.ChangeOutput,
            s.contact_add,
            False,
            "party_ref",
        ),
        (
            "contact_point.confirm",
            c.ContactInput,
            c.ChangeOutput,
            s.contact_confirm,
            False,
            "party_ref",
        ),
        (
            "contact_point.retire",
            c.ContactInput,
            c.ChangeOutput,
            s.contact_retire,
            False,
            "party_ref",
        ),
        (
            "affiliation.add",
            c.AddAffiliationInput,
            c.ChangeOutput,
            s.affiliation_add,
            False,
            "person_ref",
        ),
        (
            "affiliation.end",
            c.EndAffiliationInput,
            c.ChangeOutput,
            s.affiliation_end,
            False,
            "person_ref",
        ),
        (
            "party.resolve_or_create",
            c.ResolveInput,
            c.ResolveOutput,
            resolve_or_create,
            False,
            None,
        ),
        ("party.merge", c.MergeInput, c.MergeOutput, merge, False, "target_ref"),
        ("party.unmerge", c.UnmergeInput, c.MergeOutput, unmerge, False, None),
        (
            "review_candidate.list",
            c.ListReviewInput,
            c.ItemsOutput,
            list_review,
            True,
            None,
        ),
        (
            "review_candidate.resolve",
            c.ReviewInput,
            c.ItemsOutput,
            resolve_review,
            False,
            None,
        ),
    )
)
