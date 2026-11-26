# ADR-0022: Vendor the four docextract modules, pinned by their bytes
Status: accepted
Date: 2026-11-10

## Context

ADR-0001 imported `docextract` as a git dependency pinned to one commit of the owner's
private `doc-extraction-api` repository. Inside rheoStream that dependency does not
work: the repository is public, its lock must resolve without a private credential,
and its image builds from the public checkout. The read credential that once let the
standalone release fetch the package has been revoked.

The ledger uses four of that package's modules: `alder` (the bank statement rules),
`reference` (the mobile bill rules), `pdftext` (the `pdftotext -layout` text
extraction) and `schema` (`to_money`). `validate` served only the local parity check,
which did not move into rheoStream; `extractor`, `api` and `monitor` were never used.

## Decision

Copy the four files from their Git blobs at commit
`0ad7253f6586eea62cabc32d7d69823e3987e67b` into `rheo_finance.parsers.vendored`, under
rheoStream's AGPL-3.0. `pdftext` is unchanged. The other three carry recorded patches,
kept with the owner's private records. The first put the module's codenames where
upstream named institutions (ADR-0023). Later ones took dates and figures out of the
comments and route every label, marker and line shape through `identities.real` and
`identities.layout` with invented public defaults; a later one replaced the bill labels,
the usage-line shape and the overdraft marker with invented ones, and the latest
reorders the keys of the transaction record the statement reader returns. With the owner's
identities file the rules read what upstream's read; in public they read the invented
layout. `tests/guards/test_vendored_bytes.py` pins each file's sha256 and blob id; a
further change is another recorded patch with a new pin and its reason.
`parsers.docextract_adapter` stays the only importer, and
`docextract_version()` returns the string the pinned install reported
(`0.1.0+<commit>`), so every stored input fingerprint keeps its value. Upstream's
tests and fixtures are not copied.

## Consequences

The rules still have one home inside this module: the vendored files, guarded by the
same boundary and second-copy tests that guarded the import. Upstream fixes no longer
arrive by moving a pin; each is a deliberate patch here. The lint and type checks skip
the vendored directory, so the adapter types everything that leaves it. ADR-0001's
decision to import rather than copy is replaced; its reasoning about one home for
every rule still holds.
