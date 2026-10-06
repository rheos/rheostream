"""Independent Relationships module: no dependent domain, jobs, or UI."""

from importlib.metadata import version

from pydantic import BaseModel
from rheo_contracts import CONTRACT_VERSION, Role, SafetyClass, ToolDeclaration
from rheo_core.audit.core_sink import CORE_AUDIT_SINK
from rheo_core.modules.manifest import (
    EventDeclaration,
    ExportDeclaration,
    ModuleManifest,
    RecordType,
    StorageDeclaration,
)
from rheo_core.redaction.tiers import SensitivityTier

from rheo_relationships import contracts as c
from rheo_relationships.configuration import SETTINGS
from rheo_relationships.export import export_records, import_records
from rheo_relationships.lifecycle import authorize_delete, delete_owned
from rheo_relationships.operations import OPERATIONS
from rheo_relationships.resolvers import load_for_model, resolve_party


class PartyMerged(BaseModel):
    survivor_ref: str
    merged_ref: str
    merge_record_ref: str


TOOLS = tuple(
    ToolDeclaration(
        name=name,
        operation=operation,
        safety_class=safety,
        input_model=model,
        description=description + " Malformed inputs return input_invalid; "
        "unavailable records return not_found. "
        "Concurrent edits return record_stale. Merges may refuse "
        "party_is_alias, party_kind_mismatch, "
        "party_self_merge or affiliation_conflict; unmerge may refuse "
        "merge_already_unmerged, "
        "survivor_merged_onward or merge_item_changed; review may refuse "
        "candidate_not_open. "
        "Deletion requires approval_required confirmation and refuses alias targets.",
    )
    for name, operation, safety, model, description in (
        (
            "relationships_get_party",
            "relationships.party.get",
            SafetyClass.READ,
            c.PartyInput,
            "Read a party and its canonical identity.",
        ),
        (
            "relationships_find_party",
            "relationships.party.find",
            SafetyClass.READ,
            c.FindInput,
            "Find live workspace parties.",
        ),
        (
            "relationships_merge_parties",
            "relationships.party.merge",
            SafetyClass.MUTATE,
            c.MergeInput,
            "Merge two parties with explicit revisions and evidence; "
            "references remain stable.",
        ),
        (
            "relationships_unmerge",
            "relationships.party.unmerge",
            SafetyClass.MUTATE,
            c.UnmergeInput,
            "Restore the ownership recorded by a merge.",
        ),
        (
            "relationships_list_review",
            "relationships.review_candidate.list",
            SafetyClass.READ,
            c.ListReviewInput,
            "List identity and affiliation review candidates.",
        ),
        (
            "relationships_resolve_review",
            "relationships.review_candidate.resolve",
            SafetyClass.MUTATE,
            c.ReviewInput,
            "Accept or reject a review candidate.",
        ),
        (
            "relationships_delete_party",
            "core.record.delete",
            SafetyClass.DESTRUCTIVE,
            c.PartyInput,
            "Request approved erasure of a canonical party and its aliases.",
        ),
    )
)
MANIFEST = ModuleManifest(
    module_id="relationships",
    package_version=version("rheo-relationships"),
    core_contract_versions=(CONTRACT_VERSION,),
    dependencies=(),
    record_types=(
        RecordType(
            name="party",
            table="relationships.party",
            deletable=True,
            delete_roles=frozenset({Role.OWNER}),
            exportable=True,
            audience_field=None,
            authorize_delete=authorize_delete,
            delete_owned=delete_owned,
            load_for_model=load_for_model,
        ),
        *(
            RecordType(
                name=name,
                table=f"relationships.{name}",
                deletable=False,
                delete_roles=frozenset(),
                exportable=True,
                audience_field=None,
            )
            for name in ("merge_record", "review_candidate")
        ),
    ),
    storage=StorageDeclaration(
        schema_name="relationships",
        migrations_path="rheo_relationships.migrations",
        required_extensions=("pg_trgm",),
    ),
    configuration_schema=SETTINGS,
    operations=OPERATIONS,
    tools=TOOLS,
    events=tuple(
        EventDeclaration(type=event_type, schema_version=1, data=PartyMerged)
        for event_type in ("relationships.party.merged", "relationships.party.unmerged")
    ),
    subscriptions=(),
    jobs=(),
    schedules=(),
    resolvers=(("party", resolve_party),),
    deletion_participants=(),
    export=ExportDeclaration(
        format_version=1,
        schema_path="export.schema.json",
        exporter=export_records,
        importer=import_records,
    ),
    web=None,
    agent_guidance=None,
    secret_scopes=(),
    connector_bindings=(),
    health_checks=(),
    contract_tests="modules/relationships/tests",
    sensitivity={
        "party": {
            "kind": SensitivityTier.PUBLIC,
            "canonical_ref": SensitivityTier.PUBLIC,
            "display_name": SensitivityTier.INTERNAL,
            "contact_points": SensitivityTier.RESTRICTED,
        }
    },
    audit_sink=CORE_AUDIT_SINK,
)
