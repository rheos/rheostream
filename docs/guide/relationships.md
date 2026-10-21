# Relationships

## What it is

The shared record of people and organizations in a workspace. Leads needs to know who sent
an inquiry; an HR or dossier module would need the same people for different reasons.
Relationships owns exactly what they share and nothing more:

| Record | What it holds |
| --- | --- |
| Party | A stable id, whether it is a person or an organization, and a display name |
| Contact point | An email, phone, address, URL or external subject id, with its verification state and where it came from |
| Affiliation | A person's link to an organization, with a validity period, so representing two organizations in sequence is two rows and history is never rewritten |
| Merge record | Which two parties were merged, on what evidence, by whom and when, and everything the merge moved |
| Review candidate | A possible match the system will not decide on its own |

It owns no opportunity field, note, tag or score. It is not a CRM and not the login
identity of the people using rheoStream. A module that needs parties declares the
dependency and references them; referencing a party does not grant access to everything
about it.

## What you can do with it

- Find a party by name or by a contact value, and read its contacts and affiliations.
- Create a person or organization with up to 20 contact points in one step, an agent
  included (`relationships_create_contact`).
- Confirm a contact point, retire one, start or end an affiliation.
- Work the review queue, on its own screen or through the review tools: accept a possible match (which merges the two, or creates the
  affiliation) or reject it, after which the same pair is not proposed again for that
  reason.
- Merge two parties by hand, and unmerge them later.
- Delete a party, with an owner's per-action approval.

## How it works

### Only two kinds of evidence link automatically

When Leads processes a delivery it calls `relationships.party.resolve_or_create`, a
service-only operation no person's token can reach. An existing party is linked only on:

| Evidence | Supplied only when | Matches |
| --- | --- | --- |
| Authenticated subject | The connection declares it authenticates its subjects | An external subject id in the same connection's namespace |
| Source-verified email | The connection declares it verifies email | A source-verified email in the same connection's namespace |

Both are scoped to the connection that proved them. An email that a different connection
verified is a strong hint, not a link. A payload's own claim that its email is verified is
just a hint.

### Everything else becomes a review candidate

Without strong evidence a new party is created, and every weak match becomes an open
candidate: the same email at any verification, the same phone, a similar person name
(trigram similarity of at least 60%, up to 10 candidates), or a domain or organization
name that matches an existing organization. A new organization hint with no match creates
the organization and a dated affiliation, because creating is not matching.

### One authenticated identity, one live party

A partial unique index makes it impossible for two live parties to hold the same
authenticated identity in one namespace. Matching never crosses a workspace, because the
lookup runs against the caller's own database.

### Merging creates an alias

Merging sets the source party's `merged_into_id` to the target and moves its live contact
points and affiliations, writing one item per moved row with its previous owner. Parties
already pointing at the source are re-pointed to the target, so resolution is always
exactly one hop. Nothing outside the module changes: an observation or opportunity that
named the old party keeps that id, and resolves through the alias to the survivor. Leads
compares parties by their canonical reference, never by the stored string.

### Unmerge restores from the record

Because the merge record holds the pre-merge owner of every row it moved, unmerging puts
each one back, restores every re-pointed alias, and clears the source's pointer. Rows the
survivor gained after the merge stay with it. Every outside reference resolves to the
original party again by the same one-hop rule. Nested merges are undone in reverse order.

### Edits are revision-checked and serialized

Ordinary edits need the party's current revision, and merges need both. All identity
writes in a workspace share one lifecycle lock, so resolution, merge and erasure never
interleave. An affiliation collision or two conflicting strong identities refuse the whole
merge rather than discarding history.

### Deleting a party

Deleting a party needs owner approval and is refused for an alias (delete the survivor).
It removes the party, its aliases, contact points and affiliations, and clears the
free-text evidence on any merge record naming it, because a reviewer's note can restate a
person. Leads keeps the observations as unlinked evidence; Recallatron removes memories
about the party. Contact values sit in the `restricted` redaction tier and never reach a
model by default.

## Why it's built this way

A wrong automatic merge is a privacy event: one person's history shown as another's. So
the system links only on evidence a connection authenticated, inside that connection's
namespace, and puts every other match in front of a person.

Reversibility comes from how a merge is stored. Rewriting every reference on merge would
mean reaching into tables this module may not touch, and unmerge would have to rewrite
them all back. An alias costs one hop on every lookup and buys a merge that can be undone
with no other module involved.

## Where to look

- Specification: [relationships](../architecture/relationships.md),
  [module guide](../../modules/relationships/README.md)
- Code: [module](../../modules/relationships/src/rheo_relationships),
  [matching](../../modules/relationships/src/rheo_relationships/matching.py),
  [merge](../../modules/relationships/src/rheo_relationships/merge.py),
  [service](../../modules/relationships/src/rheo_relationships/service.py)
- Tests: [Relationships module](../../tests/postgres/test_relationships_module.py),
  [module tests](../../modules/relationships/tests)
