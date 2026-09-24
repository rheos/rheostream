# Phase two acceptance matrix

This is phase two's acceptance record so far: one row for each of criteria 24-31, 33 and
36. Read [README.md](README.md) first. Its per-row grammar, its three parser rules, and
above all its "what this is not" section govern every row here: the matrix records which
test demonstrates a criterion and which mutation proves that test bites, and it never
claims a criterion is demonstrated merely because a row exists.

**Ten rows, not fourteen.** Phase two's criteria run 24 to 37. Criteria 32, 34, 35 and 37
have no row because no run has closed them, and an absent row is the honest record of that.
Retrieval-strategy selection (31) was run 1a2's and its row was added at that run's
close-out. The predecessor migration (32) is run 1b's, and the naming gate,
fixture-provenance gate and re-asserted CI gates (34, 35, 37) are run 1a3's, because each
of them names this phase's ported interface surface, which does not exist yet. Do not add a
row for one of those on the strength of a test that happens to pass; the guard's per-file
completeness check is what keeps the set exact, and widening the set is a decision, not a
fix.

**One row is `partial`.** Criterion 24's interface half belongs to run 1a3 and nothing this
run can do closes it; the row carries a `Note:` saying which half is held back. Criterion 30
was seeded `partial` earlier in this run, because its second clause then described behaviour
the 2026-09-21 retention correction reverses; that amendment has since been ratified and
applied to `build-plan.md`, so the row now quotes the amended text, its evidence covers every
clause of it, and it is `complete`. Its second `Note:` records the promotion.

Every mutation below was applied to this working tree on 2026-09-21, run, watched go red,
and reverted, except criterion 31's, which run 1a2 captured the same way on 2026-09-23,
and criterion 25's, which run 1a3 re-captured on 2026-09-23 when its context moved;
each fenced block is the `git diff` that was captured while it was applied, never a
hand-typed hunk. Criteria 25, 26 and 33 take their demonstrators from run 1a0b's
close-out evidence, but 1a0b recorded test paths rather than node ids and captured no
applicable hunk, so the node ids below were re-derived against this tree and the mutations
are new captures. Each of those three rows says so in its own `Note:`.

---

### Criterion 24

**Text:** "The memory module is installed and enabled entirely through the registration contract: no core file changes to add it, verified by a test that enables it in a fresh workspace and asserts its records, tools, migrations, and interface contributions all appear, and that the workspace row now records the module's version and its schema version through the operation criterion 10 names." (`build-plan.md:287-291`)

**State:** partial

**Demonstrator:**
- `pytest:tests/postgres/test_module_lifecycle.py::test_install_then_enable_makes_workspace_status_report_recallatron`
- `pytest:tests/postgres/test_memory_lifecycle.py::test_loading_the_module_makes_its_memory_a_real_owned_deletable_type`
- `pytest:tests/postgres/test_memory_records.py::test_the_manifest_declares_both_ratified_events_and_no_subscription`

**Mutation:**
```diff
diff --git a/packages/core/src/rheo_core/operations/core_ops.py b/packages/core/src/rheo_core/operations/core_ops.py
index 127184f..fdaaa6d 100644
--- a/packages/core/src/rheo_core/operations/core_ops.py
+++ b/packages/core/src/rheo_core/operations/core_ops.py
@@ -283,7 +283,7 @@ def _workspace_status(
                 module_id=state.module_id,
                 package_version=state.package_version,
                 state=state.state,
-                schema_version=latest.get(state.module_id),
+                schema_version=None,
             )
             for state in states
         ],
```

**Cost:** `pytest:tests/postgres/test_module_lifecycle.py::test_install_then_enable_makes_workspace_status_report_recallatron` — first observed failure line: `E       AssertionError: assert [{'module_id'...rsion': None}] == [{'module_id'...ory_records'}]`, expanded by pytest to `{'module_id': 'recallatron', 'package_version': '0.1.0', 'state': 'enabled', 'schema_version': None} != {…, 'schema_version': '0002_memory_records'}`

**Performed by:** P7 (2026-09-21)

