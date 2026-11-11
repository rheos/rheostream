# ADR-0023: Institutions are named by codenames; their own wording stays private
Status: accepted
Date: 2026-11-10

## Context

The ledger reads statements, bills and exports from the owner's own banks, card
issuers, utilities, payment services and exchanges. Each parser, issuer directory,
reference pattern, fixture and test once named its institution. In a public
repository those names say where the owner banks and who bills him, which is personal
information whether or not any account number appears beside them.

Some of that naming is only an identifier (a parser id, an issuer key, a sink name).
Some of it is matched against the documents themselves: a bill's title line, a CSV
column header, a word in a transaction description, an API host. And a document's
structure can point to its issuer as surely as a name does: which columns an export
has and in what order, the words it uses for kinds of row, the shape of its ids, and
the labels, markers and phrasing on its pages.

## Decision

Every institution is named by a neutral codename (`alder`, `olive`, `sumac` and so
on) in code, keys, stored values, fixtures, tests and records. Parser ids, issuer
directories, institution keys, sink and settings names, and the values the frozen
revisions check are those codenames.

Where code must match an institution's own wording, it builds the pattern through
`identities.real`, which swaps each stand-in for the wording in the owner's private
`$FINANCE_DATA_DIR/identities.toml` (mode 0600, never committed). Structure goes the
same way: `identities.layout` gives, per key, the column order of an export, an id or
mask shape, a line's pattern or a date format, and a parser's own default describes an
invented layout. So every parser reads an invented layout in public: invented column
names in an invented order, invented row types, id shapes, labels and markers, and the
synthetic fixtures and tests print that layout. Without the file each stand-in and
default stands for itself and the fixtures parse; an owner document is recognised only
where the owner's file names its wording and structure. The reference patterns' digest
includes the file's forms, so a change to it changes the parse inputs of the documents
it can affect.

The mapping from codename to institution, and the recorded patches to the three
vendored files (ADR-0022), are kept with the owner's private records.

## Consequences

The public tree names no institution the owner uses and copies no issuer's document
structure. His archive directories, manual files and stored rows still carry
the old names until the data move maps them
to the codenames, from the same private mapping. A parser that needs a new piece of
an institution's wording or structure adds a stand-in or a layout key and a line in
the private file, never the wording or the structure itself. Two codenames name a service rather than an institution: `rowan`,
the published exchange-rate source, and `teak`, the portfolio price service.
