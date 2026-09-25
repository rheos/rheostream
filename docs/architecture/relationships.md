# Relationships: parties, contact points, affiliations, and merges

**Part of:** the [architecture specification](README.md). Designs against R4, R5's party clauses,
FR 40, FR 41, and criteria 50 to 54 and the party clauses of 65. Gives R4's "reversibility is a
property of the representation" its representation.
**Decision:** A16 (alias-based merge; references are never rewritten). See the
[decision list](README.md#architecture-decisions).
**Not built yet.** This is phase three's design: `modules/relationships` is still a placeholder
package, so none of the tables, operations, tools or events below exist.

## What the module owns

Exactly what R4 lists: a stable party identifier, whether the party is a person or an
organization, a display name, contact points with type, value, verification state, and
provenance, and affiliations between persons and organizations with a validity period. Plus the
two records that make the R4 rules operable: the merge record and the review candidate.

It owns nothing else. No opportunity field, no note, no tag, no score (criterion 54). A
`relationships` code path never names a Leads table; the schema check in
[storage](storage-and-workspaces.md#a3-one-postgres-schema-per-module) holds it to its own
schema. Leads reaches these records only through the operations below and through record
references ([identifiers](identifiers.md#record-references)).

## Entities

All in the `relationships` schema. Every table has `id uuid` (UUIDv7) unless noted.

| Table | Columns | Notes |
| --- | --- | --- |
| `party` | `kind text`, `display_name text`, `merged_into_id uuid null`, `revision integer`, `created_at`, `updated_at` | `kind` in `person`, `organization`. `merged_into_id` is null for a live party. A merge sets it to the survivor's id; nothing else ever writes it, and an unmerge sets it back to null. A party with it set is an **alias**: a real row that still resolves. |
| `contact_point` | `party_id`, `kind text`, `value text`, `normalized_value text`, `namespace text null`, `verification text`, `provenance_kind text`, `provenance_ref text null`, `created_at`, `retired_at null` | `kind` in `email`, `phone`, `address`, `url`, `external_subject`. `verification` in `unverified`, `source_verified`, `authenticated`, `confirmed` (confirmed by a person in the interface). `namespace` is the source connection's `source_namespace` for an `external_subject` and for a `source_verified` email; null for everything else. `provenance_kind` in `observation` (every intake transport, since imports and captures become observations), `person`, `migration`; `provenance_ref` is the record reference that supplied the value when there is one. **Partial unique index** on `(kind, namespace, normalized_value)` where `verification in (source_verified, authenticated)` and `retired_at is null`: an authenticated identity belongs to one live party. |
| `affiliation` | `person_id`, `organization_id`, `role_label text null`, `valid_from date null`, `valid_to date null`, `provenance_ref text null`, `created_at` | The validity period is the R4 requirement: a person representing two organizations in sequence is two rows with disjoint periods, and history is never rewritten. `valid_to` null means current. Unique on `(person_id, organization_id, valid_from)`. |
| `merge_record` | `survivor_id`, `merged_id`, `evidence text null`, `evidence_refs text[]`, `actor_kind`, `actor_id`, `merged_at`, `unmerged_at null`, `unmerged_by_kind null`, `unmerged_by_id null`, `unmerge_note text null` | Names the evidence, the actor, and the time (R4). One row per merge; an unmerge fills the `unmerged_*` columns and never deletes the row. `evidence` is the reviewer's free text and is nulled when either party is deleted ([deletion](#deletion-r5)); `evidence_refs` is provenance only, never a query predicate, and stays an array. |
| `merge_record_item` | `merge_record_id`, `kind text`, `item_id uuid`, `previous_owner_id uuid` | Primary key `(merge_record_id, kind, item_id)`. One row per row the merge moved, holding where it came from: `kind` in `contact_point` (the point's previous `party_id`), `affiliation_person`, `affiliation_organization` (the affiliation's previous person or organization), `alias` (a party whose `merged_into_id` was re-pointed; `previous_owner_id` is its previous target). This is the pre-merge state, in the record, which is what makes unmerge a restore rather than an undo. |
| `review_candidate` | `kind text`, `party_id`, `candidate_party_id`, `hint_kind text`, `matched_contact_point_id uuid null`, `score numeric null`, `evidence_ref text null`, `state text`, `created_at`, `resolved_at null`, `resolved_by_kind null`, `resolved_by_id null`, `merge_record_id uuid null`, `affiliation_id uuid null` | `kind` in `same_party` (party and candidate may be one person or one organization) and `affiliation` (party, a person, may belong to candidate, an organization). `hint_kind` in `name`, `email_unverified`, `email_verified_other_source`, `phone`, `domain`, `organization_name`. `state` in `open`, `accepted`, `rejected`, `withdrawn` (closed by the system because a party involved was merged or deleted). Unique on `(party_id, candidate_party_id, kind)` while `open`. Holds no contact value; `matched_contact_point_id` points at the value that matched. |

Nothing in this schema stores a reference to another module's record except `provenance_ref`,
`evidence_ref`, and `evidence_refs`, which are reference strings resolved through the core, never
foreign keys.

## The two classes of automatic-match evidence (R4)

| Class | Evidence | Matches against | Recorded as |
| --- | --- | --- | --- |
| Authenticated subject | `external_subject_id`, supplied only when the connection declares `subject_authenticated` | a live `contact_point` of kind `external_subject` with the same `namespace` and `normalized_value` | `contact_point(kind = external_subject, verification = authenticated, namespace = the connection)` |
| Source-verified email | `verified_email`, supplied only when the connection declares `email_verified` | a live `contact_point` of kind `email`, `verification = source_verified`, the same `namespace`, the same normalized value | `contact_point(kind = email, verification = source_verified, namespace = the connection)` |

Both are scoped to the connection's namespace, as R4 says: a subject id is meaningful only inside
the connection that authenticated it, and this specification applies the same rule to a verified
email. A verified email that another connection also verified is a strong hint and produces a
review candidate of `hint_kind = email_verified_other_source`, not a link. The narrower reading
costs one review per person who reaches the workspace through two verifying funnels; the wider
one would let any connection's verification stand in for any other's, and R4's rationale (a wrong
merge is a privacy event) argues for paying the review.

Matching never crosses a workspace: the lookup runs against the caller's own database
([storage](storage-and-workspaces.md#a1-the-per-workspace-unit-is-a-database)), so criterion 52
holds by construction and needs no filter.

## `relationships.party.resolve_or_create`

Called by Leads' delivery processing ([intake](intake-and-events.md#processing)) once per
receipt, and by nothing else in release one. Mutate class; roles `service`, `owner`, `member`.
Idempotency is natural: the consumer that calls it is deduplicated per receipt, so no receipt
reaches it twice.

Input (`ResolveEvidence`): `source_namespace`, `external_subject_id null`, `verified_email null`,
`hints` (`name`, `email`, `phone`, `organization_name`, `organization_domain`, each null when
absent), `evidence_ref` (the observation), `occurred_at`. Leads fills `external_subject_id` and
`verified_email` only from a connection that declares the matching class; a payload's own claim
about its email being verified is a hint.

Output: `party_ref`, `outcome` in `linked_subject`, `linked_email`, `created`, and
`review_candidate_refs`.

1. **Authenticated subject.** With `external_subject_id`, look up the authenticated contact
   point in this namespace. Found: the party is its owner, followed one hop through
   `merged_into_id`. Link; if `verified_email` is present and not yet recorded on the party,
   record it as `source_verified` with `provenance_ref = evidence_ref`. Return `linked_subject`.
2. **Source-verified email.** Otherwise, with `verified_email`, look up the source-verified email
   contact point in this namespace. Found: link the same way, recording the subject id if one was
   given. Return `linked_email`.
3. **Create.** Otherwise create a `person` party. `display_name` is the name hint, else the email
   hint, else the phone hint, else the literal `unnamed`. Record the subject id and verified
   email in their classes when present, and the email and phone hints as `unverified` contact
   points, all with `provenance_ref = evidence_ref`.
4. **Hints become candidates, never links** (criterion 51). For the new party, insert one open
   `review_candidate` per matching live party and hint:
   - `email_unverified`: any live party holding the same normalized email at any verification;
     `email_verified_other_source` when that holding is `source_verified` in another namespace.
   - `phone`: the same normalized phone.
   - `name`: person parties whose `display_name` trigram similarity (`pg_trgm`) is at least
     `relationships.match.name_similarity` (package default 0.6, workspace scope), at most
     `relationships.match.max_candidates` (default 10) by score.
   - `domain` and `organization_name`: an organization party whose `url` contact point's host
     equals the domain hint, or whose normalized display name equals the organization name hint,
     yields a candidate of `kind = affiliation` between the new person and that organization.
     When no organization matches and a hint was given, the operation creates the organization
     party (with the domain as a `url` contact point) and an affiliation from `occurred_at`,
     because creating is not matching; only a match to an existing organization is held for
     review.
5. Return `created` with the candidate references.

Two funnel steps by one person with a source-verified email from the same connection produce one
party (step 2 on the second delivery), two observations, and whatever the routing rules decide
(criterion 50). The same two steps with name, domain, and phone only produce two parties and open
candidates (criterion 51).

## Merge and unmerge (R4, criterion 53)

**The representation.** A merge never deletes, never rewrites a reference held by another
module, and never renumbers. `relationships.party.merge(source_ref, target_ref, evidence,
evidence_refs)`, mutate class (it has a reversal path, so it is not destructive under R3),
roles `owner`, `member`:

1. Refuse when source or target is an alias (`merged_into_id` set), when their kinds differ, or
   when they are the same party.
2. Write the `merge_record`.
3. Move the source's live contact points and affiliations to the target (`party_id`, or the
   person or organization column, rewritten), writing one `merge_record_item` per moved row with
   its previous owner. The authenticated-identity index cannot be violated by a move, because
   two live parties never hold the same authenticated identity in one namespace.
4. Re-point every party whose `merged_into_id` is the source to the target, writing an `alias`
   item per party, so the resolver still needs exactly one hop.
5. Set `source.merged_into_id = target`. Close the source's open review candidates as
   `withdrawn`.
6. Publish `relationships.party.merged` (references only).

**Resolution after a merge.** `observation.party_ref`, `opportunity_party.party_ref`, and every
other reference outside this module keep naming the id they were written with. The module's
resolver resolves an alias by following `merged_into_id` once and returns the survivor's display
with `state = live`; `relationships.party.get(ref)` returns `canonical_ref` (the survivor's
reference, or the party's own) and `alias_refs`. Leads compares parties by `canonical_ref`, never
by the stored string, which is how "the newest open opportunity that shares the party" in the
routing rules finds an opportunity whose party reference names an alias.

**Unmerge.** `relationships.party.unmerge(merge_record_ref, note)`, mutate class, roles
`owner`, `member`: refuse when the record is already unmerged, when the survivor has since been
merged into another party (unmerge in reverse order), or when either party no longer exists.
Otherwise restore from the record: every `contact_point` item returns to its previous owner,
every affiliation item returns to its previous person or organization, every `alias` item's
`merged_into_id` returns to its previous target, the source's
`merged_into_id` becomes null, and the record's `unmerged_*` columns are filled. Rows the
survivor gained after the merge are not in the record and stay where they are. Nothing outside
this module changes, because nothing outside it changed at merge time: every record's party
reference resolves to the pre-merge party again by the same one-hop rule, which is criterion 53's
"including every record's party reference".

## Resolving a review candidate, and how the link reaches Leads

`relationships.review_candidate.resolve(candidate_ref, decision, survivor, note)`, mutate class,
roles `owner`, `member`. `decision` in `accept`, `reject`.

- `accept` on `same_party`: run `merge` with the newer party as the source and the candidate as
  the target unless `survivor` names the other order; set `state = accepted` and
  `merge_record_id`.
- `accept` on `affiliation`: create the affiliation from the candidate's `evidence_ref`
  occurrence date; set `affiliation_id`.
- `reject`: `state = rejected`. The same pair is not proposed again for the same hint kind.

**The link reaches Leads through the alias and through nothing else.** The observation already
names the party that was created for it; after an accepted merge that party is an alias of the
existing one, and every Leads read that resolves the reference lands on the survivor. No
relationships event is consumed by Leads for this, and no Leads operation is called by the
reviewer. `relationships.party.merged` is published for read-model consumers (the memory module's
entity references) and carries nothing Leads needs. The reviewer's surface is the relationships
module's own review screen and the tools `relationships_list_review` and
`relationships_resolve_review`.

## Reads other modules use

| Operation | Class | Roles | Returns |
| --- | --- | --- | --- |
| `relationships.party.get(ref)` | read | owner, member, service | `kind`, `display_name`, `canonical_ref`, `alias_refs`, live contact points and affiliations, `revision` |
| `relationships.party.find(query, kind)` | read | owner, member, service | Parties by display name (trigram) or by a normalized contact value; live parties only, survivors in place of aliases |
| `relationships.party.aliases(ref)` | read | owner, member, service | Every reference that resolves to the same canonical party |
| `relationships.review_candidate.list(state)` | read | owner, member | Open candidates with the two parties' heads |

Contact point values are `restricted` in the
[redaction contract](runtime-and-mcp.md#the-redaction-contract); `display_name` is `internal`.

## Writes a person makes

`relationships.party.create`, `.update` (display name, kind cannot change),
`relationships.contact_point.add`, `.confirm`, `.retire`, `relationships.affiliation.add`,
`.end` (sets `valid_to`): all mutate class, roles `owner`, `member`, audited with the party as
subject. `retire` and `end` are the only removals short of R5 deletion: a contact point is never
hard-deleted by a person, so provenance stays inspectable.

## Deletion (R5)

The relationships module registers `party` as deletable. Its `delete(ref)` for the
[coordinator](deletion-export-migration.md#the-cascade):

- Refuses `party_is_alias`, naming the survivor, when the target has `merged_into_id` set: the
  alias and its survivor are one person, and the survivor is the record to delete.
- Otherwise removes the party row, its contact points, its affiliations, and every party whose
  `merged_into_id` names it (those alias rows hold nothing but a display name and the pointer,
  and they are the same person), and returns the full set of references it removed. The
  coordinator runs the participants and writes one deletion record per reference returned, so
  every alias id resolves to `state = deleted` afterward and Leads nulls the party reference on
  observations that named any of them.
- Merge records naming a removed party as survivor or merged keep their identifiers, timestamps,
  actor, and `evidence_refs`, and have their free-text `evidence` set null, because a reviewer's
  note about why two records were one person can restate that person; review candidates naming
  it close as `withdrawn`.

Deleting a party leaves observations in place as unlinked evidence; Leads' participant does that
([deletion](deletion-export-migration.md#the-cascade)). Contact-permission withdrawal is a Leads
operation on a party reference and is not deletion (FR 46).

## Export

Every table above is `exportable`; identifiers are kept on restore, so `provenance_ref`,
`evidence_ref`, and every Leads reference to a party hold across an export and restore
(criterion 67). The merge records and their items travel too, so an unmerge is possible after a
restore.

## Operations and roles

| Operation | Class | Roles | Tool |
| --- | --- | --- | --- |
| `relationships.party.resolve_or_create` | mutate | service, owner, member | none (Leads calls it) |
| `relationships.party.get`, `.find`, `.aliases` | read | owner, member, service | `relationships_get_party`, `relationships_find_party` |
| `relationships.party.create`, `.update` | mutate | owner, member | none in release one |
| `relationships.contact_point.add`, `.confirm`, `.retire` | mutate | owner, member | none in release one |
| `relationships.affiliation.add`, `.end` | mutate | owner, member | none in release one |
| `relationships.party.merge`, `.unmerge` | mutate | owner, member | `relationships_merge_parties`, `relationships_unmerge` |
| `relationships.review_candidate.list`, `.resolve` | read, mutate | owner, member | `relationships_list_review`, `relationships_resolve_review` |
| `core.record.delete` for `relationships.party` | destructive | owner | `relationships_delete_party` |

## A16. Alias-based merge, decided

**Decision.** A merged party stays a row with `merged_into_id` set; every reference outside the
module keeps its original id; the resolver follows exactly one hop; the merge record holds the
pre-merge ownership of every moved row; unmerge restores from the record.

**Why not rewrite references on merge.** Rewriting means every module holding a party reference
must be told, in one transaction, across schemas the relationships module may not touch (FR 11),
and unmerge must rewrite them all back from a log that R4 says may not be the thing reversibility
depends on. The alias costs one hop on resolution and one `aliases` read where Leads lists by
party; it buys criterion 53 by construction.

**Why not a chain.** Allowing `merged_into_id` to point at another alias would make resolution a
walk and unmerge order-sensitive in ways a reviewer cannot see. Re-pointing aliases at merge time
and recording the re-pointing keeps both operations one level deep.