**Note:** the mutation targets the *reporting* side rather than the install side, because
that is the half of the criterion's sentence a silent regression would reach. Install and
enable failing is loud; a status response that stopped carrying the applied schema revision
would leave both operations succeeding and the workspace row's own claim unverifiable. The
second and third demonstrators cover what "its records … all appear" means for the parts
this run built: the memory record type reaches the core deletion registry as a real owned
deletable type, and both declared events arrive from the manifest with no subscription —
each purely through the registration contract, with no core file naming the module.

**Note:** this row is `partial` and the held-back half is the criterion's "interface
contributions". Run 1a3 owns the unified navigation, the theme and the memory browse,
search and entity screens, and nothing in this repository contributes a `WebSurface` for
Recallatron yet. Never promote this row to `complete` on the strength of the three
demonstrators above: they prove the registration contract carries records, tools,
migrations and versions, and they say nothing about interface contributions.

---

### Criterion 25

**Text:** "The module's manifest declares its owned record types, its storage destination, its migrations, its configuration schema, its provided tools with their safety classes, and its export format. A manifest missing any of these fails validation at install with a named missing field." (`build-plan.md:292-295`)

**State:** complete

**Demonstrator:**
- `pytest:tests/test_module_manifest.py::test_an_omitted_field_is_refused_by_name`
- `pytest:tests/test_module_manifest.py::test_the_model_carries_the_ratified_fields_plus_audit_sink`
- `pytest:tests/test_module_manifest.py::test_only_web_agent_guidance_and_audit_sink_are_optional`
- `pytest:tests/test_module_manifest.py::test_a_storage_schema_that_is_not_the_module_id_is_refused`

**Mutation:**
```diff
diff --git a/packages/core/src/rheo_core/modules/manifest.py b/packages/core/src/rheo_core/modules/manifest.py
index 0cd6fbe..1a1d12f 100644
--- a/packages/core/src/rheo_core/modules/manifest.py
+++ b/packages/core/src/rheo_core/modules/manifest.py
@@ -737,7 +737,7 @@ class ModuleManifest(BaseModel):
     schedules: tuple[Schedule, ...]
     resolvers: tuple[tuple[str, RecordResolver], ...]
     deletion_participants: tuple[DeletionParticipant, ...]
-    export: ExportDeclaration
+    export: ExportDeclaration | None = None
     web: WebContribution | None = None
     agent_guidance: str | None = None
     secret_scopes: tuple[str, ...]
```

**Cost:** `pytest:tests/test_module_manifest.py::test_an_omitted_field_is_refused_by_name` — first observed failure line: `E       Failed: DID NOT RAISE ValidationError`, on the `[export]` case, with the other four parametrised cases still passing

**Performed by:** P7 (2026-09-21), 1a3-P1 (2026-09-23)

**Note:** the first demonstrator is a parametrised family and is named as the family on
purpose, per README.md's second parser rule: the criterion lists six declarations and the
test covers five of them one at a time (`record_types`, `storage`, `configuration_schema`,
`tools`, `export`), so naming one bracketed case would understate what bites. The sixth,
migrations, is `StorageDeclaration.migrations_path` rather than a manifest field of its
own, so it is covered by `storage` above and by the required-field rule the third
demonstrator pins structurally through `field.is_required()`.
The mutation gives exactly one of the six a default, which is the precise way this
criterion fails in practice: a field that stops being required stops being declared, and
nothing else in the suite notices.

**Note:** run 1a0b closed this criterion on 2026-09-19 and named `tests/test_module_manifest.py`
as its evidence, but recorded no node id and no applicable hunk. The node ids above were
re-derived against this tree and the mutation is a fresh capture; nothing here is
transcribed from the archived run.

**Note:** run 1a3's first prompt widened `web` from `WebSurface | None` to
`WebContribution | None`, which moved the hunk's context and stopped it applying. The same
one-line mutation was applied again on 2026-09-23, watched fail with the same `[export]`
line recorded in `Cost`, reverted, and the block above is that capture.

---

### Criterion 26

**Text:** "The module writes only to its own storage. A test asserts that no memory-module code path holds a handle to another module's tables, that the core's storage API is the only route to a connection, and that a cross-module change is effected only through a public operation or event." (`build-plan.md:296-299`)

