# spike — the vertical-slice spike module

**This whole directory is deleted at 0c0's branch cut.** It is not a product
feature and nothing should be built on top of it.

`modules/spike` exists so run 0v can put five ratified architecture contracts in
contact with running code four runs before the plan otherwise allows: the module
manifest and its entry-point loader, the operation registry and the dispatcher's
check order, the record reference and its resolver, the audit-sink seam, and the
one-transaction rule that says a handler may not commit.

It is an ordinary Python distribution and a `uv` workspace member. It publishes one
manifest object through the `rheo.modules` packaging entry-point group, under the
entry-point name `spike`, which is also its module id — the loader matches the
`RHEO_MODULES` allowlist against that name *before* importing anything, so a module
nobody named is never imported.

What it owns:

- schema `spike`, with two tables — `spike.note` (the record type) and
  `spike.audit_probe` (the minimum honest stand-in for `core.audit_record`, which
  is run 0c's and is not built here).
- three operations: `spike.note.add` (`mutate`), `spike.note.list` (`read`), and
  `spike.note.commit_early` (`mutate`, test-only scaffolding — its handler calls
  `uow.commit()` so the sealed handler view's refusal can be driven through the
  real dispatch path, and its `{owner}`-only role set is the only reachable driver
  for a `role_not_permitted` refusal in this run).
- one resolver for `spike.note`.
- one audit sink, which the **dispatcher** calls; this module never calls it.
- `rheo-spike`, a dev-only CLI with `install` and `session`, both refused under
  `RHEO_PROFILE=production`.

Install it into a workspace with:

```
rheo-spike install --workspace <workspace-id>
```

and load it into a process with `RHEO_MODULES=spike`. Neither is a production
path: module lifecycle is phase 2's, and `RHEO_MODULES`'s env read goes when the
real `modules.installed` settings key lands.
