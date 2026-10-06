"""Party identities and reversible merge history, owned by one workspace."""

from typing import Any

from sqlalchemy import (
    CheckConstraint,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Numeric,
    Table,
    Text,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY

metadata = MetaData(schema="relationships")


def id_column() -> Column[Any]:
    return Column("id", Uuid, primary_key=True)


def stamp(name: str) -> Column[Any]:
    return Column(name, DateTime(timezone=True), nullable=False)


def party_fk(name: str) -> Column[Any]:
    return Column(name, Uuid, ForeignKey("relationships.party.id"), nullable=False)


party = Table(
    "party",
    metadata,
    id_column(),
    Column("kind", Text, nullable=False),
    Column("display_name", Text, nullable=False),
    Column("merged_into_id", Uuid, ForeignKey("relationships.party.id")),
    Column("revision", Integer, nullable=False, server_default="1"),
    stamp("created_at"),
    stamp("updated_at"),
    CheckConstraint("kind in ('person','organization')", name="party_kind"),
    CheckConstraint(
        "merged_into_id IS NULL OR merged_into_id <> id", name="party_not_self_alias"
    ),
)
contact_point = Table(
    "contact_point",
    metadata,
    id_column(),
    party_fk("party_id"),
    Column("kind", Text, nullable=False),
    Column("value", Text, nullable=False),
    Column("normalized_value", Text, nullable=False),
    Column("namespace", Text),
    Column("verification", Text, nullable=False),
    Column("provenance_kind", Text, nullable=False),
    Column("provenance_ref", Text),
    stamp("created_at"),
    Column("retired_at", DateTime(timezone=True)),
    CheckConstraint(
        "kind in ('email','phone','address','url','external_subject')",
        name="contact_kind",
    ),
    CheckConstraint(
        "verification in ('unverified','source_verified','authenticated','confirmed')",
        name="contact_verification",
    ),
    CheckConstraint(
        "provenance_kind in ('observation','person','migration')",
        name="contact_provenance",
    ),
    CheckConstraint(
        "(verification = 'authenticated' AND kind = 'external_subject' AND"
        " namespace IS NOT NULL) OR (verification = 'source_verified' AND "
        "kind = 'email' AND namespace IS NOT NULL) OR (verification IN "
        "('unverified','confirmed') AND namespace IS NULL)",
        name="contact_evidence_class",
    ),
)
Index(
    "uq_contact_identity",
    contact_point.c.kind,
    contact_point.c.namespace,
    contact_point.c.normalized_value,
    unique=True,
    postgresql_where=text(
        "verification in ('source_verified','authenticated') AND retired_at IS NULL"
    ),
)
affiliation = Table(
    "affiliation",
    metadata,
    id_column(),
    party_fk("person_id"),
    party_fk("organization_id"),
    Column("role_label", Text),
    Column("valid_from", Date),
    Column("valid_to", Date),
    Column("provenance_ref", Text),
    stamp("created_at"),
    CheckConstraint(
        "valid_from IS NULL OR valid_to IS NULL OR valid_to >= valid_from",
        name="affiliation_dates",
    ),
)
Index(
    "uq_affiliation_period",
    affiliation.c.person_id,
    affiliation.c.organization_id,
    affiliation.c.valid_from,
    unique=True,
    postgresql_nulls_not_distinct=True,
)
merge_record = Table(
    "merge_record",
    metadata,
    id_column(),
    Column("survivor_id", Uuid, nullable=False),
    Column("merged_id", Uuid, nullable=False),
    Column("evidence", Text),
    Column("evidence_refs", ARRAY(Text), nullable=False),
    Column("actor_kind", Text, nullable=False),
    Column("actor_id", Uuid),
    stamp("merged_at"),
    Column("unmerged_at", DateTime(timezone=True)),
    Column("unmerged_by_kind", Text),
    Column("unmerged_by_id", Uuid),
    Column("unmerge_note", Text),
)
merge_record_item = Table(
    "merge_record_item",
    metadata,
    Column(
        "merge_record_id",
        Uuid,
        ForeignKey("relationships.merge_record.id"),
        primary_key=True,
    ),
    Column("kind", Text, primary_key=True),
    Column("item_id", Uuid, primary_key=True),
    Column("previous_owner_id", Uuid, nullable=False),
    CheckConstraint(
        "kind in "
        "('contact_point','affiliation_person','affiliation_organization','alias')",
        name="merge_item_kind",
    ),
)
review_candidate = Table(
    "review_candidate",
    metadata,
    id_column(),
    Column("kind", Text, nullable=False),
    Column("party_id", Uuid, nullable=False),
    Column("candidate_party_id", Uuid, nullable=False),
    Column("hint_kind", Text, nullable=False),
    Column("matched_contact_point_id", Uuid),
    Column("score", Numeric),
    Column("evidence_ref", Text),
    Column("occurred_at", DateTime(timezone=True), nullable=False),
    Column("state", Text, nullable=False),
    stamp("created_at"),
    Column("resolved_at", DateTime(timezone=True)),
    Column("resolved_by_kind", Text),
    Column("resolved_by_id", Uuid),
    Column("merge_record_id", Uuid, ForeignKey("relationships.merge_record.id")),
    Column("affiliation_id", Uuid),
    CheckConstraint("kind in ('same_party','affiliation')", name="candidate_kind"),
    CheckConstraint(
        "hint_kind in "
        "('name','email_unverified','email_verified_other_source','phone','domain','organization_name')",
        name="candidate_hint",
    ),
    CheckConstraint(
        "state in ('open','accepted','rejected','withdrawn')", name="candidate_state"
    ),
    CheckConstraint(
        "(kind = 'affiliation') = (hint_kind in ('domain','organization_name'))",
        name="candidate_hint_kind",
    ),
    CheckConstraint(
        "kind <> 'same_party' OR party_id < candidate_party_id", name="candidate_order"
    ),
)
Index(
    "uq_candidate_open",
    review_candidate.c.party_id,
    review_candidate.c.candidate_party_id,
    review_candidate.c.hint_kind,
    unique=True,
    postgresql_where=text("state = 'open'"),
)