**State:** complete

**Demonstrator:**
- `pytest:tests/postgres/test_module_storage_ownership.py::test_recallatron_names_no_foreign_storage`
- `pytest:tests/postgres/test_module_storage_ownership.py::test_recallatron_constructs_no_raw_database_access`
- `pytest:tests/postgres/test_module_storage_ownership.py::test_recallatron_migration_creates_objects_only_inside_its_own_schema`

**Mutation:**
```diff
diff --git a/modules/recallatron/src/rheo_recallatron/storage/tables.py b/modules/recallatron/src/rheo_recallatron/storage/tables.py
index 1a51c11..51521ff 100644
--- a/modules/recallatron/src/rheo_recallatron/storage/tables.py
+++ b/modules/recallatron/src/rheo_recallatron/storage/tables.py
@@ -47,6 +47,7 @@ from sqlalchemy.dialects.postgresql import TSVECTOR, UUID
 from sqlalchemy.types import UserDefinedType
 
 MEMORY_SCHEMA: Final = "recallatron"
+MODULE_STATE_TABLE: Final = "core.module_state"
 
 MEMORY_KINDS: Final = ("note", "fact", "decision", "summary")
 """The four ratified kinds.
```

**Cost:** `pytest:tests/postgres/test_module_storage_ownership.py::test_recallatron_names_no_foreign_storage` — first observed failure line: `E       AssertionError: Recallatron names foreign storage: {'rheo_recallatron/storage/tables.py': ['qualified name core.module_state@50']}`

**Performed by:** P7 (2026-09-21)

**Note:** the three demonstrators split the criterion's three clauses. The first is the AST
scan for any name reaching another module's storage, and it is what the mutation reddens:
one added constant naming a core table is enough, which is the point — a foreign table name
is a handle whether or not anything queries through it yet. The second proves no raw
connection or DSN is constructed anywhere in the module's source, so the core's storage API
is the only route. The third is the live `pg_catalog` census: every object the Recallatron
migration creates, tables and non-table objects alike, lands inside the module's own schema.
Each of the first two carries its own positive control in-test, so a scan that stopped
matching would red rather than pass empty.

**Note:** run 1a0b named `tests/postgres/test_module_storage_ownership.py` as this
criterion's evidence while the file was still unwritten, then built it over that run's last
phase, up to the table-owned object census at `7daf9db`; this run extended it at `2725353`
when Recallatron's own schema arrived. So the demonstrator is carried and the node ids are
read off the current file rather than transcribed, and the mutation is a fresh capture.

---

### Criterion 27

**Text:** "A retrieval call by an actor without permission on a source record returns no content derived from that record, verified by a test that stores a memory under one permission scope and queries it under another." (`build-plan.md:300-302`)

**State:** complete

**Demonstrator:**
- `pytest:tests/postgres/test_memory_records.py::test_recall_excludes_a_row_whose_source_the_caller_cannot_read`
- `pytest:tests/postgres/test_memory_records.py::test_eligibility_denies_a_row_whose_ordinary_link_does_not_resolve`
- `pytest:tests/postgres/test_memory_records.py::test_eligibility_denies_a_member_audience_row_for_another_account`
- `pytest:tests/postgres/test_memory_records.py::test_read_history_mode_is_not_a_permission_override`

**Mutation:**
```diff
diff --git a/modules/recallatron/src/rheo_recallatron/eligibility.py b/modules/recallatron/src/rheo_recallatron/eligibility.py
index 534ae6b..1b9d9e4 100644
--- a/modules/recallatron/src/rheo_recallatron/eligibility.py
+++ b/modules/recallatron/src/rheo_recallatron/eligibility.py
@@ -695,7 +695,7 @@ def _resolved_link(
         return Denied(REFERENCE_SCAN_LIMIT)
     resolved = resolve_in(parsed, ctx, uow)
     if isinstance(resolved, Unavailable):
-        return Denied(NOT_FOUND)
+        return LinkHead(link.ref, link.relation, False, None)
     if not _readable(resolved):
         return Denied(NOT_FOUND)
     if not contact_permitted(ctx, uow, parsed, link.relation, request=request):
```

