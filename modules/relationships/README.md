# Relationships

Shared workspace party identity: people, organizations, contact points and affiliation
history. Install and enable this module through the standard module lifecycle. It has
no domain-module dependency, background worker or web UI.

The backend declares 15 operations and seven MCP tools. It links only authenticated
subjects or source-verified emails in the same source namespace. Other evidence creates
review candidates. Source verification belongs to the calling service's authenticated
transport; arbitrary contact writes can record only unverified contacts, which a person
can confirm. Confirmation never upgrades a contact into an automatic match key.

`party.resolve_or_create` accepts `source_namespace`, optional `external_subject_id`
and `verified_email`, `hints`, canonical `evidence_ref`, and timezone-aware `occurred_at`.
It returns `party_ref`, `outcome`, and `review_candidate_refs`. Supplying conflicting
strong identities refuses `identity_conflict`. Namespace values and identity values are
case-sensitive except email normalization. Email comparison trims and case-folds; phone
comparison removes formatting without guessing a country; domain hints compare hosts.

Names use `relationships.match.name_similarity_percent` (60 by default) and
`relationships.match.max_candidates` (10, at most 100). Candidates are per hint and use
canonical same-party pair order. Linked calls also discover hints. Names, retired contact
points and aliases never supply automatic identity evidence. A new unmatched organization
hint creates an organization and dated affiliation; an existing match requires review.
The candidate stores its occurrence timestamp so review needs no cross-module data read.
Contact provenance records the observation that introduced the value; later observations
retain their own party reference in their owning module.

Ordinary edits require the party's current `revision`; merge requires `source_revision`
and `target_revision`. Contacts can be confirmed or retired; affiliations can be ended,
never silently overwritten. All identity writers take the workspace lifecycle lock, so
resolution, merge and approved erasure serialize in one workspace. This favors correctness
and modest-volume intake over concurrent identity-write throughput.

Merge moves live contacts and affiliation ownership, records original ownership and
repoints aliases to preserve one-hop resolution. Unmerge restores only those moved rows;
new survivor contacts remain with the survivor. Undo nested merges in reverse order.
Affiliation key collisions refuse `affiliation_conflict` without discarding either history.
Withdrawn review tasks are not reopened by unmerge. Deleted children are not resurrected.

`relationships_delete_party` uses the core destructive approval coordinator, owner-only.
Alias targets refuse `party_is_alias`; request deletion of the canonical party. Approval
binds the party revision. Erasure removes the survivor, aliases, contacts and affiliations,
clears merge free text, and leaves content-free history. Core records each erased alias
and runs its participants in the same transaction. No direct deletion endpoint bypasses
approval. Live module activation is an operator decision, separate from shipping code.

The format-one export describes all six tables and preserves IDs and merge history.
Restore requires an empty module and validates one-hop aliases before its second pass.
This module-level roundtrip is tested; deployment-wide operational restore remains the
core export/restore workflow's responsibility.

Synthetic integration tests live in `tests/postgres/test_relationships_module.py`, using
the shared test harness and a workspace with this module alone. Pure contract tests live
in `modules/relationships/tests`. See the [architecture](../../docs/architecture/relationships.md).
