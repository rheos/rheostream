"""Frozen initial Relationships DDL."""

from alembic import op

revision = "0001_relationships"
down_revision = None
branch_labels = None
depends_on = None
STATEMENTS = (
    """CREATE SCHEMA IF NOT EXISTS relationships""",
    """CREATE TABLE relationships.merge_record (
id UUID NOT NULL,
survivor_id UUID NOT NULL,
merged_id UUID NOT NULL,
evidence TEXT,
evidence_refs TEXT[] NOT NULL,
actor_kind TEXT NOT NULL,
actor_id UUID,
merged_at TIMESTAMP WITH TIME ZONE NOT NULL,
unmerged_at TIMESTAMP WITH TIME ZONE,
unmerged_by_kind TEXT,
unmerged_by_id UUID,
unmerge_note TEXT,
PRIMARY KEY (id)
)""",
    """CREATE TABLE relationships.party (
id UUID NOT NULL,
kind TEXT NOT NULL,
display_name TEXT NOT NULL,
merged_into_id UUID,
revision INTEGER DEFAULT '1' NOT NULL,
created_at TIMESTAMP WITH TIME ZONE NOT NULL,
updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
PRIMARY KEY (id),
CONSTRAINT party_kind CHECK (kind in ('person','organization')),
CONSTRAINT party_not_self_alias CHECK (merged_into_id IS NULL OR merged_into_id <>
id),
FOREIGN KEY(merged_into_id) REFERENCES relationships.party (id)
)""",
    """CREATE TABLE relationships.affiliation (
id UUID NOT NULL,
person_id UUID NOT NULL,
organization_id UUID NOT NULL,
role_label TEXT,
valid_from DATE,
valid_to DATE,
provenance_ref TEXT,
created_at TIMESTAMP WITH TIME ZONE NOT NULL,
PRIMARY KEY (id),
CONSTRAINT affiliation_dates CHECK (valid_from IS NULL OR valid_to IS NULL OR
valid_to >= valid_from),
FOREIGN KEY(person_id) REFERENCES relationships.party (id),
FOREIGN KEY(organization_id) REFERENCES relationships.party (id)
)""",
    """CREATE UNIQUE INDEX uq_affiliation_period ON relationships.affiliation
(person_id,
organization_id, valid_from) NULLS NOT DISTINCT""",
    """CREATE TABLE relationships.contact_point (
id UUID NOT NULL,
party_id UUID NOT NULL,
kind TEXT NOT NULL,
value TEXT NOT NULL,
normalized_value TEXT NOT NULL,
namespace TEXT,
verification TEXT NOT NULL,
provenance_kind TEXT NOT NULL,
provenance_ref TEXT,
created_at TIMESTAMP WITH TIME ZONE NOT NULL,
retired_at TIMESTAMP WITH TIME ZONE,
PRIMARY KEY (id),
CONSTRAINT contact_kind CHECK (kind in
('email','phone','address','url','external_subject')),
CONSTRAINT contact_verification CHECK (verification in
('unverified','source_verified','authenticated','confirmed')),
CONSTRAINT contact_provenance CHECK (provenance_kind in
('observation','person','migration')),
CONSTRAINT contact_evidence_class CHECK ((verification = 'authenticated' AND kind
= 'external_subject' AND namespace IS NOT NULL) OR (verification =
'source_verified' AND kind = 'email' AND namespace IS NOT NULL) OR (verification
IN ('unverified','confirmed') AND namespace IS NULL)),
FOREIGN KEY(party_id) REFERENCES relationships.party (id)
)""",
    """CREATE UNIQUE INDEX uq_contact_identity ON relationships.contact_point (kind,
namespace, normalized_value) WHERE verification in
('source_verified','authenticated') AND retired_at IS NULL""",
    """CREATE TABLE relationships.merge_record_item (
merge_record_id UUID NOT NULL,
kind TEXT NOT NULL,
item_id UUID NOT NULL,
previous_owner_id UUID NOT NULL,
PRIMARY KEY (merge_record_id, kind, item_id),
CONSTRAINT merge_item_kind CHECK (kind in
('contact_point','affiliation_person','affiliation_organization','alias')),
FOREIGN KEY(merge_record_id) REFERENCES relationships.merge_record (id)
)""",
    """CREATE TABLE relationships.review_candidate (
id UUID NOT NULL,
kind TEXT NOT NULL,
party_id UUID NOT NULL,
candidate_party_id UUID NOT NULL,
hint_kind TEXT NOT NULL,
matched_contact_point_id UUID,
score NUMERIC,
evidence_ref TEXT,
occurred_at TIMESTAMP WITH TIME ZONE NOT NULL,
state TEXT NOT NULL,
created_at TIMESTAMP WITH TIME ZONE NOT NULL,
resolved_at TIMESTAMP WITH TIME ZONE,
resolved_by_kind TEXT,
resolved_by_id UUID,
merge_record_id UUID,
affiliation_id UUID,
PRIMARY KEY (id),
CONSTRAINT candidate_kind CHECK (kind in ('same_party','affiliation')),
CONSTRAINT candidate_hint CHECK (hint_kind in
('name','email_unverified','email_verified_other_source','phone','domain','organization_name')),
CONSTRAINT candidate_state CHECK (state in
('open','accepted','rejected','withdrawn')),
CONSTRAINT candidate_hint_kind CHECK ((kind = 'affiliation') = (hint_kind in
('domain','organization_name'))),
CONSTRAINT candidate_order CHECK (kind <> 'same_party' OR party_id <
candidate_party_id),
FOREIGN KEY(merge_record_id) REFERENCES relationships.merge_record (id)
)""",
    """CREATE UNIQUE INDEX uq_candidate_open ON relationships.review_candidate
(party_id,
candidate_party_id, hint_kind) WHERE state = 'open'""",
)


def upgrade() -> None:
    for statement in STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    raise RuntimeError("forward-only module migrations")