**Cost:** `pytest:tests/postgres/test_memory_records.py::test_recall_excludes_a_row_whose_source_the_caller_cannot_read` — first observed failure line: `E       AssertionError: assert ['apples one', 'apples two'] == ['apples one']`

**Performed by:** P7 (2026-09-21)

**Note:** the mutation changes one branch: a linked source this caller cannot resolve stops
denying the memory and becomes a content-free link head instead. Every other gate is left
standing, including the live-and-readable check on the line below, which is what makes the
red informative. The leaked row (`apples two`) is the one whose *source* the caller cannot
read, the readable row beside it is unaffected, and the leaked row's own title reaches the
caller — so the failure is content disclosure rather than a count or a timing signal.
The fourth demonstrator is listed because `include_invalidated` is the obvious way a
source permission check gets bypassed by accident, and it has a test saying history mode is
not a permission override; the mutation does not red it, which README.md's third parser
rule expressly allows.

---

### Criterion 28

**Text:** "A memory derived from two sources carries the intersection of their audiences, not the union. A test combines a broadly readable source with a restricted one and asserts the result is restricted." (`build-plan.md:303-305`)

**State:** complete

**Demonstrator:**
- `pytest:tests/postgres/test_memory_records.py::test_derive_meets_the_source_audiences_and_intersects_their_purposes`
- `pytest:tests/postgres/test_memory_records.py::test_derive_refuses_an_empty_meet_or_an_empty_intersection_without_writing`
- `pytest:tests/postgres/test_memory_records.py::test_derive_inherits_its_sources_actual_restrictions`

**Mutation:**
```diff
diff --git a/modules/recallatron/src/rheo_recallatron/writes.py b/modules/recallatron/src/rheo_recallatron/writes.py
index 1cce277..dbe3a75 100644
--- a/modules/recallatron/src/rheo_recallatron/writes.py
+++ b/modules/recallatron/src/rheo_recallatron/writes.py
@@ -151,9 +151,9 @@ def _meet(left: WriteAudience, right: WriteAudience) -> WriteAudience | None:
     § A4 calls ``audience_empty``: there is no third party both members belong to.
     """
     if left.kind == AUDIENCE_WORKSPACE:
-        return right
-    if right.kind == AUDIENCE_WORKSPACE:
         return left
+    if right.kind == AUDIENCE_WORKSPACE:
+        return right
     return left if left.id == right.id else None
 
 
```

**Cost:** `pytest:tests/postgres/test_memory_records.py::test_derive_meets_the_source_audiences_and_intersects_their_purposes` — first observed failure line: `E       AssertionError: assert 'workspace' == 'member'`

**Performed by:** P7 (2026-09-21)

**Note:** the mutation swaps the two branches of the audience meet, which turns it into
exactly the union the criterion's sentence forbids, and the observed failure line is that
substitution in the result: a memory derived from a workspace-readable source and a
member-private one comes back `workspace`. This is the criterion's own worked example
rather than an arbitrary break, which is why it is the one captured here rather than a
purpose-intersection break: the purposes are covered by the same demonstrator and by the
second, and a purpose bug would not widen who can read the derived row.

---

### Criterion 29

**Text:** "Deleting or superseding a source record invalidates its derived summaries and its embeddings in the same operation, verified by querying the retrieval index afterward." (`build-plan.md:306-308`)

**State:** complete

**Demonstrator:**
- `pytest:tests/postgres/test_memory_lifecycle.py::test_correction_removes_the_embeddings_of_every_row_it_touches`
- `pytest:tests/postgres/test_memory_lifecycle.py::test_correction_rewrites_in_place_and_invalidates_what_was_built_on_it`
- `pytest:tests/postgres/test_memory_lifecycle.py::test_supersession_marks_the_pre_existing_derivatives_and_bumps_each_revision`
- `pytest:tests/postgres/test_memory_lifecycle.py::test_an_approved_erasure_removes_the_closure_and_counts_only_removed_rows`

**Mutation:**
```diff
diff --git a/modules/recallatron/src/rheo_recallatron/lifecycle.py b/modules/recallatron/src/rheo_recallatron/lifecycle.py
index fa43b78..fe7e17b 100644
--- a/modules/recallatron/src/rheo_recallatron/lifecycle.py
+++ b/modules/recallatron/src/rheo_recallatron/lifecycle.py
@@ -282,7 +282,6 @@ def _mark_invalidated(
         revision=revision,
         superseded_by_id=successor,
     )
-    delete_memory_embeddings(uow.connection, memory_id)
     publish_memory_event(
         ctx,
         uow,
```

**Cost:** `pytest:tests/postgres/test_memory_lifecycle.py::test_correction_removes_the_embeddings_of_every_row_it_touches` — first observed failure line: `E       AssertionError: assert not True`, at the assertion that the *derivative* `b` no longer has an embedding; the target row `a`'s own assertion one line above still passes

**Performed by:** P7 (2026-09-21); re-performed by run 1a2 Prompt 3 (2026-09-23) after the
delete moved below the mark, with the same first failure line

**Why the hunk moved:** run 1a2 made `_mark_invalidated` mark the row *before* deleting its
embeddings, so the mark's row lock waits out an embed writer holding that row `FOR SHARE`
and the delete then removes the vector it committed. The mutation is the same line, removed
from its new place.

**Note:** the mutation is deliberately the narrow one. Correction drops the target's own
embeddings at its own call site and the closure drops each affected derivative's inside
`_mark_invalidated`, so deleting only the second leaves the target looking correctly
handled and the derivatives carrying vectors for text that no longer stands. That is the
shape this failure takes in production — a stale vector on a row nobody looked at — and it
is why the row names the derivative assertion in its cost line rather than the first one.
The remaining demonstrators cover the criterion's other three paths: the derived rows
marked on correction, the pre-existing derivatives marked on supersession, and the erasure
closure. Deletion by *user erasure* removes rows physically and takes their embeddings with
them by cascade, which is why its demonstrator is a closure-membership test rather than an
index query.

---

### Criterion 30

**Text:** "Every workspace has explicit memory retention settings rather than an inherited posture: both are written as rows when the module is enabled, so what a workspace does about age is always a stored, readable choice. The package default for the age-expiry gate is off, so nothing is deleted for age alone unless a workspace states that it wants that, and the retention window is a bounded positive number of days, read only while the gate is on. A missing or out-of-range window is refused rather than replaced by a default, and the window carries no indefinite sentinel value: unbounded retention is the gate's off state." (`build-plan.md:309-318`)

**State:** complete

**Demonstrator:**
- `pytest:tests/postgres/test_memory_retention.py::test_enabling_the_module_writes_both_retention_rows_and_a_daily_schedule`
- `pytest:tests/postgres/test_memory_retention.py::test_a_gate_off_sweep_deletes_nothing_and_finishes_an_ordinary_success`
- `pytest:tests/postgres/test_memory_retention.py::test_turning_the_gate_on_needs_one_settings_write_and_no_new_schedule`
- `pytest:tests/postgres/test_memory_retention.py::test_a_retention_row_outside_the_declared_range_expires_nothing`
- `pytest:tests/postgres/test_memory_retention.py::test_the_boundary_instant_is_retained_and_anything_older_is_not`

**Mutation:**
```diff
diff --git a/packages/core/src/rheo_core/modules/operations.py b/packages/core/src/rheo_core/modules/operations.py
index 856698f..ccb1adb 100644
--- a/packages/core/src/rheo_core/modules/operations.py
+++ b/packages/core/src/rheo_core/modules/operations.py
@@ -608,7 +608,7 @@ def _write_enable_rows(
     and enabling one module must not write another's.
     """
     for spec in manifest.configuration_schema:
-        if not spec.explicit_per_workspace:
+        if not spec.explicit_per_workspace or spec.key.endswith(".retention.days"):
             continue
         insert_workspace_setting_if_absent(
             uow.connection,
```

**Cost:** `pytest:tests/postgres/test_memory_retention.py::test_enabling_the_module_writes_both_retention_rows_and_a_daily_schedule` — first observed failure line: `E       ValueError: invalid literal for int() with base 10: 'None'`, raised reading back `recallatron.retention.days`, which enable no longer wrote

**Performed by:** P7 (2026-09-21)

**Note:** the five demonstrators split the amended criterion's clauses. Enabling Recallatron
writes both retention rows explicitly — `recallatron.retention.expire_by_age` stored as
`false` and a positive bounded `recallatron.retention.days` — so no workspace is left with
retention implied by a package default nobody chose. The second demonstrator is the
off-by-default clause itself: three consecutive daily sweeps against a workspace that
configured nothing delete nothing, select no root, write no deletion record and finish an
ordinary success rather than a failure. The third is that turning the gate on is one
settings write against a schedule that already exists; the fourth is that a window row
outside the declared range expires nothing rather than falling back to a default; the fifth
is the boundary instant, where equality is retained and anything older is not. The mutation
drops exactly one of the two rows from enable and the read fails closed rather than
substituting a default, which is the property the first clause is worth having.

**Note:** this row was seeded `partial` earlier in this run and is promoted to `complete`
here. The held-back half was the criterion's old second clause, "a workspace created with no
explicit setting receives a bounded default rather than indefinite retention" — the rule
Robin's 2026-09-21 correction reverses: age-based expiry is opt-in, a workspace that turns
nothing on keeps its memories, and member-audience memories stay outside the mechanism in
both gate states. That correction was ratified at this run's checkpoint 16 and the public
amendment landed with this prompt: `build-plan.md`'s criterion 30, its FR 29 trace row, and
the recorded change of direction in `docs/architecture/memory.md` § Retention and decision
ledger entry 13. The `Text` above is the amended criterion, quoted from the file, and every
clause of it has a demonstrator, which is what the earlier `Note:` said promotion required.

---

### Criterion 31

**Text:** "The retrieval strategy is selected through the adapter, and a test runs the module's full behavioural suite against both a dense-only and a lexical-only configuration, both passing." (`build-plan.md:319-321`)

**State:** complete

**Demonstrator:**
- `ci:python / Criterion 31 (lexical and dense passes)`

**Mutation:**
```diff
diff --git a/modules/recallatron/src/rheo_recallatron/retrieval/dispatch.py b/modules/recallatron/src/rheo_recallatron/retrieval/dispatch.py
index f7fc8f0..2b65c0f 100644
--- a/modules/recallatron/src/rheo_recallatron/retrieval/dispatch.py
+++ b/modules/recallatron/src/rheo_recallatron/retrieval/dispatch.py
@@ -81,4 +81,4 @@ def resolve_strategy(ctx: WorkspaceContext, uow: UnitOfWork) -> RetrievalStrateg
             uow, workspace_id=ctx.workspace_id
         ).workspace_overrides(ctx.workspace_id)
     )
-    return STRATEGY_REGISTRY[name]
+    return STRATEGY_REGISTRY[str(RETRIEVAL_STRATEGY_SPEC.default)]
```

**Cost:** `ci:python / Criterion 31 (lexical and dense passes)` — first observed failure line: `E       AssertionError: assert 'hybrid' == 'lexical'`, at `tests/postgres/test_memory_records.py:1073` in `test_the_pass_recalls_with_the_strategy_its_environment_names`, the lexical pass's first red of nine; `make` stopped there, before the dense pass

**Performed by:** run 1a2 Prompt 8 (2026-09-23)

**Note:** the demonstrator is the workflow step that runs `make criterion-31`, not a node id,
because the two passes are one file run twice under two environments, `lexical` and then
`dense`, and a node id cannot say which. The mutation breaks the adapter's selection step and
nothing else: `resolve_strategy` still reads the workspace's `recallatron.retrieval.strategy`
row, then dispatches the package default, `hybrid`, whatever the row says. Every recall still
answers and nothing raises. The first red is the pass sentinel,
`test_the_pass_recalls_with_the_strategy_its_environment_names`, the direct guard for this
failure: it checks that each pass's recall reports the strategy the pass named, so an
override that silently did nothing cannot leave the dense pass running the default while
`make criterion-31` goes green twice on one code path. It is not the only test this
particular mutation reddens. Eight other cases in the lexical pass also failed, seven test
functions with one of them parametrised twice, each pinning its workspace's strategy or
reading it back and asserting on what answered: nine failures of 79. The dense pass's own
command, run by hand against the same mutation, failed at the same sentinel with
`assert 'hybrid' == 'dense'`.

**Note:** what the dense claim is (spec § Technical Risks 8). The module's behavioural suite,
`tests/postgres/test_memory_records.py`, passes under `dense` at the shipped floor (30, cosine
0.30) against the real model, fastembed's MiniLM-L6-v2 through the `local` provider, and the
pass refuses any other provider. A test that pins its own workspace's strategy runs that
strategy in both passes; the rest run under the pass's. The dense pass is a harness claim with
two stated limits: it selects the provider by rebinding
`embedding_registry.configured_provider_name`, not through the
`recallatron.embedding.provider` setting a real deployment cannot set yet (issue #108), and
it fills every live memory synchronously with the rebuild's own fill step after each commit
point, so it proves the steady state and not a memory written moments before its embed job
runs. One assertion holds only when the
answer is lexical-only: the exact list `["apples"]` in
`test_recall_returns_eligible_rows_marked_with_the_resolved_strategy`
(`test_memory_records.py:1513`). That form encodes lexical `@@` semantics. On the shipped
provider `pears / a note about pears` scores cosine 0.471 against `apples`, clears the floor,
and dense returns it, so under `dense` the test asserts instead that `apples` comes first, the
expired row is absent and every score clears the floor. The dense equivalent is proven beside
it, in `tests/postgres/test_memory_retrieval.py::test_dense_recall_of_apples_returns_the_nearest_rows_apples_then_pears`,
which asserts `["apples", "pears"]` and checks `pears`'s margin over the floor in the test. The
first dense pass (Prompt 6) reported no red among the other measured sites, and none is open.

---

### Criterion 33

**Text:** "Every phase-one path completes correctly in a workspace where the memory module was never installed and never enabled, verified by running the phase-one acceptance suite in such a workspace. Workspace-level disable is phase-seven work under D5, so release one proves the module boundary by absence rather than by disable." (`build-plan.md:328-333`)

**State:** complete

**Demonstrator:**
- `ci:absence-proof / Run make absence-proof`
- `pytest:tests/test_module_import_graph.py::test_core_contracts_and_apps_do_not_depend_on_recallatron`
- `pytest:tests/test_absence_proof.py::test_workflow_runs_the_absence_proof_beside_a_known_positive_control`
- `pytest:tests/test_absence_proof.py::test_compare_rejects_one_changed_outcome_and_accepts_the_identical_pair`
- `pytest:tests/test_absence_proof.py::test_summary_rejects_empty_selected_and_accepts_nonempty_selected`

**Mutation:**
```diff
diff --git a/packages/core/src/rheo_core/modules/loader.py b/packages/core/src/rheo_core/modules/loader.py
index 3de7d9f..46fe27e 100644
--- a/packages/core/src/rheo_core/modules/loader.py
+++ b/packages/core/src/rheo_core/modules/loader.py
@@ -102,6 +102,8 @@ from typing import Final
 from packaging.specifiers import SpecifierSet
 from rheo_contracts import CONTRACT_VERSION
 
+from rheo_recallatron.manifest import MODULE_ID as _MEMORY_MODULE_ID
+
 from rheo_core.audit.sink import install_sink
 from rheo_core.deletion.registry import (
     OWNED_DELETIONS,
```

**Cost:** `pytest:tests/test_module_import_graph.py::test_core_contracts_and_apps_do_not_depend_on_recallatron` — first observed failure line: `E       AssertionError: undeclared Recallatron dependencies: ['packages/core/src/rheo_core/modules/loader.py']`

**Performed by:** P7 (2026-09-21)

**Note:** the criterion has two halves and the first demonstrator is the one that answers it
directly: `make absence-proof` runs the phase-one matrix's own demonstrators twice, once
with the module on disk but absent from `modules.installed` (`--mode config`) and once with
it absent from the checkout entirely (`--mode checkout`), then compares the two outcome sets
row by row (`--mode compare`). It is a workflow step rather than a pytest node because
running the phase-one suite twice under two module configurations is not something one test
process can do to itself.

The captured mutation reddens the second demonstrator instead, and the choice is deliberate.
The criterion's own parenthetical scopes it to "the half requiring that the core carry no
dependency on a module added through the public extension points", and the import graph is
exactly that invariant: one import of the module from a core file is enough, added here to
the loader, which is the file most plausibly tempted to reach for a concrete module. A
mutation that broke `make absence-proof` instead would take a full CI-length run to observe
and would prove the weaker claim. The third demonstrator pins the wiring — that the job and
its step exist in the workflow at all, beside the ordinary `python / Pytest` job — and the
last two keep the comparison itself honest: one refuses a pair whose outcomes differ, the
other refuses a summary whose selected set is empty. Together they are what stops "both
runs agreed" from quietly meaning "both runs ran nothing".

---

### Criterion 36

**Text:** "Criterion 21 is re-asserted at the end of this phase against a workspace that has the memory module installed, enabled, and populated. The export carries the module's records through the export format its manifest declares (criterion 25), and after a restore into an empty deployment the restored workspace's composition, configuration versions, module schema versions, and memory records match the original by comparison." (`build-plan.md:345-350`)

**State:** complete

**Demonstrator:**
- `pytest:tests/postgres/test_memory_export_restore.py::test_a_populated_workspace_restores_equal_on_every_category_and_its_digest`
- `pytest:tests/postgres/test_memory_export_restore.py::test_retained_history_and_every_receipt_state_round_trip`
- `pytest:tests/postgres/test_memory_export_restore.py::test_no_embedding_survives_the_round_trip`
- `pytest:tests/postgres/test_memory_export_restore.py::test_a_failure_in_the_second_pass_imports_nothing_and_activates_nothing`

**Mutation:**
```diff
diff --git a/modules/recallatron/src/rheo_recallatron/export.py b/modules/recallatron/src/rheo_recallatron/export.py
index 59afa81..7f3003b 100644
--- a/modules/recallatron/src/rheo_recallatron/export.py
+++ b/modules/recallatron/src/rheo_recallatron/export.py
@@ -336,14 +336,6 @@ def export_memory_records(
     connection = snapshot.connection
     lines: list[Mapping[str, object]] = []
     lines.extend(_memory_line(row) for row in export_memory_rows(connection))
-    lines.extend(
-        {
-            _RECORD_KEY: PURPOSE_RECORD,
-            "memory_id": row.memory_id,
-            "purpose": row.purpose,
-        }
-        for row in export_memory_purpose_rows(connection)
-    )
     lines.extend(_entity_line(row) for row in export_memory_entity_rows(connection))
     lines.extend(
         {
```

**Cost:** `pytest:tests/postgres/test_memory_export_restore.py::test_a_populated_workspace_restores_equal_on_every_category_and_its_digest` — first observed failure line: `E       AssertionError: Refusal(state='workspace_unavailable', detail='restoring')`, the restore job having failed with `recallatron_artifact_invalid: memories carry no purpose: [...]` in the captured log

**Performed by:** P7 (2026-09-21)

**Note:** the test drops the source workspace's database and rebuilds it from the archive
alone, then compares every digest category — composition, settings, approvals, operations,
audit, deletions and the module's own — plus the deletion ledger row by row. Dropping one
exported category from the writer is therefore caught on the way *in* rather than by a
digest mismatch on the way out: the importer's own validation refuses an artifact whose
memories carry no purpose, so the workspace never comes back and the restored context is a
refusal. That is a sharper red than a digest diff and it exercises the same seam.

**Note:** criterion 36 re-asserts criterion 21, whose own row is in
[phase-1-matrix.md](phase-1-matrix.md). This row is about that re-assertion with the memory
module installed, enabled and populated; it does not restate or replace criterion 21's
evidence, and the two matrices deliberately share no criterion number.
