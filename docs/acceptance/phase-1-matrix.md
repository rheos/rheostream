# Phase one acceptance matrix

This is the phase-one acceptance matrix: one row for each of criteria 1 through 23 and
criterion 69, twenty-four in all, written across the whole life of run 0c3 rather than in
one pass. Rows arrive as the chunk that can honestly write them reaches them, so some rows
are opened `deferred` before the work behind them exists. Read [README.md](README.md) for
the per-row grammar, what the matrix does and does not claim, and the three-command loop
for reproducing any row's mutation yourself.

The eleven rows below (criteria 1-10 and 22) are the re-verification of what runs 0a and
0b built and claimed. Every one of their mutations was applied to a real working tree, run,
observed red, and reverted on 2026-09-16.

The eight rows for criteria 11-17 and 69 come after criterion 22 rather than before it,
because rows are written in the order the chunks reach them and nothing in this document
depends on sequence. They are the settled ones: five built by runs 0c1 and 0c2 and by the
settings work, one half built, and two that cannot be built against this tree at all. Each of
their five `complete` rows carries a mutation freshly applied to this working tree on
2026-09-16, watched go red, and reverted; none is transcribed from the archived run's own
demonstration. Criteria 15, 16 and 17 name run 0c4 in their own row text rather than here,
because a reader who lands on one row is not reading this paragraph.

---

### Criterion 1

**Text:** "A clone of the public repository, with no private configuration and no credentials, builds and starts the synthetic demo, and the demo completes without reading any file outside the checkout and the synthetic fixture set." (`build-plan.md:84-86`)

**State:** complete

**Demonstrator:**
- `ci:docker / Build the core/worker image`

**Mutation:**
```diff
diff --git a/.dockerignore b/.dockerignore
index 3e8f0a7..628f061 100644
--- a/.dockerignore
+++ b/.dockerignore
@@ -18,7 +18,6 @@
 !deploy/
 !deploy/**
 !pyproject.toml
-!uv.lock
 !.python-version
 
 # Re-exclude private artifacts and local dependencies within allowed source trees.
```

**Cost:** `ci:docker / Build the core/worker image` — first observed failure line: `ERROR: failed to build: failed to solve: failed to compute cache key: failed to calculate checksum of ref <per-build ref id>: "/uv.lock": not found` (the ref id is regenerated on every build; everything else is stable). The build is green on the unmutated tree, 39s wall clock locally.

**Performed by:** C1 (2026-09-16)

**Note:** the mutation targets `.dockerignore` rather than the `Dockerfile` on purpose. The
build context is deny-everything (`**`) plus a named allowlist, and the `Dockerfile`'s own
comment at `:30-32` says the three re-included root manifests are what make a clean public
clone buildable. Removing one proves the claim from the side that could break silently: a
private file re-entering the context would not fail a build, but the allowlist is what keeps
it out, and this shows the allowlist is load-bearing rather than decorative.

**Note:** the criterion's demo clause ("starts the synthetic demo, and the demo completes
without reading any file outside the checkout and the synthetic fixture set") has **no
demonstrator expressible in this grammar.** `make demo` exists and does exactly that work,
but no job in `.github/workflows/repository-checks.yml` runs it and no pytest node exercises
it; a search of `tests/` for `demo`, `Dockerfile`, `compose.yaml` and `docker build` finds
only docstring references. The build half is gated; the demo half is exercised by hand.
Recorded as an evidence gap rather than papered over.

---

### Criterion 2

**Text:** "Running the demo and normal startup writes no file inside the tracked source tree; `git status` is clean after a full run." (`build-plan.md:87-88`)

**State:** complete

**Demonstrator:**
- `pytest:tests/test_git_clean.py::test_startup_writes_nothing_new_to_tracked_tree`

**Mutation:**
```diff
diff --git a/apps/cli/src/rheo_app_cli/main.py b/apps/cli/src/rheo_app_cli/main.py
index 1ed88fe..ef7fe09 100644
--- a/apps/cli/src/rheo_app_cli/main.py
+++ b/apps/cli/src/rheo_app_cli/main.py
@@ -82,6 +82,9 @@ def run_command(handler: Handler, args: argparse.Namespace) -> int:
 
 
 def main(argv: Sequence[str] | None = None) -> int:
+    from pathlib import Path
+
+    Path(__file__).with_name("startup.trace").write_text("started\n")
     parser = build_parser()
     arguments = list(sys.argv[1:] if argv is None else argv)
     try:
```

**Cost:** `pytest:tests/test_git_clean.py::test_startup_writes_nothing_new_to_tracked_tree` — first observed failure line: `E       AssertionError: startup introduced new working-tree changes: ['?? apps/cli/src/rheo_app_cli/startup.trace']`

**Performed by:** C1 (2026-09-16)

**Note:** the write goes in `main()` rather than at module import, because the test captures
`git status --porcelain` around the exercise, not around the import. It also has to use an
extension the repository ignore rules do not match: a `.log` marker is gitignored and would
leave the test green, which is itself a small demonstration that this test is scoped to the
*tracked* tree exactly as the criterion words it. Delete the stray `startup.trace` before
re-running: the file is untracked, so a second run finds it in the "before" capture and
passes.

---

### Criterion 3

**Text:** "With the checkout-local fallback directory explicitly selected, every configuration file, upload, database, database sidecar, export, and log it produces is matched by the repository ignore rules, verified by a test that creates one of each and asserts it is ignored." (`build-plan.md:89-92`)

**State:** complete

**Demonstrator:**
- `pytest:tests/test_data_root.py::test_rheo_local_is_accepted_only_when_named_explicitly`
- `pytest:tests/test_ignored_artifacts.py::test_checkout_local_artifacts_are_ignored`

**Mutation:**
```diff
diff --git a/packages/core/src/rheo_core/storage/data_root.py b/packages/core/src/rheo_core/storage/data_root.py
index 718dc6f..f7b1584 100644
--- a/packages/core/src/rheo_core/storage/data_root.py
+++ b/packages/core/src/rheo_core/storage/data_root.py
@@ -33,7 +33,7 @@ DATA_ROOT_VARIABLE: Final = "RHEO_DATA_ROOT"
 CONTAINER_VARIABLE: Final = "RHEO_IN_CONTAINER"
 CONTAINER_DATA_ROOT: Final = Path("/var/lib/rheo-stream")
 APP_DIR_NAME: Final = "rheo-stream"
-LOCAL_OPT_IN: Final = ".rheo-local"
+LOCAL_OPT_IN: Final = ".rheo-data"
 _CONTAINER_TRUE: Final = frozenset({"1", "true"})
 
 DATA_ROOT_IN_CHECKOUT: Final = "data_root_in_checkout"
```

**Cost:**
- `pytest:tests/test_data_root.py::test_rheo_local_is_accepted_only_when_named_explicitly` — first observed failure line: `E               rheo_core.storage.data_root.DataRootRefusal: data_root_in_checkout: data root <pytest tmp>/checkout/.rheo-local is inside the source checkout <pytest tmp>/checkout; only <pytest tmp>/checkout/.rheo-data is allowed, and only when RHEO_DATA_ROOT names it explicitly` (the tmp prefix is pytest's per-run directory).
- `pytest:tests/test_ignored_artifacts.py::test_checkout_local_artifacts_are_ignored` — first observed failure line: `E       AssertionError: checkout-local artifacts not git-ignored: ['.rheo-data/workspaces/<token>/profile.json', '.rheo-data/uploads/<token>/evidence.pdf', '.rheo-data/transcripts/<token>/session.jsonl', '.rheo-data/exports/<token>/workspace.csv', '.rheo-data/backups/<token>.sql', '.rheo-data/memory/<token>/index.bin', '.rheo-data/config/<token>/deployment.toml', '.rheo-data/secrets/<token>/cluster/primary-dsn']` (`<token>` is the fixture's per-run uuid). Both demonstrators observed red together under one application of the hunk: `2 failed`.

**Performed by:** C1 (2026-09-16), C5 (2026-09-16)

**Note:** the criterion has two halves. The half this hunk breaks is "the checkout-local
fallback directory": move the directory the deployment writes into and the ignore rules no
longer match anything it produces, which is the criterion's failure in its most direct form.

**Note on the second demonstrator, which could not fail for the reason it claimed until
C5's pass.** As C1 captured this row, `test_checkout_local_artifacts_are_ignored` stayed green
under this hunk. The first reason to reach for was that its guarded behaviour lives in
`.gitignore`, which is not a declared path for run 0c3 so no mutation of it was attempted. That
was true, and it was not the whole reason. The test built its eight fixture paths from **string
literals** (`f".rheo-local/workspaces/{token}/profile.json"`, and seven like it) rather than
from `LOCAL_OPT_IN` (`packages/core/src/rheo_core/storage/data_root.py:36`), the constant this
hunk changes. So it asserted that a fixed list of paths somebody typed is ignored, not that
**what the product produces** is ignored — and the criterion's own words are "every
configuration file, upload, database, database sidecar, export, and log **it produces**". Under
this hunk the product wrote to `.rheo-data/` while the test went on asserting that
`.rheo-local/` is ignored: green and wrong.

**Fixed 2026-09-16, in C5, from the CodeRabbit thread on this row (PR #79).**
`tests/test_ignored_artifacts.py` now derives every fixture path from `LOCAL_OPT_IN`, so the
directory under test is the one the product actually writes into. Re-applying the hunk above
now reddens this demonstrator directly: the product writes to `.rheo-data/`, `git check-ignore`
reports all eight paths unignored, and the assertion names every one of them — the `Cost` entry
above is that observed line, not a predicted one.

The two demonstrators stay independent instruments rather than one guard seen twice: the first
reads the product's own refusal path, the second reads `.gitignore` through `git check-ignore`.
And deriving here leaves the name pinned, not unpinned — the literal `.rheo-local` is still
asserted exactly once, in `test_rheo_local_is_accepted_only_when_named_explicitly`
(`tests/test_data_root.py:114-131`), which is the literal-list companion that catches the
constant being renamed at all.

---

### Criterion 4

**Text:** "`python3 scripts/check_repository.py` exits zero in continuous integration on every branch, and the container build context excludes the private sibling directories by construction." (`build-plan.md:93-95`)

**State:** complete

**Demonstrator:**
- `ci:repository-checks / Check public repository boundaries and links`

**Mutation:**
```diff
diff --git a/README.md b/README.md
index 3ceeacc..b241a38 100644
--- a/README.md
+++ b/README.md
@@ -139,6 +139,8 @@ This verifies private-path exclusions, tracked artifacts, and local Markdown
 link targets. It also runs in GitHub Actions. It is not a secret scanner and
 does not replace review of public content.
 
+See the [local-only note](docs/untracked-local-note.md) for more.
+
 ## Contributing and license
 
 Read [CONTRIBUTING.md](CONTRIBUTING.md) before changing the architecture or
```

**Cost:** `ci:repository-checks / Check public repository boundaries and links` — first observed failure line: `Local link is not tracked: README.md -> docs/untracked-local-note.md`, exit status 1.

**Performed by:** C1 (2026-09-16)

**Note on the mutation's shape.** The natural mutation for this criterion is to un-ignore a
private path in `.gitignore` and watch the script report `Private path is not ignored: ...`.
`.gitignore` is not in run 0c3's `allowed_paths`, so that was not done. The hunk above trips
a different check in the same script, and one that belongs to the same publication-review
concern: a public document linking at a file that exists only on the author's machine. It
demonstrates that the CI step's exit status is wired to a real property of the tree, not to a
script that prints and returns zero.

**Note on the second clause.** "The container build context excludes the private sibling
directories by construction" has no test and cannot usefully have one. The build context is
the checkout root (`deploy/compose.yaml:37` sets `context: ..`, and the `docker` job builds
`.`), so a sibling directory outside the checkout is unreachable by Docker rather than
excluded by a rule anything could weaken. The mechanism that *is* mechanical is
`.dockerignore`'s deny-all-then-allowlist, and nothing asserts the built image lacks a
`.env` or a `CLAUDE.md`. Criterion 1's row exercises that allowlist from the buildability
side. An image-content assertion does not exist anywhere in the tree; recorded as an
evidence gap.

---

### Criterion 5

**Text:** "Each workspace's records are in a distinct Postgres database or schema, verified by a test that creates two workspaces, writes to one, and asserts the other's storage is unchanged." (`build-plan.md:96-98`)

**State:** complete

**Demonstrator:**
- `pytest:tests/postgres/test_isolation.py::test_a_note_written_in_a_is_absent_from_b`
- `pytest:tests/postgres/test_isolation.py::test_a_workspace_setting_write_in_a_leaves_b_unchanged`
- `pytest:tests/postgres/test_isolation.py::test_two_provisions_yield_two_rows_and_two_databases`

**Mutation:**
```diff
diff --git a/packages/core/src/rheo_core/storage/provisioning.py b/packages/core/src/rheo_core/storage/provisioning.py
index fd4362c..5a80c18 100644
--- a/packages/core/src/rheo_core/storage/provisioning.py
+++ b/packages/core/src/rheo_core/storage/provisioning.py
@@ -80,7 +80,7 @@ def database_name_for(workspace_id: UUID) -> str:
     """``ws_`` + the UUID's 32 hex digits. Pure; the only source of that name."""
     if not isinstance(workspace_id, UUID):
         raise TypeError("workspace_id must be a UUID")
-    return f"{DATABASE_NAME_PREFIX}{workspace_id.hex}"
+    return f"{DATABASE_NAME_PREFIX}{'0' * 32}"
 
 
 def core_version() -> str:
```

**Cost:** `pytest:tests/postgres/test_isolation.py::test_a_note_written_in_a_is_absent_from_b` — first observed failure line: `E           psycopg.errors.UniqueViolation: duplicate key value violates unique constraint "workspace_database_name_key"` with `DETAIL:  Key (database_name)=(ws_00000000000000000000000000000000) already exists.`

**Performed by:** C1 (2026-09-16)

**Note:** the red arrives earlier than expected and that is the interesting part. Making the
derived database name a constant does not produce two workspaces quietly sharing one
database; it cannot get that far, because `control.workspace.database_name` carries a unique
constraint. "Distinct database per workspace" is enforced by the schema, not only by the name
derivation, so the criterion holds at two levels rather than one.

---

### Criterion 6

**Text:** "No code path accepts a database path, connection string, schema name, workspace identifier, or actor identity from a request body, query parameter, model argument, or workspace setting. A test supplies each of those and asserts the value is ignored and the authenticated context is used." (`build-plan.md:99-102`)

**State:** complete

**Demonstrator:**
- `pytest:tests/postgres/test_context_routing.py::test_registering_a_reserved_input_field_is_refused_naming_it`
- `pytest:tests/postgres/test_context_routing.py::test_the_reserved_list_is_the_ratified_one`
- `pytest:tests/postgres/test_context_routing.py::test_a_dispatch_payload_naming_workspace_b_lands_in_a`
- `pytest:tests/postgres/test_context_routing.py::test_actor_id_in_a_payload_is_ignored_at_dispatch`
- `pytest:tests/postgres/test_context_routing.py::test_reserved_names_as_setting_rows_are_refused_setting_undeclared`
- `pytest:tests/postgres/test_api_surface.py::test_reserved_field_in_query_string_and_body_is_ignored`

**Mutation:**
```diff
diff --git a/packages/core/src/rheo_core/operations/registry.py b/packages/core/src/rheo_core/operations/registry.py
index 03d5ae6..f32d575 100644
--- a/packages/core/src/rheo_core/operations/registry.py
+++ b/packages/core/src/rheo_core/operations/registry.py
@@ -148,7 +148,7 @@ def _alias_names(model: type[BaseModel]) -> set[str]:
 
 def reserved_input_fields(model: type[BaseModel]) -> frozenset[str]:
     """The reserved names ``model`` would accept, by field name or alias."""
-    return frozenset(_alias_names(model) & RESERVED_INPUT_FIELDS)
+    return frozenset()
 
 
 @dataclass(frozen=True, slots=True)
```

**Cost:** `pytest:tests/postgres/test_context_routing.py::test_registering_a_reserved_input_field_is_refused_naming_it` — first observed failure line: `E           Failed: DID NOT RAISE RegistrationRefused`, twelve times, once per reserved name (`actor`, `actor_id`, `connection_string`, `database`, `dsn`, `schema`, `sql`, `statement`, `table_name`, `tenant_id`, `workspace`, `workspace_id`). 12 failed, 21 passed.

**Performed by:** C1 (2026-09-16)

**Note on a vacuity trap found while choosing this mutation, worth not rediscovering.** The
first mutation attempted was to delete `"workspace_id"` from `RESERVED_INPUT_FIELDS` in
`packages/contracts/src/rheo_contracts/manifest.py`. Only **one** test went red, and not the
refusal test: `test_registering_a_reserved_input_field_is_refused_naming_it` is parametrised
over `sorted(RESERVED_INPUT_FIELDS)`, so shrinking that set silently removes the case instead
of failing it. What caught the deletion was
`test_the_reserved_list_is_the_ratified_one`, which spells all twelve names as a literal.
That literal is the only thing standing between a shrunken reserved list and a green suite.
The mutation recorded above attacks the enforcement rather than the declaration, so all
twelve parametrised cases bite.

**That backstop is listed as the row's second `Demonstrator` for that reason, not for
completeness.** It is the only test in the tree that fails when the reserved list itself
loses a name, and the row's first demonstrator provably does not. Listing it puts it inside
what chunk 10's guard resolves, so a later rename or deletion of it turns the guard red
instead of quietly leaving criterion 6's declaration half unguarded. A general rule worth
carrying: any test parametrised over the constant it is testing needs a literal-list
companion, and that companion belongs in the field the guard reads.

**Note on the unmutated demonstrators in this row.** The recorded hunk reddens the first
demonstrator only. `test_the_reserved_list_is_the_ratified_one` is unmoved by it and is
reddened instead by the deletion described above, which was run and observed (1 failed, 31
passed) but is not the hunk this row records, since it demonstrates less. The three routing
demonstrators (`test_a_dispatch_payload_naming_workspace_b_lands_in_a`,
`test_actor_id_in_a_payload_is_ignored_at_dispatch`,
`test_reserved_field_in_query_string_and_body_is_ignored`) cover the dispatch and HTTP
channels of the same criterion. They are reddened by **criterion 7's** hunk, which severs
context-to-database routing: applying that hunk and running these three gives `3 failed in
2.70s`, checked here rather than assumed. So they are considered and set aside, not
unconsidered; one row carries one hunk, and the registration channel is the one this row
attacks.

---

### Criterion 7

**Text:** "A request carrying a valid record or operation identifier belonging to another workspace is refused by the HTTP API, the MCP façade, and the job dispatcher alike." (`build-plan.md:103-105`)

**State:** complete

**Demonstrator:**
- `pytest:tests/postgres/test_cross_workspace.py::test_cross_workspace_reference_refused_not_found_both_surfaces`
- `pytest:tests/postgres/test_cross_workspace.py::test_same_reference_under_an_a_token_succeeds_both_surfaces`

**Mutation:**
```diff
diff --git a/packages/core/src/rheo_core/storage/routing.py b/packages/core/src/rheo_core/storage/routing.py
index dcbf5d3..485baab 100644
--- a/packages/core/src/rheo_core/storage/routing.py
+++ b/packages/core/src/rheo_core/storage/routing.py
@@ -30,8 +30,11 @@ def active_workspace(workspace_id: UUID) -> WorkspaceRow:
     unless its state is ``active`` (a missing row is unavailable too)."""
     if not isinstance(workspace_id, UUID):
         raise TypeError("workspace_id must be a UUID")
+    from rheo_core.storage.control_plane import list_workspaces
+
     with get_backend().control_engine.connect() as connection:
-        row = get_workspace(connection, workspace_id)
+        active = list_workspaces(connection, state=WorkspaceState.ACTIVE)
+        row = active[0] if active else get_workspace(connection, workspace_id)
     if row is None:
         raise StorageRefusal(
             WORKSPACE_UNAVAILABLE,
```

**Cost:** `pytest:tests/postgres/test_cross_workspace.py::test_cross_workspace_reference_refused_not_found_both_surfaces` — first observed failure line: `E       assert 200 == 404`. The positive control in the same file
(`test_same_reference_under_an_a_token_succeeds_both_surfaces`) stays green under the hunk,
so the red is "a foreign reference was served" and not "everything broke".

**Performed by:** C1 (2026-09-16)

**Note:** the mutation severs the link between the authenticated context and the workspace
database by routing every context to the oldest active workspace. Both bearer surfaces go
through that one function, which is why one hunk reddens the HTTP and MCP halves together.

**Note on two clauses with weaker evidence than the record half.** "The job dispatcher
alike" is demonstrated indirectly by
`tests/postgres/test_work_failures.py::test_an_extra_payload_key_is_ignored_and_the_context_decides`,
which proves the work surface answers from the context's workspace but never presents it a
*valid identifier from another workspace*. "Or operation identifier" has no cross-workspace
test at all: `tests/postgres/test_operation_records.py::test_an_unknown_operation_id_refuses_not_found`
uses an unknown id, not a foreign one. Both clauses ride on the same context-routing
mechanism this hunk breaks, so they are not unguarded, but neither is presented with a
foreign identifier by any test in the tree. Recorded as an evidence gap.

---

### Criterion 8

**Text:** "A person signs in through GitHub OAuth and the resulting session carries an actor, a workspace, and a role. The provider is reached only through the identity-provider boundary, verified two ways: the second identity-provider implementation, which is a test double and not a shipped provider, is substituted behind the same boundary and leaves the domain service suite passing unchanged, and a check asserts no domain module imports the provider package. A second member added by the operator-level command receives a `member` session, and an operation restricted to `owner` is refused to it." (`build-plan.md:106-113`)

**State:** complete

**Demonstrator:**
- `pytest:tests/postgres/test_identity.py::test_github_provider_completes_with_the_documented_shape_and_a_scoped_secret`
- `pytest:tests/postgres/test_identity.py::test_context_from_session_succeeds_with_the_ratified_values`
- `pytest:tests/postgres/test_identity.py::test_context_from_session_reflects_the_membership_role`
- `pytest:tests/postgres/test_identity.py::test_sign_in_through_fixed_identity_provider_creates_a_session_row`
- `pytest:tests/test_boundary_scans.py::test_no_module_distribution_imports_the_identity_boundary`
- `pytest:tests/postgres/test_sessions.py::test_member_add_via_cli_then_sign_in_yields_role_member`
- `pytest:tests/postgres/test_api_surface.py::test_role_not_permitted_403`

**Mutation:**
```diff
diff --git a/packages/core/src/rheo_core/boundary/factories.py b/packages/core/src/rheo_core/boundary/factories.py
index e2f954d..341ce40 100644
--- a/packages/core/src/rheo_core/boundary/factories.py
+++ b/packages/core/src/rheo_core/boundary/factories.py
@@ -241,7 +241,7 @@ def context_from_session(
     return WorkspaceContext(
         workspace_id=session_row.active_workspace_id,
         actor=Actor(kind=ActorKind.ACCOUNT, id=session_row.account_id),
-        role=membership.role,
+        role=Role.OWNER,
         entry=Entry(entry),
         audience=Audience(kind=AudienceKind.SESSION, id=session_row.account_id),
         operation_set=ALL_OPERATIONS,
```

**Cost:**
- `pytest:tests/postgres/test_identity.py::test_context_from_session_reflects_the_membership_role` — first observed failure line: `E       AssertionError: assert <Role.OWNER: 'owner'> is <Role.MEMBER: 'member'>`
- `pytest:tests/postgres/test_sessions.py::test_member_add_via_cli_then_sign_in_yields_role_member` — first observed failure line: `E       AssertionError: assert 'owner' == 'member'`

**Performed by:** C1 (2026-09-16)

**Note:** the hunk attacks the role clause, which spans both ends of the criterion: "the
resulting session carries an actor, a workspace, and a role" and "a second member added by
the operator-level command receives a `member` session". One line makes every session an
owner session, and the CLI-driven end-to-end test is one of the two that catch it, which is
what makes this the right clause to mutate rather than the sign-in shape.

**Note on the import check.** The clause "a check asserts no domain module imports the
provider package" is demonstrated by
`tests/test_boundary_scans.py::test_no_module_distribution_imports_the_identity_boundary`,
an AST scan over the `modules/` distribution directory. Its mutation would be an
`import rheo_core.identity` planted in a file under `modules/`, and `modules/**` is a
**denied** path for run 0c3. That mutation was not attempted, so this clause carries a live
demonstrator with no performed mutation.

---

### Criterion 9

**Text:** "A locally issued command-line or MCP token names exactly one actor, one workspace, and one permitted operation set. It is issued either from an already-authenticated web session or by the operator-level command against the control plane, and presenting it never requires a browser flow. A test presents a malformed token, an expired token, and a valid token whose operation set excludes the requested operation, and asserts each is refused with a distinct non-success state and leaves no partial effect." (`build-plan.md:114-120`)

**State:** complete

**Demonstrator:**
- `pytest:tests/postgres/test_tokens.py::test_malformed_presentation_refused_with_unchanged_snapshot`
- `pytest:tests/postgres/test_tokens.py::test_expired_presentation_refused_with_unchanged_snapshot`
- `pytest:tests/postgres/test_tokens.py::test_out_of_scope_presentation_refused_with_unchanged_snapshot`
- `pytest:tests/postgres/test_tokens.py::test_expired_is_checked_before_revoked`
- `pytest:tests/postgres/test_tokens.py::test_session_issued_token_row_shapes`
- `pytest:tests/postgres/test_tokens.py::test_operator_issued_token_via_real_dispatch_path`

**Mutation:**
```diff
diff --git a/packages/core/src/rheo_core/tokens/presentation.py b/packages/core/src/rheo_core/tokens/presentation.py
index edec99c..4fef4a7 100644
--- a/packages/core/src/rheo_core/tokens/presentation.py
+++ b/packages/core/src/rheo_core/tokens/presentation.py
@@ -91,7 +91,7 @@ def resolve_token(value: str, surface: str) -> ResolvedToken | Refusal:
             return Refusal(TOKEN_MALFORMED)
         if row.kind not in _SURFACE_ACCEPTS.get(surface, frozenset()):
             return Refusal(TOKEN_WRONG_KIND)
-        if row.expires_at <= datetime.now(UTC):
+        if False:
             return Refusal(TOKEN_EXPIRED)
         if row.revoked_at is not None:
             return Refusal(TOKEN_REVOKED)
```

**Cost:**
- `pytest:tests/postgres/test_tokens.py::test_expired_presentation_refused_with_unchanged_snapshot` — first observed failure line: `E       AssertionError: assert WorkspaceContext(workspace_id=UUID(...), actor=Actor(kind=<ActorKind.TOKEN: 'token'...) == Refusal(state='token_expired', detail=None)` (the UUIDs are per-run; an expired token resolved to a live context instead of refusing).
- `pytest:tests/postgres/test_tokens.py::test_expired_is_checked_before_revoked` — first observed failure line: `E           state: 'token_revoked' != 'token_expired'`

2 failed, 17 passed.

**Performed by:** C1 (2026-09-16)

**Note:** the criterion names three presentations that must be refused with *distinct*
states. Removing the expiry branch is the mutation that shows the distinctness is real
rather than incidental: the token that should say `token_expired` falls through and either
resolves or says `token_revoked` instead, and a suite that only checked "some refusal
happened" would stay green.

**Note on the unmutated demonstrators in this row.** The recorded hunk attacks one of the
criterion's three named presentations, the expired one, and reddens two demonstrators. The
malformed and out-of-scope refusals
(`test_malformed_presentation_refused_with_unchanged_snapshot`,
`test_out_of_scope_presentation_refused_with_unchanged_snapshot`) and the two issuance-path
demonstrators (`test_session_issued_token_row_shapes`,
`test_operator_issued_token_via_real_dispatch_path`) are unmoved by it. They were considered
and set aside, not unconsidered: each would need its own branch removed from the same
refusal chain in `resolve_token`, and the expired branch was chosen because it is the one
the chain's *order* also depends on, so a single hunk reddens both a refusal test and an
ordering test. The other four are the same shape of guard on the same chain.

---

### Criterion 10

**Text:** "Every workspace row records the installed core version, each installed module's version, and each module's schema version, readable through a supported operation rather than by querying a migration table directly. No module exists in this phase, so the two module clauses pass on an empty set here; criterion 24 re-asserts them with the memory module installed, and only that re-assertion counts as their test." (`build-plan.md:121-126`)

**State:** complete

**Demonstrator:**
- `pytest:tests/postgres/test_workspace_status.py::test_status_under_an_operator_context`
- `pytest:tests/postgres/test_workspace_status.py::test_status_under_owner_and_member_harness_contexts`
- `pytest:tests/postgres/test_workspace_status.py::test_status_under_a_real_session_context`
- `pytest:tests/postgres/test_workspace_status.py::test_status_lists_an_enabled_module_with_no_applied_schema_step`

**Mutation:**
```diff
diff --git a/packages/core/src/rheo_core/operations/core_ops.py b/packages/core/src/rheo_core/operations/core_ops.py
index d8965bd..014663b 100644
--- a/packages/core/src/rheo_core/operations/core_ops.py
+++ b/packages/core/src/rheo_core/operations/core_ops.py
@@ -217,8 +217,11 @@ def _workspace_status(
     for version in list_module_schema_versions(connection):
         # Rows come ordered by applied_at, so the last write per module wins.
         latest[version.module_id] = version.schema_version
+    migration_version = connection.exec_driver_sql(
+        "SELECT version_num FROM core.alembic_version_core"
+    ).scalar_one()
     return WorkspaceStatus(
-        core_version=composition.core_version,
+        core_version=str(migration_version),
         core_contract_version=composition.core_contract_version,
         modules=[
             ModuleStatus(
```

**Cost:** `pytest:tests/postgres/test_workspace_status.py::test_status_under_an_operator_context` — first observed failure line: `E       AssertionError: ['SELECT current_database()', 'SELECT core.workspace_composition.singleton, core.workspace_composition.core_version, c..._schema_version.applied_at, core.module_schema_version.module_id', 'SELECT version_num FROM core.alembic_version_core']`. 4 failed, 1 passed.

**Performed by:** C1 (2026-09-16)

**Note:** the hunk violates both halves of the criterion's headline sentence at once. It reads
the core version from `core.alembic_version_core`, which is querying a migration table
directly, and it therefore stops reporting the *installed* core version. The statement-capturing
connection event in `_status_under` is what notices the first half, and
`status.core_version == metadata.version("rheo-core")` the second. Only the first is reached
before the test aborts, so the observed line is the captured-statement list.

---

### Criterion 22

**Text:** "Every link the web interface produces is generated through routing configuration, with no route string hard-coded to one topology. This phase's surface is the application shell, the login, the workspace switcher, the post-login redirect, and the OAuth callback URL, and the last two are topology-sensitive, so they are in scope here rather than later. In subdomain mode that surface spans the shell host and the identity host the companion document names under its recorded changes of direction, and the callback resolves to the identity host in subdomain mode and to a path on the single origin in single-host path mode, from the same routing configuration. The interface test suite covering that surface passes in single-host path mode and in subdomain mode with no code change between the two runs, and both runs are a continuous-integration gate from this phase onward, over whatever interface surface exists when it runs." (`build-plan.md:195-205`)

**State:** complete

**Demonstrator:**
- `ci:web / Vitest (path mode)`
- `ci:web / Vitest (subdomain mode)`
- `ci:web / Routing-literal gate (criterion 22)`
- `pytest:tests/test_routing.py::test_the_identity_callback_is_the_documented_url`
- `pytest:tests/test_routing.py::test_url_for_matches_the_fixture`

**Mutation:**
```diff
diff --git a/apps/web/src/components/logout-form.tsx b/apps/web/src/components/logout-form.tsx
index 508a537..1618256 100644
--- a/apps/web/src/components/logout-form.tsx
+++ b/apps/web/src/components/logout-form.tsx
@@ -7,9 +7,9 @@
  * same-host path from `logoutAction`; an absolute URL would send a subdomain-mode
  * POST to the identity host, which does not hold this host's session cookie.
  */
-export function LogoutForm({ action }: { action: string }) {
+export function LogoutForm({ action: _action }: { action: string }) {
   return (
-    <form method="post" action={action}>
+    <form method="post" action="/auth/logout">
       <button type="submit">Sign out</button>
     </form>
   );
diff --git a/apps/web/src/lib/routing/url-for.ts b/apps/web/src/lib/routing/url-for.ts
index a7f2245..d9472a1 100644
--- a/apps/web/src/lib/routing/url-for.ts
+++ b/apps/web/src/lib/routing/url-for.ts
@@ -88,15 +88,7 @@ export function urlFor(
     throw new Error(`routing surface '${surface}' is not served by this application`);
   }
   const surfacePath = spec.path ?? "";
-  if (config.mode === "path") {
-    return `${config.scheme}://${config.public_host}${joinPrefix(surfacePath, path)}`;
-  }
-  // Subdomain mode: every surface drops its prefix once it has its own host —
-  // except `identity`, whose `fixed_path` holds `/auth/*` on every application
-  // host in both modes.
-  const prefix = spec.fixed_path === true ? surfacePath : "";
-  const host = `${spec.host}.${config.public_host}`;
-  return `${config.scheme}://${host}${joinPrefix(prefix, path)}`;
+  return `${config.scheme}://${config.public_host}${joinPrefix(surfacePath, path)}`;
 }
 
 /**
diff --git a/packages/core/src/rheo_core/routing/url_for.py b/packages/core/src/rheo_core/routing/url_for.py
index 0e678ef..7fe146f 100644
--- a/packages/core/src/rheo_core/routing/url_for.py
+++ b/packages/core/src/rheo_core/routing/url_for.py
@@ -58,11 +58,7 @@ def url_for(config: RoutingConfig, surface: str, path: str) -> str:
         raise ValueError(
             f"routing surface {surface!r} is not served by this application"
         )
-    if config.mode is RoutingMode.PATH:
-        return f"{config.scheme}://{config.public_host}{_join_prefix(spec.path, path)}"
-    prefix = spec.path if spec.fixed_path else ""
-    host = f"{spec.host}.{config.public_host}"
-    return f"{config.scheme}://{host}{_join_prefix(prefix, path)}"
+    return f"{config.scheme}://{config.public_host}{_join_prefix(spec.path, path)}"
 
 
 def identity_path(config: RoutingConfig, path: str) -> str:
```

**Cost:**
- `ci:web / Routing-literal gate (criterion 22)` — first observed failure line: `Hard-coded route literal: apps/web/src/components/logout-form.tsx`, exit status 1.
- `ci:web / Vitest (subdomain mode)` — first observed failure line: `AssertionError: expected 'example.test' to be 'auth.example.test' // Object.is equality`. 15 tests failed.
- `ci:web / Vitest (path mode)` — first observed failure line: `AssertionError: expected 'example.test' to be 'auth.example.test' // Object.is equality`. 5 tests failed.
- `pytest:tests/test_routing.py::test_the_identity_callback_is_the_documented_url` — first observed failure line: `E       AssertionError: assert 'https://example.test/login' == 'https://circ...le.test/login'`. 9 failed, 34 passed.

**Performed by:** C1 (2026-09-16), C9 (2026-09-17)

**Note on C9's ops-side half (2026-09-17).** C1 proved the routing contract through product
code and vitest; C9 proves the static gate itself bites. Mutation applied to
`apps/web/src/components/workspace-switcher.tsx`, confirmed, reverted:

```diff
diff --git a/apps/web/src/components/workspace-switcher.tsx b/apps/web/src/components/workspace-switcher.tsx
--- a/apps/web/src/components/workspace-switcher.tsx
+++ b/apps/web/src/components/workspace-switcher.tsx
@@ -34,7 +34,7 @@ export function WorkspaceSwitcher({
     setPending(true);
     setRefusal(null);
     try {
-      const response = await fetch(action, {
+      const response = await fetch("/api/v1/planted", {
         method: "POST",
         credentials: "same-origin",
         headers: { "Content-Type": "application/json" },
```

- `ci:web / Routing-literal gate (criterion 22)` — first observed failure line: `Hard-coded route literal: apps/web/src/components/workspace-switcher.tsx`, exit status 1.

**Note on why this row has five demonstrators and one three-file hunk.** The criterion is
three claims and each has its own gate. `check_routing_literals.py` is the "no route string
hard-coded to one topology" half, and `logout-form.tsx` is where the hunk plants one, outside
the routing package the scan allowlists. The two vitest runs are the "passes in path mode and
in subdomain mode with no code change between the two runs" half, and the `url-for.ts` change
is the topology hard-coding that breaks them. `tests/test_routing.py` is the Python side of
the same contract: both implementations are asserted against the same three fixture files in
`tests/fixtures/routing/`, so a hunk that hard-codes only one language would leave the other
green and the cross-language guarantee unproven. Hard-coding both is what shows the fixtures
are the contract rather than a convenience.

**Note on the path-mode run going red too.** Five tests fail in `RHEO_ROUTING_MODE=path` as
well, and that is the design working. `links.test.ts`'s "links across both modes" block and
`url-for.test.ts`'s "rules asserted against literals, in both modes" block load both fixture
files regardless of which mode the run selects, precisely so that neither run can be
vacuously green. The subdomain run is the one the criterion names; the path run failing too
means the both-modes assertions are not sleeping.

---

### Criterion 23

**Text:** "The web interface builds and runs in the single-server container deployment with no hosting-platform-specific feature in its dependency set, enforced by a build that fails on a platform-only import or configuration key. That build is a continuous-integration gate from this phase onward, over whatever interface surface exists when it runs. *(Scenario: Fresh public clone; FR 48, guardrail 15.)*" (`build-plan.md:206-210`)

**State:** complete

**Demonstrator:**
- `ci:docker / Build the core/worker image`
- `ci:web / Web platform-only gate (criterion 23)`

**Mutation:**
```diff
diff --git a/apps/web/src/app/layout.tsx b/apps/web/src/app/layout.tsx
--- a/apps/web/src/app/layout.tsx
+++ b/apps/web/src/app/layout.tsx
@@ -1,5 +1,6 @@
 import type { Metadata } from "next";
 import type { ReactNode } from "react";
+import { x } from "@vercel/edge";
 
 export const metadata: Metadata = {
   title: "rheoStream",
```

A second mutation (`export const runtime = "edge"` in `apps/web/src/app/page.tsx`) was applied, confirmed, and reverted separately; first observed failure line: `Edge runtime export declared: apps/web/src/app/page.tsx`, exit status 1.

**Cost:**
- `ci:web / Web platform-only gate (criterion 23)` — first observed failure line: `Platform-only @vercel/* import: apps/web/src/app/layout.tsx`, exit status 1.
- `ci:docker / Build the core/worker image` — see C9 handoff for the GitHub Actions workflow run URL and conclusion on branch `bureau/20260916-0c3-remaining-skeleton`.

**Performed by:** C9 (2026-09-17)

---

### Criterion 11

**Text:** "A state change and its outgoing event commit in one transaction. A test that kills the process between commit and delivery, then restarts, observes the event delivered exactly once to the deduplicating test consumer, which records processed identifiers." (`build-plan.md:127-130`)

**State:** complete

**Demonstrator:**
- `pytest:tests/postgres/test_event_delivery.py::test_a_redelivery_of_a_processed_event_never_runs_the_handler_again`
- `pytest:tests/postgres/test_event_delivery.py::test_a_process_that_dies_between_commit_and_delivery_delivers_exactly_once`
- `pytest:tests/postgres/test_event_delivery.py::test_a_published_event_is_delivered_to_its_consumer_exactly_once`
- `pytest:tests/postgres/test_outbox.py::test_a_rolled_back_publish_leaves_neither_the_event_nor_its_deliveries`
- `pytest:tests/postgres/test_outbox.py::test_a_second_record_processed_for_the_same_pair_raises`

**Mutation:**
```diff
diff --git a/packages/core/src/rheo_core/work/loop.py b/packages/core/src/rheo_core/work/loop.py
index feeba15..1da746d 100644
--- a/packages/core/src/rheo_core/work/loop.py
+++ b/packages/core/src/rheo_core/work/loop.py
@@ -893,7 +893,7 @@ def _run_consumer(
         connection = uow.connection
         try:
             if not already_processed(
-                connection, consumer_id=leased.consumer_id, event_id=leased.event_id
+                connection, consumer_id=owner, event_id=leased.event_id
             ):
                 handler(HandlerUnitOfWork(uow), _envelope_for(connection, leased))
                 record_processed(
```

**Cost:** `pytest:tests/postgres/test_event_delivery.py::test_a_redelivery_of_a_processed_event_never_runs_the_handler_again` — first observed failure line: `E       AssertionError: the consumer's handler body started twice: a redelivery of an event already in core.consumer_processed must be marked delivered without running it`, then `E       assert 2 == 1`. 1 failed, 20 passed across `test_outbox.py` and `test_event_delivery.py` together.

**Performed by:** C2 (2026-09-16)

**Note on why the key is substituted rather than the check deleted.** The archived mutation
(0c2's M11c) deletes the `already_processed` call so a redelivery re-runs the handler. Swapping
its `consumer_id` for the worker's lease-owner string reaches the same state by a different
route — the ledger row for the real consumer never matches, so the handler body starts again —
and it leaves the `record_processed` write on the real pair, which is what produces the
primary-key conflict the archived prompt's own note describes. The counter reads **exactly 2**
under the hunk and 1 unmutated, which is the assertion that distinguishes this failure; the
delivery's `state` assertion would move under several unrelated mutations and is the second
signal, not the evidence.

**Note on the positive controls.** Twenty of the twenty-one tests in these two files stay green,
including the criterion's own crash sentence
(`test_a_process_that_dies_between_commit_and_delivery_delivers_exactly_once`, a real
`os._exit(0)` between the child's commit and the parent's delivery). The red is therefore "the
event was delivered twice", not "the delivery path broke".

**Note on the demonstrators this hunk does not redden, and why they are listed anyway.** The
criterion is two sentences. The at-most-once half is what this hunk attacks. The one-transaction
half is `test_a_rolled_back_publish_leaves_neither_the_event_nor_its_deliveries`, and the
mutation for it that was tried first — a `connection.commit()` at the end of
`events/publish.py`'s `publish` — turned **all 21** tests in both files red, which is a
sledgehammer rather than a demonstration and was discarded for that reason. It was run and
observed, not inferred. One row carries one hunk; the surgical one is recorded.

---

### Criterion 12

**Text:** "A worker killed mid-task releases its lease and the task is retried within its bounded retry budget; a task exhausting its budget appears in a failure list with its error, and is not silently dropped. A queued task that is cancelled never runs, and a running task that is cancelled reaches a terminal cancelled status rather than a success or a further retry, verified by a test that cancels one of each and asserts the recorded terminal status and the absence of any later effect." (`build-plan.md:131-136`)

**State:** complete

**Demonstrator:**
- `pytest:tests/postgres/test_worker_loop.py::test_a_job_that_dies_on_every_attempt_reaches_failed_rather_than_relooping`
- `pytest:tests/postgres/test_worker_loop.py::test_an_exhausted_long_running_job_terminalises_its_operation_record`
- `pytest:tests/postgres/test_worker_loop.py::test_a_job_whose_lease_expired_is_reacquired_and_runs_to_completion`
- `pytest:tests/postgres/test_worker_loop.py::test_a_cancelled_queued_job_is_terminal_before_any_visit`
- `pytest:tests/postgres/test_worker_loop.py::test_a_cancellation_observed_at_a_checkpoint_ends_the_job_cancelled`
- `pytest:tests/postgres/test_worker_loop.py::test_a_cancellation_never_observed_by_the_handler_ends_cancelled_not_queued`
- `pytest:tests/postgres/test_worker_loop.py::test_a_cancelled_job_that_is_also_out_of_budget_ends_cancelled_not_failed`

**Mutation:**
```diff
diff --git a/packages/core/src/rheo_core/work/loop.py b/packages/core/src/rheo_core/work/loop.py
index feeba15..be71fab 100644
--- a/packages/core/src/rheo_core/work/loop.py
+++ b/packages/core/src/rheo_core/work/loop.py
@@ -383,7 +383,7 @@ def _run_leased_job(
             )
         return
 
-    if leased.attempts > leased.max_attempts:
+    if leased.attempts > leased.max_attempts + 1:
         # The crash-side half of the retry budget: a dead worker reports nothing, so
         # the next lease is the first moment anything can observe exhaustion. ``>``,
         # one more than the exception arm's ``>=``, deliberately — both arms grant
```

**Cost:**
- `pytest:tests/postgres/test_worker_loop.py::test_a_job_that_dies_on_every_attempt_reaches_failed_rather_than_relooping` — first observed failure line: `E       AssertionError: assert 'succeeded' == 'failed'`
- `pytest:tests/postgres/test_worker_loop.py::test_an_exhausted_long_running_job_terminalises_its_operation_record` — first observed failure line: `E       AssertionError: assert 'succeeded' == 'failed'`

2 failed, 33 passed.

**Performed by:** C2 (2026-09-16)

**Note on why the budget is widened by one rather than the gate removed.** The archived mutation
(0c1's number 5) deletes pre-handler gate 2 outright. Adding one to its bound produces the same
observable on the same pass — the re-lease falls through to a handler that succeeds — while
keeping the gate in the file, so the hunk is a bound that is wrong by one rather than a branch
that is gone. The identical `'succeeded' == 'failed'` line on both nodes is not a copy-paste but
it is the same assertion: both tests read the `job` row first, and the second
(`test_an_exhausted_long_running_job_terminalises_its_operation_record`, at
`tests/postgres/test_worker_loop.py:626`) carries its `core.operation` assertion three lines
further down, which the hunk never reaches because the job-row assertion aborts the test first.
The operation-record half of "is not silently dropped" is therefore covered by that test and not
by this observation — recorded rather than implied.

**Note on the cancellation demonstrators, which this hunk does not move.** Criterion 12's second
sentence is a separate mechanism — `AND cancel_requested = false` on the `requeue_for_retry` and
`finish_failed` predicates, two files away in `work/jobs.py` — and 0c1 carried two mutations of
its own for it. The four cancellation demonstrators above
(`test_a_cancelled_queued_job_is_terminal_before_any_visit`,
`test_a_cancellation_observed_at_a_checkpoint_ends_the_job_cancelled`,
`test_a_cancellation_never_observed_by_the_handler_ends_cancelled_not_queued`,
`test_a_cancelled_job_that_is_also_out_of_budget_ends_cancelled_not_failed`) are listed because
they are real demonstrators of this criterion and chunk 10's guard should resolve them; they are
considered and set aside, not unconsidered. One row carries one hunk, and the budget half is the
one this row attacks.

---

### Criterion 13

**Text:** "Every long-running operation returns an operation identifier before it completes, and that operation reports one of a fixed set of terminal statuses, including an explicit unresolved status. A test asserts no operation can report success without a recorded terminal check." (`build-plan.md:137-140`)

**State:** complete

**Demonstrator:**
- `pytest:tests/postgres/test_operation_records.py::test_the_minted_id_is_readable_while_the_work_is_still_pending`
- `pytest:tests/postgres/test_operation_records.py::test_the_record_is_readable_from_outside_while_the_handler_is_still_running`
- `pytest:tests/postgres/test_operation_records.py::test_a_succeeded_job_terminalises_its_record_with_a_terminal_check`
- `pytest:tests/postgres/test_operation_records.py::test_a_failed_job_terminalises_its_record_failed_and_not_succeeded`
- `pytest:tests/postgres/test_operation_records.py::test_the_database_refuses_succeeded_without_a_terminal_check`
- `pytest:tests/postgres/test_operation_records.py::test_resolve_clears_an_unresolved_record_to_a_named_outcome_with_a_note`
- `pytest:tests/postgres/test_operation_records.py::test_a_non_long_running_operation_mints_no_record_at_all`

**Mutation:**
```diff
diff --git a/packages/core/src/rheo_core/operations/dispatch.py b/packages/core/src/rheo_core/operations/dispatch.py
index 06b125f..26abc94 100644
--- a/packages/core/src/rheo_core/operations/dispatch.py
+++ b/packages/core/src/rheo_core/operations/dispatch.py
@@ -934,5 +934,5 @@ def dispatch(
     # committed; every path that did not raised or returned inside the block above.
     if operation_id is not None:
         _mark_workspace_due(ctx)
-        return OperationOutcome(PENDING, result=output, operation_id=operation_id)
+        return OperationOutcome(PENDING, result=output, operation_id=None)
     return OperationOutcome(SUCCEEDED, result=output)
```

The hunk above was recaptured at C3, which is why it is younger than the rest of this row: C3's own `dispatch.py` change added a post-commit due-work call on this exact branch, moving the context lines so the originally captured hunk no longer passed `git apply --check`. The mutation is the same one (0c2's M13c) and `Cost` below was re-verified under the recaptured hunk, not carried over.

**Cost:**
- `pytest:tests/postgres/test_operation_records.py::test_the_minted_id_is_readable_while_the_work_is_still_pending` — first observed failure line: `E       AssertionError: OperationOutcome(state='pending', result=NoteScheduled(job_id=UUID('01a0ab4b-7801-75ed-834f-15a76c9463a2'), operation_id=UUID('01a0ab4b-77f0-7372-8606-f13ddb70769e')), error=None, operation_id=None)`, then `E       assert None is not None` (the UUIDs are per-run).
- `pytest:tests/postgres/test_operation_records.py::test_a_succeeded_job_terminalises_its_record_with_a_terminal_check` — first observed failure line: the same `E       AssertionError: OperationOutcome(state='pending', … operation_id=None)` then `E       assert None is not None`.
- `pytest:tests/postgres/test_operation_records.py::test_a_failed_job_terminalises_its_record_failed_and_not_succeeded` — first observed failure line: the same pair.
- `pytest:tests/postgres/test_operation_records.py::test_resolve_clears_an_unresolved_record_to_a_named_outcome_with_a_note` — first observed failure line: the same pair.

11 failed, 12 passed.

**Performed by:** C2 (2026-09-16)

**Note on what the failure text shows, which is the point of choosing this hunk.** This is 0c2's
M13c ("return `operation_id=None` from a `long_running` dispatch"), and the assertion message
prints the whole outcome: the record **was** minted and its id is sitting right there inside
`NoteScheduled`, while `OperationOutcome.operation_id` is `None`. So the red is specifically
"the identifier was not returned to the caller", not "no record exists" — which is the clause
the criterion's first sentence actually makes ("returns an operation identifier **before it
completes**").

**Note on how much independence those four reds actually represent — less than four.** All four
fail on the *same* line: `assert outcome.operation_id is not None, outcome`, inside the shared
`_schedule` helper each of them calls to set up its own case. So the hunk is caught once, at one
assertion, by four tests that all enter through it, and the four `Cost` entries above are four
sightings of one guard rather than four independent pins. They are recorded because the README's
rule makes the absence of a cost line a claim that the hunk left a demonstrator alone, and that
claim would have been false for three of them; they are not recorded as four separate proofs.

**Note on the three demonstrators that stayed green, checked rather than assumed.** Eleven of
the file's twenty-three tests failed under this hunk and the other twelve were read, not
inferred. Three of the twelve are listed above.
`test_the_database_refuses_succeeded_without_a_terminal_check` is the criterion's last sentence
("no operation can report success without a recorded terminal check") and is enforced by a
database constraint rather than by the dispatcher, so no dispatcher hunk can redden it; its own
mutation is 0c2's M13b, at the worker's terminalisation call site.
`test_the_record_is_readable_from_outside_while_the_handler_is_still_running` reads the record
through the supported operation rather than off `OperationOutcome`, which is exactly the channel
this hunk leaves intact. `test_a_non_long_running_operation_mints_no_record_at_all` asserts the
negative case and is unreachable by a hunk on the `long_running` branch.

---

### Criterion 14

**Text:** "Every mutating operation writes an audit record naming actor, workspace, operation, and time, readable through a supported operation rather than by querying a table directly. A mutating operation registered without an audit path fails registration at startup with a named operation, so the record cannot be skipped by omission. A test performs one mutation of each registered kind and asserts a matching audit record exists for every one." (`build-plan.md:141-146`)

**State:** complete

**Demonstrator:**
- `pytest:tests/postgres/test_audit_dispatch.py::test_one_dispatch_of_every_mutating_kind_leaves_a_matching_audit_record`
- `pytest:tests/postgres/test_audit_dispatch.py::test_the_mutating_set_derived_from_the_registry_is_the_declared_seventeen`
- `pytest:tests/postgres/test_audit_dispatch.py::test_the_success_row_names_the_actor_the_entry_and_the_request`
- `pytest:tests/postgres/test_audit_dispatch.py::test_an_operation_above_mutate_is_audited_too`
- `pytest:tests/postgres/test_context_routing.py::test_registering_a_non_read_operation_with_no_audit_spec_is_refused`
- `pytest:tests/postgres/test_audit_dispatch.py::test_check_audit_paths_names_every_offender`
- `pytest:tests/postgres/test_audit_dispatch.py::test_a_missing_sink_refuses_the_call_and_writes_neither_effect_nor_row`

**Mutation:**
```diff
diff --git a/packages/core/src/rheo_core/audit/core_sink.py b/packages/core/src/rheo_core/audit/core_sink.py
index 9296e2f..97e5124 100644
--- a/packages/core/src/rheo_core/audit/core_sink.py
+++ b/packages/core/src/rheo_core/audit/core_sink.py
@@ -75,7 +75,7 @@ class CoreAuditSink:
             actor_kind=ctx.actor.kind.value,
             actor_id=ctx.actor.id,
             entry=ctx.entry.value,
-            operation_name=operation,
+            operation_name=operation.split(".", 1)[0],
             safety_class=safety_class.value,
             operation_id=operation_id,
             subject_ref=None if subject_ref is None else subject_ref.format(),
```

**Cost:**
- `pytest:tests/postgres/test_audit_dispatch.py::test_one_dispatch_of_every_mutating_kind_leaves_a_matching_audit_record` — first observed failure line: `E       AssertionError: ['core', 'harness']`, then `E       assert {'core', 'harness'} == {'core.approv....create', ...}` with `'core'` and `'harness'` as the extra items in the left set and the fourteen operation names in the right.
- `pytest:tests/postgres/test_audit_dispatch.py::test_the_success_row_names_the_actor_the_entry_and_the_request` — first observed failure line: `E       AssertionError: assert 'core' == 'core.settings.set'`.
- `pytest:tests/postgres/test_audit_dispatch.py::test_an_operation_above_mutate_is_audited_too` — first observed failure line: `E       AssertionError: assert 'harness' == 'harness.probe.destroy'`.

13 failed, 10 passed.

**Performed by:** C2 (2026-09-16), C6 (2026-09-16)

**Note on which clause this attacks.** The criterion's first sentence requires the record to
**name the operation**, and the third requires one record per registered mutating kind, read
through a supported operation (`core.audit.list`) rather than off the table. The hunk writes the
module id where the operation name belongs, so every dispatch still produces a row, the row is
still readable through `core.audit.list`, and the fourteen distinct names collapse to two. The red
is therefore "the audit record does not identify what happened", which is the failure that would
be hardest to notice in production: the rows are all there and the count is right.

**Note on what the three reds do and do not span.** They are three different assertions in three
different tests — a set comparison against `THE_FOURTEEN`, a single-row column check, and the
above-`mutate` case — rather than one shared helper seen three times, so this row's `Cost` is
three independent sightings, and each was re-run **individually** at re-capture rather than read
off the aggregate. **Three** of the other listed demonstrators stayed green, and were
read rather than assumed:
`test_the_mutating_set_derived_from_the_registry_is_the_declared_fourteen`
reads the registry, not the rows; `test_check_audit_paths_names_every_offender` and
`test_a_missing_sink_refuses_the_call_and_writes_neither_effect_nor_row` are registration- and
dispatch-time refusals that never reach a sink write.

**The fourth listed demonstrator now has an observed result, and it is green.**
`test_registering_a_non_read_operation_with_no_audit_spec_is_refused` is in another file
(`test_context_routing.py`) and was not in C2's run, so C2 recorded no claim about it. C6's
re-capture ran it under this hunk: 5 passed, all five parametrised safety classes. That is the
expected answer rather than a surprise — it is a *registration*-time refusal and this hunk edits
a sink write — and it is recorded because "not executed" was a gap that a single command closed.

**Note on the fourteen-operation set and where its literal lives.**
`test_the_mutating_set_derived_from_the_registry_is_the_declared_fourteen` derives the set from the
registry by safety class and compares it against `THE_FOURTEEN`, a literal frozenset of the
fourteen names (resolve it by that constant name, not by a line number). It is
listed as a demonstrator for the same reason criterion 6's second demonstrator is: it is the
literal-list companion that catches a shrinking set, which a coverage assertion derived from the
registry alone could not. Neither test is parametrised over the constant it tests.

**Why this row was re-captured twice, by C6 and again by C7, and why `State` is still
`complete`.** Run 0c3's C6 registered four more operations above the read class —
`core.approval.approve` and `core.approval.refuse`, plus the two upper-class harness fixtures
`harness.fixture.act` and `harness.sink.send` — growing the set this row's coverage clause is
*over* from eight to twelve. C7 then registered `core.standing_grant.create` and
`core.standing_grant.revoke`, growing it to fourteen. Each time the test that names the set was
renamed with its constant, and a renamed demonstrator id resolves to nothing.

The criterion itself is unchanged and still holds: every one of the fourteen leaves a matching
audit record, read through `core.audit.list`. **What changed is this row's evidence, not its
verdict.** C7 re-applied the same hunk on `ad4783d`, re-ran the file (13 failed, 10 passed — the
same aggregate C6 recorded) and re-ran each reddened node **individually**. Two of the three
`Cost` lines came back byte-identical; the first one's second fragment did not, because the
right-hand set now sorts `'core.approv....create'` where it sorted `'core.approv...n.issue'`, and
that fragment is what is corrected above.

**The renamed demonstrator is green under this hunk, and that was run rather than assumed.**
`test_the_mutating_set_derived_from_the_registry_is_the_declared_fourteen` reads the registry,
not the audit rows, so a hunk that edits a sink write cannot reach it: 1 passed on its own at
C7's re-capture. That is the same answer C6 recorded for the same reason.

**One `Cost` line changed for a second reason, and it is recorded rather than smoothed over.**
`test_an_operation_above_mutate_is_audited_too` used to dispatch a `DESTRUCTIVE` probe and assert
a `succeeded` row; from C6 that dispatch is *held* for an approval, so the test asserts a
`refused` row instead. Its first observed failure line under this hunk is unchanged
(`assert 'harness' == 'harness.probe.destroy'`) because the operation-name assertion still fails
first — but that identity was **captured, not assumed**: the line above is what the node printed
when run on its own at re-capture.

**Note on one clause with a live demonstrator and no performed mutation.** "A mutating operation
registered without an audit path fails registration at startup with a named operation" is
demonstrated by
`tests/postgres/test_context_routing.py::test_registering_a_non_read_operation_with_no_audit_spec_is_refused`,
parametrised over the five non-`READ` safety classes written as a literal list. Its mutation is
0c2's **M14a** — remove the `audit is None` check from `OperationRegistry.register`
(`packages/core/src/rheo_core/operations/registry.py:206-212`). **That edit was attempted on this
tree and refused by this session's own permission layer**, which classified deleting the
registration guard as audit tampering; it was not performed, and no inference about it is
recorded here. The clause carries a live demonstrator with no mutation performed by C2.

**Note accounting for all six mutations 0c2's own checkpoint offered, since this row uses none of
them.** M14a removes the `audit is None` guard in `register` — attempted, refused, above. **M14b**
(make `check_audit_paths` log instead of raise), **M14c** (drop the missing-sink refusal so the
operation succeeds with no audit row), **M14e** (drop the non-success audit row entirely) and
**M14f** (write the row for `MUTATE` only, skipping every class above it) were **not attempted**.
Each disables an audit write or an audit guard, which is the shape the permission layer refused
for M14a — but that is a prediction from one refusal, not four observations, and it is written
here as a prediction. **M14d** (commit the success-path audit row in a transaction separate from
the operation's) is **not** that shape and was also not attempted: its observable is an orphaned
audit row after a failed operation, caught by
`test_an_operation_whose_own_transaction_cannot_commit_leaves_only_a_failed_row` — a test this
row does not list, and adding a demonstrator so the row can carry a second hunk is wider than one
row's job. Recorded as a gap rather than as a reason.

---

### Criterion 15

**Text:** "A workflow that declares a requirement the configured runtime cannot satisfy (structured output, streaming, or continuation) is rejected before the runtime is invoked, with a named unmet capability." (`build-plan.md:147-149`)

**State:** complete

**Demonstrator:**
- `pytest:tests/postgres/test_runtime_matrix.py::test_capability_gate_refuses_named_unmet_capability_before_start`

**Mutation:**
```diff
diff --git a/packages/core/src/rheo_core/runtime/operations.py b/packages/core/src/rheo_core/runtime/operations.py
index 5f5b1f7..ee6bdd1 100644
--- a/packages/core/src/rheo_core/runtime/operations.py
+++ b/packages/core/src/rheo_core/runtime/operations.py
@@ -688,7 +688,7 @@ def make_run_runtime_job(
                 )
                 return
             try:
-                check_runtime_gate(payload.requirements, adapter.capabilities())
+                pass
             except UnmetCapability as exc:
                 _fail(
                     uow,
```

**Cost:**
- `pytest:tests/postgres/test_runtime_matrix.py::test_capability_gate_refuses_named_unmet_capability_before_start` — first observed failure line: `E       AssertionError: assert 'succeeded' == 'failed'`.

4 failed (structured_output, streaming, continuation, isolation_enforced). Applying the hunk skips `RuntimeGate.check` in `run_runtime_job`; each named-kind demonstrator then false-succeeds and `adapter.start` runs.

**Performed by:** C5 (2026-09-18)

**Note:** The family covers AC 1 and AC 2. Bool requirements pin `error_code` to `UnmetCapability.name` (`structured_output`, `streaming`, `continuation`). The isolation row pins the literal token `isolation=enforced` against `isolation = advisory`. Every case also asserts `terminal_check_kind` is not `handler_returned`, the job row is `succeeded` (finish_failed-then-return), `start` count is zero, and `error_code` is not `job_failed`. Injectable recording doubles only; no live `claude`.

---

### Criterion 16

**Text:** "A headless run whose executable is missing, whose credential is expired, whose tool is denied, whose deadline elapses before the runtime answers, or whose output stream truncates returns a distinct non-success state within the configured deadline. A test induces each of the five, the timeout by a runtime double that never answers, and asserts no hang and no success report." (`build-plan.md:150-154`)

**State:** complete

**Demonstrator:**
- `pytest:tests/postgres/test_runtime_matrix.py::test_executable_unavailable_fails_named_kind_within_deadline`
- `pytest:tests/postgres/test_runtime_matrix.py::test_credential_invalid_fails_named_kind_within_deadline`
- `pytest:tests/postgres/test_runtime_matrix.py::test_tool_denied_fails_named_kind_within_deadline`
- `pytest:tests/postgres/test_runtime_matrix.py::test_never_answer_fails_deadline_exceeded_without_hang`
- `pytest:tests/postgres/test_runtime_matrix.py::test_stream_truncated_fails_named_kind_within_deadline`
- `pytest:tests/postgres/test_runtime_matrix.py::test_login_from_other_member_is_credential_not_owned_without_spawn`

**Mutation:**
```diff
diff --git a/packages/core/src/rheo_core/runtime/operations.py b/packages/core/src/rheo_core/runtime/operations.py
index 5f5b1f7..0556ffb 100644
--- a/packages/core/src/rheo_core/runtime/operations.py
+++ b/packages/core/src/rheo_core/runtime/operations.py
@@ -373,7 +373,7 @@ def _fail(
         uow.connection,
         operation_id=payload.operation_id,
         now=now,
-        error_code=error_code,
+        error_code="job_failed",
         error_text=error_text,
     )
     if request_id is not None:
```

**Cost:**
- `pytest:tests/postgres/test_runtime_matrix.py::test_executable_unavailable_fails_named_kind_within_deadline` — first observed failure line: `E       AssertionError: assert 'job_failed' == 'executable_unavailable'`.
- `pytest:tests/postgres/test_runtime_matrix.py::test_credential_invalid_fails_named_kind_within_deadline` — first observed failure line: `E       AssertionError: assert 'job_failed' == 'credential_invalid'`.
- `pytest:tests/postgres/test_runtime_matrix.py::test_tool_denied_fails_named_kind_within_deadline` — first observed failure line: `E       AssertionError: assert 'job_failed' == 'tool_denied'`.
- `pytest:tests/postgres/test_runtime_matrix.py::test_never_answer_fails_deadline_exceeded_without_hang` — first observed failure line: `E       AssertionError: assert 'job_failed' == 'deadline_exceeded'`.
- `pytest:tests/postgres/test_runtime_matrix.py::test_stream_truncated_fails_named_kind_within_deadline` — first observed failure line: `E       AssertionError: assert 'job_failed' == 'stream_truncated'`.
- `pytest:tests/postgres/test_runtime_matrix.py::test_login_from_other_member_is_credential_not_owned_without_spawn` — first observed failure line: `E       AssertionError: assert 'job_failed' == 'credential_not_owned'`.

6 failed. The hunk collapses every named runtime handshake onto `job_failed`, which is the mapping the five criterion-16 kinds and AC 12 all share.

**Performed by:** C5 (2026-09-18)

**Note:** AC 3–5/7 use a frozen injected clock and assert wall time under five seconds — named `error_code` is reached without `time.sleep` and without waiting out `deadline_seconds`. AC 6 (`NeverAnswerAdapter`) jumps the injected clock past the deadline; `finished()` stays false; `poll()` returns `None`; no hang. AC 12 is listed here because it is the same finish_failed-then-return handshake with `credential_not_owned`, driven from a different workspace member against a `login` binding; recording double, seed directory mtime and bytes unchanged, `start` count zero.

**Note on a second mutation that was performed but is not this row's hunk.** Bypassing the account comparison in `run_runtime_job` (`if False and (account_id is None or str(account_id) != wanted)`) reddens the AC 12 demonstrator at `E       AssertionError: assert 'succeeded' == 'failed'` because `start` then runs. One row carries one hunk; the recorded hunk is the shared `_fail` mapping so all six named codes go red together. Injectable doubles only; no live `claude`.

---

### Criterion 17

**Text:** "A secret is held by reference and resolves only inside the component that presents it. A test configures a model credential and the OAuth client secret as references, drives one runtime request that needs the model credential and one sign-in that needs the client secret, and asserts that the runtime request as the adapter records it, the arguments of every tool call made during the run, the operation record, and the audit record contain neither a secret value nor a reference that resolves to one; a second check asserts the resolved value is never passed to a domain service." (`build-plan.md:155-163`)

**State:** complete

**Demonstrator:**
- `pytest:tests/postgres/test_identity.py::test_github_provider_completes_with_the_documented_shape_and_a_scoped_secret`
- `pytest:tests/postgres/test_identity.py::test_github_client_secret_is_reachable_only_through_its_own_scope`
- `pytest:tests/postgres/test_signin_secret_boundary.py::test_resolved_settings_repr_never_carries_a_reference_or_a_value`
- `pytest:tests/test_secrets.py::test_resolve_with_a_foreign_scope_is_denied`
- `pytest:tests/test_secrets.py::test_bytes_are_reachable_only_through_expose`
- `pytest:tests/postgres/test_runtime_matrix.py::test_model_credential_run_does_not_leak_secret_or_ref`
- `pytest:tests/postgres/test_runtime_matrix.py::test_ac8_leak_into_recorded_request_reddens_the_boundary`

**Mutation:**
```diff
diff --git a/packages/core/src/rheo_core/secrets/store.py b/packages/core/src/rheo_core/secrets/store.py
index 775a52f..fba35b8 100644
--- a/packages/core/src/rheo_core/secrets/store.py
+++ b/packages/core/src/rheo_core/secrets/store.py
@@ -52,11 +52,6 @@ class SecretStore:
             raise TypeError(
                 "resolve() requires a SecretScope from SecretStore.scope_for"
             )
-        if not scope.permits(ref):
-            raise SecretRefusal(
-                SECRET_SCOPE_DENIED,
-                f"scope {scope.component!r} may not resolve {ref}",
-            )
         if ref.backend is SecretBackend.FILE:
             raw = self._files.read(ref.id)
         else:
```

**Cost:**
- `pytest:tests/postgres/test_identity.py::test_github_client_secret_is_reachable_only_through_its_own_scope` — first observed failure line: `E       Failed: DID NOT RAISE SecretRefusal`.
- `pytest:tests/test_secrets.py::test_resolve_with_a_foreign_scope_is_denied` — first observed failure line: `E           Failed: DID NOT RAISE SecretRefusal`.

2 failed, 86 passed across `test_identity.py`, `test_signin_secret_boundary.py` and
`test_secrets.py` together. The new model-credential demonstrators stay green under this hunk:
the recording double never calls `SecretStore.resolve`.

**Performed by:** C2 (2026-09-16), C5 (2026-09-18)

**Note:** Complete. The sign-in half — the OAuth client secret held by reference, resolved only
inside the GitHub provider that presents it — remains the C2 demonstrators above. C5 adds the
model-credential half: `credential_kind=api_key` with `runtime.claude_cli.credential_ref =
secret://env/RHEO_ANTHROPIC_API_KEY`, a recording double that captures `RuntimeRequest` and
emits a fake `tool_call`, and assertions that the recorded request, tool-call digest, operation
row, audit records, and runtime tables contain neither the secret value nor a resolvable
`secret://` reference. `SecretValue.expose` is probed and must not appear in domain frames
(`rheo_core.operations`, `rheo_core.runtime`, `rheo_core.audit`, `rheo_core.identity`).

**Note on where the sign-in evidence actually lives, because the obvious file is not it.**
`tests/postgres/test_signin_secret_boundary.py` is named for this criterion but carries the
settings-repr half and a dispatcher log-line assertion, not the sign-in drive; the sign-in that
needs the client secret is in `tests/postgres/test_identity.py`. Recorded here so the next reader
does not conclude from the filename that the sign-in half is untested, and so a rename of either
file is caught by chunk 10's guard against this row.

**Note on the mutation's shape, and on the second one that was performed but is not recorded as
this row's hunk.** The recorded hunk deletes the scope check in `SecretStore.resolve`
(`packages/core/src/rheo_core/secrets/store.py:55-59`), which is the enforcement behind the
criterion's **first sentence** — "resolves only inside the component that presents it". With it
gone, any component's scope resolves any reference, and the two demonstrators above stop raising.
Eighty-six of eighty-eight tests stay green, so the red is precisely "a foreign scope resolved a
secret", not a broken sign-in. **C5 does not replace this hunk.**

A **second** mutation was applied, run and reverted on this row and is described here rather than
in the `Mutation` block, because one row carries one hunk and this one demonstrates less: in
`identity/provider_config.py:35`, `client_id=client_id` becomes `client_id=client_secret_ref`,
planting the `secret://` reference in a control-plane column that may not hold it. It reddens
`test_github_provider_completes_with_the_documented_shape_and_a_scoped_secret` at
`E                   AssertionError: ('identity_provider', 'client_id', 'secret reference leaked
into a control-plane row')`, 1 failed and 21 passed across `test_identity.py` and
`test_signin_secret_boundary.py`. It attacks the criterion's last clause — "nor a reference that
resolves to one" — and was the row's recorded hunk until a cold review pointed out that the
criterion's first sentence had none.

**C5 AC 8 leak mutation (performed, not this row's hunk).** Concatenating
`runtime.claude_cli.credential_ref` onto `RuntimeRequest.task` in `run_runtime_job` reddens
`test_model_credential_run_does_not_leak_secret_or_ref` at
`E       AssertionError: assert 'secret reference' is None`. The hunk is registered at
`tests/fixtures/ac8-credential-leak.diff` and `git apply --check`'d by
`tests/test_runtime_matrix_sources.py::test_ac8_leak_hunk_still_applies`. An in-process companion
(`test_ac8_leak_into_recorded_request_reddens_the_boundary`) monkeypatches
`build_runtime_request` to append only the `secret://` ref so the suite itself proves
the reference detector bites (not the secret-value detector).

**What still has no mutation, and why that is not a permission story.** The ~26 lines of boundary
assertions at `test_identity.py:226-252` — the resolved value reaches the token-exchange POST
body and no other request, URL, header, response or log line — are reddened by neither hunk. A
mutation for them would widen where the resolved *value* travels, and this session's permission
layer refuses edits that move a credential into a new sink; that was the stated reason for the
control-plane substitution, and it was too broad, because it did not cover the scope guard, which
this session permitted on the first attempt. The narrower true statement: the value-widening
route is refused here, the scope route is not, and the boundary assertions remain unproven by
mutation. Recorded, not inferred.

---

### Criterion 69

**Text:** "Configuration resolves from exactly three sources in a fixed precedence — package defaults, then deployment settings, then workspace and member overrides — and a key that no settings schema declares is refused at every source. For a key the operator's policy floor governs, a workspace override that would relax the deployment value is refused at write time naming the key, and an override already stored is clamped at read time from the moment the deployment value tightens. A test sets one key at each source and asserts the resolved value follows the precedence; writes a floored key looser than the deployment value and asserts the refusal names the key; and tightens the deployment value beneath an existing looser row and asserts the resolved value is the deployment value while the row is left in place." (`build-plan.md:211-220`)

**State:** complete

**Demonstrator:**
- `pytest:tests/postgres/test_settings_floor_e2e.py::test_tightening_the_deployment_value_clamps_an_existing_row_left_in_place`
- `pytest:tests/test_settings_floor.py::test_read_path_clamps_a_stored_loosening_row`
- `pytest:tests/test_settings_floor.py::test_tightened_deployment_value_neutralises_an_older_row`
- `pytest:tests/postgres/test_settings_floor_e2e.py::test_one_key_at_each_source_resolves_in_precedence_order`
- `pytest:tests/postgres/test_settings_floor_e2e.py::test_a_floored_write_looser_than_the_deployment_value_is_refused_naming_the_key`
- `pytest:tests/test_settings_floor.py::test_refusal_states_are_exactly_the_four_criterion_69_names`

**Mutation:**
```diff
diff --git a/packages/core/src/rheo_core/settings/resolver.py b/packages/core/src/rheo_core/settings/resolver.py
index ef7fc4b..ab9a8e0 100644
--- a/packages/core/src/rheo_core/settings/resolver.py
+++ b/packages/core/src/rheo_core/settings/resolver.py
@@ -156,7 +156,7 @@ def apply_floor(
     """
     match floor:
         case Floor.MIN:
-            return min(_as_int(deployment_value), _as_int(override_value))
+            return max(_as_int(deployment_value), _as_int(override_value))
         case Floor.UNION:
             base = _as_items(deployment_value)
             extra = tuple(x for x in _as_items(override_value) if x not in base)
```

**Cost:**
- `pytest:tests/test_settings_floor.py::test_read_path_clamps_a_stored_loosening_row` — first observed failure line: `E       AssertionError: assert 60 == 50` (the stored looser row is handed back instead of the deployment's tightened value), both parametrised cases.
- `pytest:tests/test_settings_floor.py::test_tightened_deployment_value_neutralises_an_older_row` — first observed failure line: `E       assert 90 == 60`.
- `pytest:tests/postgres/test_settings_floor_e2e.py::test_tightening_the_deployment_value_clamps_an_existing_row_left_in_place` — first observed failure line: `E       AssertionError: assert 60 == 30`.
- `pytest:tests/postgres/test_settings_floor_e2e.py::test_one_key_at_each_source_resolves_in_precedence_order` — first observed failure line: `E       AssertionError: assert 60 == 30` (the precedence clause: the workspace override no longer lands at its own value under the floor).

10 failed, 40 passed across `test_settings_floor_e2e.py` and `test_settings_floor.py` together.
Four of the row's six demonstrators went red; the two that stayed green are named in the notes
below.

**Performed by:** C2 (2026-09-16)

**Note:** the hunk inverts the `min` comparator inside `apply_floor`, the function `_apply_rows`
calls on **every read** for a floored key. That is the read-time clamp: with the comparator
inverted, an override already stored is handed back at its own looser value from the moment the
deployment value tightens, instead of being lowered to it. The row itself is untouched either
way, which is why "left in place" is not what breaks.

**Note on where the red lands in the e2e test, which is one assertion earlier than expected.**
`test_tightening_the_deployment_value_clamps_an_existing_row_left_in_place` fails at its line
168 (`assert _resolved(...) == 30`, the write-then-read step) rather than at line 170 (the
clamp-after-tighten step), because both steps go through the same comparator and the earlier one
aborts the test first. The clamp assertion in isolation is
`tests/test_settings_floor.py::test_read_path_clamps_a_stored_loosening_row`, which is listed
first in `Cost` for that reason: its `60 == 50` is the criterion's third clause failing with
nothing else in front of it.

**Note on the two demonstrators that stayed green, both read rather than assumed.**
`test_a_floored_write_looser_than_the_deployment_value_is_refused_naming_the_key` passes under
this hunk and is listed anyway. The refusal at write time is `is_looser`, a separate function
this hunk does not touch, so the criterion's two floor clauses are independently enforced rather
than sharing one comparator — worth knowing, since a single shared helper would have made one
mutation look like it proved both. `test_refusal_states_are_exactly_the_four_criterion_69_names`
also passes: it pins the four refusal-state strings and never resolves a value, so no comparator
change can reach it.

**Note on the parametrisation.** `test_read_path_clamps_a_stored_loosening_row` is parametrised
over `CASES` (`tests/test_settings_floor.py:70-103`) and `test_comparators` over a literal list
of tuples (`:261-275`), and in both the expected values (`clamped_looser=50`, and the rest) are
written as literals rather than derived from `Floor` or from `REGISTRY`. So this is not the
shape criterion 6's note warns about: shrinking a constant cannot silently remove a case here,
because the cases are not built from the constant under test.
`test_refusal_states_are_exactly_the_four_criterion_69_names` is the literal-list companion for
the refusal-state names and is listed for the same reason criterion 6's second demonstrator is.

---

### Criterion 20

**Text:** "Every tool the MCP façade exposes is namespaced and goal-level and reaches storage only by calling an application service. A test enumerates the registered tools and asserts that none accepts SQL, a table name, or a query fragment as an argument, and a check asserts that no file in the MCP façade imports a database driver or a repository directly." (`build-plan.md:183-187`)

**State:** complete

**Demonstrator:**
- `pytest:tests/test_mcp_boundary.py::test_registered_tools_accept_no_sql_table_name_or_query_fragment`
- `pytest:tests/test_mcp_boundary.py::test_mcp_imports_no_core_storage_or_db_driver`

**Mutation:**
```diff
diff --git a/packages/core/src/rheo_core/operations/registry.py b/packages/core/src/rheo_core/operations/registry.py
index 03d5ae6..f32d575 100644
--- a/packages/core/src/rheo_core/operations/registry.py
+++ b/packages/core/src/rheo_core/operations/registry.py
@@ -148,7 +148,7 @@ def _alias_names(model: type[BaseModel]) -> set[str]:
 
 def reserved_input_fields(model: type[BaseModel]) -> frozenset[str]:
     """The reserved names ``model`` would accept, by field name or alias."""
-    return frozenset(_alias_names(model) & RESERVED_INPUT_FIELDS)
+    return frozenset()
 
 
 @dataclass(frozen=True, slots=True)
diff --git a/tests/test_mcp_boundary.py b/tests/test_mcp_boundary.py
index 1efc9c1..138f53a 100644
--- a/tests/test_mcp_boundary.py
+++ b/tests/test_mcp_boundary.py
@@ -34,9 +34,22 @@ from pathlib import Path
 from typing import Final
 
 from pydantic import BaseModel
-from rheo_contracts import RESERVED_INPUT_FIELDS
+from rheo_contracts import RESERVED_INPUT_FIELDS, SafetyClass, ToolDeclaration
+from rheo_core.settings import CORE_ORIGIN
 from rheo_core.tokens.sets import TOOL_REGISTRY, register_core_tools
 
+
+class _ScratchSqlInput(BaseModel):
+    sql: str
+
+
+_SCRATCH_TOOL = ToolDeclaration(
+    name="scratch_sql_tool",
+    safety_class=SafetyClass.READ,
+    operation="core.workspace.status",
+    input_model=_ScratchSqlInput,
+)
+
 _REPO_ROOT = Path(__file__).resolve().parents[1]
 _MCP_ROOT = _REPO_ROOT / "apps" / "mcp"
 _FORBIDDEN_DRIVER_ROOTS = frozenset({"psycopg", "sqlalchemy", "asyncpg"})
@@ -119,6 +132,7 @@ def test_registered_tools_accept_no_sql_table_name_or_query_fragment() -> None:
     no tools — the vacuous pass this assertion is most likely to decay into.
     """
     register_core_tools()
+    TOOL_REGISTRY.register(_SCRATCH_TOOL, origin=CORE_ORIGIN)
     declarations = TOOL_REGISTRY.declarations()
     assert declarations, "the tool registry is empty; the enumeration proves nothing"
     offenders: dict[str, list[str]] = {}
```

**Cost:** `pytest:tests/test_mcp_boundary.py::test_registered_tools_accept_no_sql_table_name_or_query_fragment` — first observed failure line: `E       AssertionError: registered tool(s) accept a reserved argument: {'scratch_sql_tool': ['sql']}; workspace, actor and storage identity come from the context only`

**Performed by:** C4 (2026-09-16)

**Note: the hunk is two-part because no one-part version of it bites, and all three
one-part versions were run rather than reasoned about.** Disabling the shared
`reserved_input_fields` check is one half and registering a reserved-field-bearing tool is
the other; each alone leaves the enumeration green.

- **A tool declaring `query: str`, refusal live.** It registers cleanly — `query` is not one
  of the ratified twelve — and the enumeration correctly finds nothing. Observed: the live set
  became `['workspace_status', 'harness_get_note', 'scratch_sql_tool']` and the node passed.
- **A tool declaring `sql: str`, refusal live — two runs, because only one of them is what
  this row's own hunk produces.** Apply **only** the `tests/` half of the fenced hunk above,
  leaving `reserved_input_fields` intact, and the node **fails**, at the registration call and
  not at the enumeration: `E rheo_core.operations.refusals.RegistrationRefused:
  scratch_sql_tool: input model declares reserved field(s) ['sql']; workspace, actor and
  storage identity come from the context only`. That is the refusal working — the enumeration
  assertion never executes, so it catches nothing and proves nothing about the tool. To watch
  the enumeration *itself* under this variant, the registration has to be wrapped so execution
  reaches it; done, and then the refusal is raised and swallowed, the live set is unchanged at
  `['workspace_status', 'harness_get_note']`, and the node **passes**.

  **An earlier revision of this row reported only the second run's `passed`, under a heading
  describing the first run's hunk.** A reviewer applied the hunk and got `failed`. Both runs
  are now written out with which hunk produces which, because a row whose recorded observation
  does not reproduce is the exact failure this matrix exists to prevent.
- **Disabling the shared check alone, nothing registered.** Observed: node passed. This is the
  one that looks sufficient and is not — disabling a refusal changes nothing already
  registered, and neither shipped tool declares a reserved field
  (`packages/core/src/rheo_core/tokens/sets.py`: `_WorkspaceStatusToolInput` declares no
  fields, `_HarnessNoteRefToolInput` declares only `ref`).

**Note: the enumeration does not call `reserved_input_fields`, and that is what makes the
hunk able to bite at all.** That function is what registration refuses with. Had the
enumeration reused it, the first half of this hunk would have blinded the assertion at the
same moment it removed the refusal, and the row would have recorded a green run as proof.
Instead the enumeration reads the ratified `RESERVED_INPUT_FIELDS` and intersects it with the
argument names each tool's published JSON schema exposes — a different instrument for the same
claim, and closer to the criterion's own words, since "accepts ... as an argument" is about
what a client may send.

**Note: the second demonstrator's own mutation, performed separately and not recorded as this
row's hunk** (the grammar carries one). `import sqlalchemy` added to
`apps/mcp/src/rheo_app_mcp/transport.py` turned
`test_mcp_imports_no_core_storage_or_db_driver` red with `E       AssertionError: apps/mcp
imports a forbidden module: {'apps/mcp/src/rheo_app_mcp/transport.py': ['sqlalchemy']}` —
which names both the file and the import it caught, so a future failure tells its reader what
to go and look at rather than only that something is wrong. Reverted. The scan stays green
against the enlarged package: `transport.py` reaches storage only through the same `dispatch`
call `tools.py` already made.

**Note: the first clause has no demonstrator in this grammar, and is recorded as such rather
than covered over.** "Namespaced and goal-level" is a property of the two tool names
(`workspace_status`, `harness_get_note`) that no test asserts; `runtime-and-mcp.md` fixes the
convention (`<module>_<verb>[_<noun>]`) and nothing mechanically enforces it. "Reaches storage
only by calling an application service" is enforced structurally rather than by assertion — a
`ToolDeclaration` has no handler field at all, so a tool has nothing to reach storage *with*
except the operation it names. Both are true; neither is a pytest node.

---

### Criterion 18

**Text:** "Every registered MCP tool and every registered service operation declares exactly one safety class, and either one registered without a class fails registration at startup, naming what was registered. A test registers one tool and one service operation with no declared class and asserts startup fails naming each. A second check starts the application under the production profile and asserts that its registration set contains no tool, operation, consumer, or identity provider registered by the test harness and no operation in the external-side-effect or financial class; the check runs in continuous integration from this phase onward." (`build-plan.md:164-171`)

**State:** complete

**Demonstrator:**
- `pytest:tests/test_production_registration.py::test_a_production_start_registers_nothing_from_the_test_harness`
- `pytest:tests/test_production_registration.py::test_the_identity_provider_clause_tracks_what_is_configured`
- `pytest:tests/test_production_registration.py::test_startup_refuses_a_classless_registration_naming_it`
- `pytest:tests/test_production_registration.py::test_registering_an_operation_with_no_safety_class_is_refused_naming_it`
- `pytest:tests/test_contracts.py::test_tool_registration_refuses_a_declaration_with_no_safety_class`
- `pytest:tests/test_contracts.py::test_tool_declaration_requires_a_safety_class`
- `pytest:tests/test_contracts.py::test_operation_declaration_requires_a_safety_class`
- `ci:python / Pytest`

**Mutation:**
```diff
diff --git a/apps/core/src/rheo_app_core/startup.py b/apps/core/src/rheo_app_core/startup.py
index af0ff97..2fe691c 100644
--- a/apps/core/src/rheo_app_core/startup.py
+++ b/apps/core/src/rheo_app_core/startup.py
@@ -125,6 +125,26 @@ def run_startup() -> StartupReport:
     # call ran anywhere, ``TOOL_REGISTRY`` was empty in a deployed process and
     # ``agent_default`` named nothing, so the MCP facade had no tools to list.
     register_core_tools()
+    from pydantic import BaseModel
+    from rheo_contracts import Idempotency, OperationDeclaration, Role, SafetyClass
+
+    from rheo_core.settings import TEST_HARNESS_ORIGIN
+
+    class _ProbeInput(BaseModel):
+        pass
+
+    REGISTRY.register(
+        OperationDeclaration(
+            name="harness.probe.get",
+            safety_class=SafetyClass.READ,
+            roles=frozenset({Role.OWNER}),
+            input_model=_ProbeInput,
+            output=_ProbeInput,
+            idempotency=Idempotency.NONE,
+        ),
+        lambda ctx, uow, model_input: _ProbeInput(),
+        origin=TEST_HARNESS_ORIGIN,
+    )
     # ``deletions`` is this process's half of a module's erasure cascade, and the
     # value is the process-global registry because that is the one the deletion
     # coordinator resolves against — passing any other instance would register hooks
diff --git a/packages/core/src/rheo_core/operations/registry.py b/packages/core/src/rheo_core/operations/registry.py
index e33bd79..dd1e27a 100644
--- a/packages/core/src/rheo_core/operations/registry.py
+++ b/packages/core/src/rheo_core/operations/registry.py
@@ -91,14 +91,7 @@ def module_id_for_origin(origin: str) -> str:
 
 def check_origin_profile(origin: str) -> None:
     """Refuse the test-harness origin outside ``profile = test``."""
-    if origin == TEST_HARNESS_ORIGIN:
-        profile = current_profile()
-        if profile != "test":
-            raise RegistrationRefused(
-                origin,
-                f"origin {origin!r} is accepted only under profile = test (resolved "
-                f"profile is {profile!r})",
-            )
+    return
 
 
 def check_origin(origin: str, module_id: str, *, name: str) -> None:
```

**Cost:**
- `pytest:tests/test_production_registration.py::test_a_production_start_registers_nothing_from_the_test_harness` — first observed failure line: `E       AssertionError: stdout:`, immediately followed by the child's own captured stderr, which is where the naming happens: `AssertionError: a production start registered what it must not:`, then `operation 'harness.probe.get' was registered by origin 'test_harness'` and `operation 'harness.probe.get' belongs to module 'harness'`, then `E       assert 1 == 0`.

1 failed.

**Performed by:** C4 (2026-09-16), C5 (2026-09-16), 1a1 P8 (2026-09-21)

**Note on why the hunk is two-part.** Neither half alone bites. Dropping
`check_origin_profile`'s gate changes nothing on its own, because no production composition
root calls `register_harness()` — the gate is what would refuse it *if* something did.
Registering a harness-origin operation from `run_startup()` on its own is refused by the live
gate, so the process never reaches the enumeration. Together they are the actual scenario the
criterion's third sentence is about: a harness registration surviving into a started
production process. That is the shape of row 20's hunk too, and for the same reason.

**Note on the three other mutations run against this row, each applied, watched, and reverted
on 2026-09-16.** They are not this row's fenced hunk — one hunk per row — but they were run
rather than reasoned about, and each reddens a different clause:

- **A class-less tool added to `CORE_TOOLS`** (`ToolDeclaration.model_construct(name=
  "no_class_tool", ...)`, `packages/core/src/rheo_core/tokens/sets.py`). Red at
  `register_core_tools()` inside `run_startup()`:
  `rheo_core.operations.refusals.RegistrationRefused: no_class_tool: declares no safety class`.
  This is the criterion's first sentence demonstrated through a *real startup* rather than
  through a private registry, and it is also what proves the wiring: with
  `register_core_tools()` absent from `run_startup()` this hunk would have reddened nothing.
- **`register_core_tools()` removed from `run_startup()`**, the anti-vacuous control. Red with
  `the tool registry is empty; register_core_tools() did not run during startup, so the tool
  half of this enumeration would pass over nothing` — so the tool half of the enumeration
  cannot pass by walking an empty set.
- **`OperationRegistry.register`'s `declares no safety class` refusal removed**
  (`packages/core/src/rheo_core/operations/registry.py`). Red at
  `test_registering_an_operation_with_no_safety_class_is_refused_naming_it` with
  `E                   AttributeError: 'OperationDeclaration' object has no attribute
  'safety_class'` — no `RegistrationRefused` raised at all, which is the absence the removal
  was meant to prove rather than a second refusal standing in for the first. This one was
  applied and observed before the identical-looking edit to `register_core_tools()` was refused
  by the permission classifier, so the refusal below is about that specific call and not about
  the class of edit.

**Note on the two chunks and which clause each closed.** C4 shipped
`ToolDeclaration.safety_class` and turned tool registration into a path that can refuse; its
two `test_contracts.py` nodes are the tool half of the criterion's first two sentences. C5
wrote the production-profile check, wired `register_core_tools()` into startup, and added the
operation half of that same test — which had no demonstrator anywhere at this run's base:
`registry.py`'s refusal has shipped since run 0b1 and nothing registered a class-less
operation against it.

**Note on "asserts *startup* fails", which the unit-level nodes do not demonstrate.** Both
`test_contracts.py` nodes and this row's own
`test_registering_an_operation_with_no_safety_class_is_refused_naming_it` build a **private**
registry the shipped startup never touches, so each proves that a registry refuses — not that a
started application does. A startup that filtered a class-less declaration out, or registered
through some other path, would leave all three green.
`test_startup_refuses_a_classless_registration_naming_it` closes that: it injects a class-less
declaration into the tuple the core's own registration function reads — `CORE_OPERATIONS` for
the operation half, `CORE_TOOLS` for the tool half — then drives the real `run_startup()` in a
subprocess and requires it to refuse, naming what was registered. Demonstrated on 2026-09-16 by
giving the injected operation a real safety class, so startup had nothing to refuse: red with
`startup completed with a class-less operation registered; it did not refuse 'core.probe.act'`,
while the tool case stayed green because only the operation half was mutated. Reverted. This
row's three unit-level nodes are kept as the fast companions, not as the answer to that
sentence.

**Note on one mutation this row does not carry because a permission layer refused it.** The
first attempt at the above was to make `register_core_tools()` *skip* a class-less declaration
rather than register it, which is the more direct rendering of "the test would fail if startup
filters the declaration". The edit was refused by this session's own permission classifier,
which reads removing a registration guard as tampering — the same shape that refused 0c2's
M14a on criterion 14's row. It was not performed, and no inference about it is recorded. The
probe-side mutation above was run instead and produces the same signal from the other
direction: startup is given nothing to refuse rather than being taught not to refuse.

**Note on the CI clause, which is satisfied by an existing step rather than a new one.**
"The check runs in continuous integration from this phase onward" is met by
`.github/workflows/repository-checks.yml`'s `python` job: its `Pytest` step runs `uv run
pytest`, whose `testpaths = ["tests"]` collects `tests/test_production_registration.py`, and a
red result fails that step and the workflow. The outer node shells out to a nested `uv run`,
which is already proven to work inside that step by
`tests/postgres/test_migrations.py::test_suite_fails_fast_when_the_cluster_is_unreachable`. A
dedicated step was considered and not added: it would start a second production process, run
the same control-chain migration again, and carry no signal the existing step does not.

**Note on the one clause with no origin to filter on, and how far it is checkable anyway.**
Identity providers are "registered" by `sync_providers()` upserting `control.identity_provider`
rows from the resolved deployment settings, and that table has no origin or profile column —
unlike operations, resolvers and tools, which all pass `check_origin_profile`. Provenance
therefore cannot be read off a row. What can be checked, and is, is that the rows present after
startup are **exactly** the set this process's own resolved settings would have produced; a row
this startup did not write fails that equality whatever id it carries. Demonstrated on
2026-09-16 by planting a `provider_id = 'github'` row into the child's control database before
the enumeration, which is precisely the case an allowlist of shipped ids would have waved
through: red with `identity provider rows ['github'] are not the set this process's own
settings produce ([]); a provider row this startup did not write is in the control plane`.
Reverted. The allowlist clause is kept beside it so an *unknown* id is still named as unknown.

**That equality was, at first, only ever evaluated in the branch where it could not fail, and
the fix is `test_the_identity_provider_clause_tracks_what_is_configured`.** A cold review probe
set `sync_providers` to a no-op and the clause stayed **green** — `identity_providers=0`, exit
0, `PROFILE=production` — because the child strips every `RHEO_*` variable and gets an empty
data root, so `identity.providers.github.enabled` always resolved false, the expectation was
always `[]`, and the equality compared two empty lists. The other three clauses each have a
guard that fires when the thing they enumerate is missing entirely; this one had none, and the
comment claiming its rule was "a deliberate second statement" that "can disagree" was true only
in a branch that never ran. That is a claim and its check sharing a failure mode, landing on
the comment itself.

The clause is now run twice in one node against the same code, differing only in deployment
settings: unconfigured it must see nothing, configured (`RHEO__identity__providers__github__
enabled=true` plus a placeholder client id — nothing reaches GitHub, startup only upserts the
row) it must see exactly `{github}`, and the child reports the expected count beside the actual
one so the populated branch is proved live rather than inferred from a row existing. **Verified
by the probe that found the gap:** with `sync_providers` returning immediately, the new node is
red with `identity provider rows [] are not the set this process's own settings produce
(['github']); a provider row this startup did not write is in the control plane`. Reverted.
The original enumeration node stays green under that same mutation, correctly — with nothing
configured and nothing synced, both sets really are empty — which is exactly why the paired
node had to exist.

**The residual gap, which this does not close:** in a deployment where GitHub is genuinely
configured, a planted `github` row is indistinguishable from the real one, because both satisfy
the equality. Closing that needs a provenance column on `control.identity_provider` — a schema
change, a migration, and an edit to `sync_providers` — all outside this chunk's declared paths,
and escalated to the Conductor rather than widened into here.

**Note on the external/financial clause, which ranges over ten real operations and can never
match one.** Nothing above `MUTATE` exists in shipped code — the production registry holds ten
operations, every one `read` or `mutate`, and C6/C7's upper-class fixtures are harness-registered
and profile-gated. So the predicate is evaluated ten times and has never been seen to fire,
which is the position the consumer clause was in, and it gets the same instrument rather than a
sentence excusing it. The operation scan is a named function applied both to the production
registry and to one built to hold a single `EXTERNAL` operation beside a `READ` one, where it
must flag the first and only the first. Demonstrated on 2026-09-16 by narrowing the predicate to
`DESTRUCTIVE` so it could no longer match `EXTERNAL`: red with `the external/financial clause's
own positive control did not fire: an EXTERNAL operation beside a READ one produced [], not
exactly one problem naming the EXTERNAL one, so the same predicate applied to the production
registry would not catch one there either`. Reverted.

**Note on a fact this row's headline does not say, which is safe and worth stating anyway.**
This chunk's `register_core_tools()` call puts a tool named `harness_get_note` into a production
process's tool registry for the first time — the child reports `tools=2`, `workspace_status` and
`harness_get_note` — and the assertion's tool loop can never flag it, because
`register_core_tools()` registers both under `CORE_ORIGIN` by design. The row's headline says a
production start registers "no tool … from the test harness", so a reader could reasonably
expect that registry to hold no `harness_*` entry at all. **It is not a defect and needs no
fix**, for two independently verified reasons: `agent_default()` in the production child returns
`['core.workspace.status']` only, because `harness.note.get` never reaches `REGISTRY` outside
`profile = test`; and `tools.py`'s `list_tools` filters on `_visible(ctx, tool.operation)`, which
needs `harness` in `ctx.enabled_modules`, which no production deployment produces. The
assertion checks the `agent_default` intersection for exactly this reason. But the reasoning
that makes it safe is one comment deep, so it is written here rather than left to be
rediscovered by whoever next reads that registry directly.

**Note on the consumer clause, which is checked by a positive control rather than by a count.**
Release one registers no production consumer, so the worker's `CONSUMERS` is legitimately
empty and the loop over it passes over nothing. Requiring it to be non-empty would be a guard
that is red against correct code today. The predicate is instead a named function applied
twice: to the worker's registry, and to a throwaway registry holding one harness subscription
and one core subscription, where it must find the first and not the second. Demonstrated on
2026-09-16 by pointing the predicate at a module id that matches nothing: red with `the
consumer check's own positive control did not fire, so the predicate applied to the worker's
registry would not catch a harness consumer there either`. Reverted.

---

### Criterion 19

**Text:** "A headless or channel-mediated request that reaches a confirmation requirement receives an explicit approval-required state and nothing else happens. A test invokes the destructive-class fixture operation through an MCP token session and asserts that the response is an approval-required state carrying an approval identifier, distinct from success and from failure, returned within the configured deadline; that the fixture recorded no request; and that no operation record reports success. The test then approves through the approval operation as an authenticated actor and asserts the fixture executes once and a durable approval record exists carrying the payload digest, readable through a supported operation; and it requests a standing grant naming the destructive class and asserts the grant is refused at grant time." (`build-plan.md:172-182`)

**State:** complete

**Demonstrator:**
- `pytest:tests/postgres/test_approvals.py::test_criterion_19_end_to_end_through_an_mcp_token_session`
- `pytest:tests/postgres/test_approvals.py::test_revoking_the_gated_actors_role_refuses_execution_and_records_it`
- `pytest:tests/postgres/test_approvals.py::test_revoking_the_gated_token_refuses_approved_execution`
- `pytest:tests/postgres/test_approvals.py::test_revoking_the_approving_actors_role_refuses_execution_too`
- `pytest:tests/postgres/test_approvals.py::test_a_refusing_guard_stops_the_effect_and_a_permitting_one_does_not`
- `pytest:tests/postgres/test_approvals.py::test_the_core_attaches_two_guards_always_and_the_third_only_with_a_subject`
- `pytest:tests/postgres/test_approvals.py::test_a_destructive_dispatch_is_held_and_the_fixture_records_nothing`
- `pytest:tests/postgres/test_approvals.py::test_an_external_dispatch_is_held_and_the_sink_sends_nothing`
- `pytest:tests/postgres/test_approvals.py::test_approving_runs_the_fixture_once_and_the_record_carries_the_digest`
- `pytest:tests/postgres/test_approvals.py::test_two_concurrent_approvals_execute_the_destructive_fixture_once`
- `pytest:tests/postgres/test_standing_grants.py::test_a_grant_naming_an_upper_class_operation_is_refused_naming_it`
- `pytest:tests/postgres/test_standing_grants.py::test_a_refused_grant_writes_no_row_at_all`
- `pytest:tests/postgres/test_standing_grants.py::test_a_grant_naming_only_lower_class_operations_is_accepted`

**Mutation:**
```diff
diff --git a/packages/core/src/rheo_core/approvals/gate.py b/packages/core/src/rheo_core/approvals/gate.py
--- a/packages/core/src/rheo_core/approvals/gate.py
+++ b/packages/core/src/rheo_core/approvals/gate.py
@@ -421,16 +421,6 @@ def execute_approved(
     # only a rebuild failure they did *not* catch — a tampered ``purpose`` column, a
     # missing provenance row — reaches the raise below, where a rollback is the right
     # answer because nothing was decided.
-    refusal = run_guards(
-        ctx if isinstance(held_caller, Refusal) else held_caller,
-        uow,
-        approval=approval,
-        model_input=model_input,
-        now=now,
-        guards=core_guards_for(declaration),
-    )
-    if refusal is not None:
-        return _record_guard_refusal(uow, approval=approval, refusal=refusal, now=now)
     if isinstance(held_caller, Refusal):
         raise OperationRefused(held_caller.state, str(held_caller))
     output = operation.handler(
```

**Cost:**
- `pytest:tests/postgres/test_approvals.py::test_revoking_the_gated_actors_role_refuses_execution_and_records_it` — first observed failure line: `E       AssertionError: OperationOutcome(state='membership_missing', result=None, error=OperationError(error_code='membership_missing', ...`.
- `pytest:tests/postgres/test_approvals.py::test_revoking_the_approving_actors_role_refuses_execution_too` — first observed failure line: `E       AssertionError: assert 'executed' == 'refused'`.
- `pytest:tests/postgres/test_approvals.py::test_revoking_the_gated_token_refuses_approved_execution` — first observed failure line: `E       AssertionError: assert 'executed' == 'refused'`.
- `pytest:tests/postgres/test_approvals.py::test_a_refusing_guard_stops_the_effect_and_a_permitting_one_does_not` — first observed failure line: `E       AssertionError: assert 'executed' == 'refused'`.

4 failed, 41 passed across `tests/postgres/test_approvals.py` and `tests/postgres/test_standing_grants.py`.

**Performed by:** C6 (2026-09-16), C7 (2026-09-17), 1a1 P1 (2026-09-20)

**What the hunk attacks, and why this one.** It deletes the guard-check step from
`execute_approved` — the `run_guards` call and the branch that records its refusal — leaving the
binding check, the handler, the approval's `executed` transition and the audit write exactly where
they were. So the criterion's first three sentences stay green under it: the call is still held,
the fixture still records nothing, the approval still carries the digest, the grant is still
refused at grant time. What goes red is the fourth thing, which is the half C7 exists to add: an
approval released by a person whose role has since been revoked **executes anyway**. The red is
therefore "a revocation between approval and execution does not bite", which is R3 item 4's whole
content and the failure a caller would never see — the effect lands, the record says `succeeded`,
and nothing anywhere reports that the permission was gone.

**Note on which demonstrators the hunk does and does not redden, checked node by node rather than
inferred.** Four of the thirteen go red and each was re-run **individually** at recapture, not read
off the aggregate. The other nine stayed green and were read rather than assumed: the four C6
demonstrators (`..._is_held_and_the_fixture_records_nothing`, `..._sink_sends_nothing`,
`..._runs_the_fixture_once_and_the_record_carries_the_digest`,
`..._execute_the_destructive_fixture_once`) exercise the gate and the binding, which this hunk
does not touch; the three `test_standing_grants.py` demonstrators are about grant time, which has
no guard in it at all; `test_the_core_attaches_two_guards_always_and_the_third_only_with_a_subject`
asserts `core_guards_for`, which the hunk leaves whole and merely stops calling; and
`test_criterion_19_end_to_end_through_an_mcp_token_session` never revokes anything, so its
execution is one the guards permit.

**Recaptured in 1a1's P1, and two things about it changed rather than one.** P1 rewrote the region
this hunk cuts — `execute_approved` now rebuilds the original held caller's context before the
guards and runs both them and the handler under it — so the stored diff no longer applied and was
regenerated against the new code. It still cuts exactly the guard step and nothing else. The
recapture also corrected an undercount: the node counts here said three red of twelve while
thirteen were listed, and `..._revoking_the_gated_token_refuses_approved_execution` belonged in the
red column all along — the hunk removes the guard that catches a revoked token just as it removes
the one that catches a revoked membership. The gated-actor node's first failure line changed with
the code: with the guards gone, a revoked membership is now caught one layer earlier, by the
context rebuild refusing `membership_missing`, so that node reddens on the refusal rather than on
`'executed' == 'refused'`. It still reddens, and still for the reason the row is about.

**That last one is the honest limit of this row's single hunk, and it is stated rather than
smoothed over.** The criterion's own named test is green under the mutation that removes the
guards, because the criterion's text does not mention guards — they are AC 11's clause, reached
through the same flow. A second hunk covering the criterion's own sentences would be the
dispatcher's class branch, which criterion 14's and C6's own demonstrators already pin from the
other side. One hunk, the three reddened nodes named above, and this note saying what it does not
reach.

**Note on the two guards that are not demonstrated end to end, and why that is not a gap to
close later.** `WindowGuard` and `RecordStateGuard` are unit-tested against the classes directly
(`test_the_window_guard_refuses_outside_its_window_and_permits_inside_it`,
`test_the_record_state_guard_refuses_a_gone_or_moved_subject`,
`test_the_record_state_guard_permits_a_live_matching_subject`) and are deliberately **not** listed
as demonstrators of this criterion, because neither is reachable end to end on this tree.
`WindowGuard` cannot fire through a dispatch at all: the binding tuple carries the window too and
is checked first, so an expired approval refuses `invalid_approval` before the guards run.
`RecordStateGuard`'s revision branch has no honest staging either — `harness.note` is the only
resolvable record type and it is immutable, reporting the constant revision 1, so a dispatch-level
revision mismatch would need a new mutable record type or a tampered
`core.approval.subject_revision`, which would be a test about tampering. Both guards were
mutation-verified in their own right (inverting `WindowGuard`'s comparison reddens its three
window cases; returning `None` before `RecordStateGuard`'s resolve reddens all three of its
refusal branches and leaves its live-subject control green).

**Note on the two guards this run does not build.** `docs/architecture/confirmation-and-safety.md`
§ Execution guards lists five. `ContactPermissionGuard` is added by Leads and `DestinationGuard`
by the connector owning a destination — both are *domain* guards a module supplies through its own
declaration, and `modules/**` and `connectors/**` are denied paths for run 0c3 because neither a
module nor a connector exists in phase one. There is no module to add either, so neither has a
class, a seam or an attachment test, and this row claims nothing about them.

**Note on what "within the configured deadline" is asserted against.** There is no response-deadline
key anywhere in the settings schema; the only configured time bound the approvals design has is the
execution window, `approvals.default_window_seconds` clamped by `approvals.max_window_seconds`,
which `rheo_core.approvals.gate.window_seconds` resolves per workspace. The test reads that
function and asserts the held call returned inside it, so a deployment that changes the setting
changes the assertion with it — which is what AC 5 asks for ("asserted against the deadline
setting rather than a wall-clock constant chosen by the test"). A hold that outlived the window it
was minting would hand back an approval that had already expired, so the bound is meaningful as
well as setting-derived.

---

### Criterion 21

**Text:** "A workspace export produces an artifact that a restore reads back into an empty
deployment, after which the restored workspace's composition, configuration versions, module
schema versions, and records match the original, verified by comparison rather than by inspection.
A pending approved action is restored in a state that requires a fresh approval before it can
execute: the test approves the destructive-class fixture operation, exports before it runs,
restores, and asserts the restored action is refused until approved again and that the fixture
recorded nothing in between. *(Scenario: Export and restore; FR 52.)*"
(`build-plan.md:188-194`)

**State:** complete

**Demonstrator:**
- `pytest:tests/postgres/test_export_restore.py::test_restore_matches_source_by_workspace_digest`
- `pytest:tests/postgres/test_export_restore.py::test_approved_action_restore_requires_fresh_approval_and_executes_nothing`

**Mutation:**
```diff
diff --git a/packages/core/src/rheo_core/exports/artifact.py b/packages/core/src/rheo_core/exports/artifact.py
index dbcd726..7d9ca79 100644
--- a/packages/core/src/rheo_core/exports/artifact.py
+++ b/packages/core/src/rheo_core/exports/artifact.py
@@ -393,6 +393,7 @@ def digest_categories(
         for name, body in serialised_categories(
             snapshot, skip_operation_id=skip_operation_id
         ).items()
+        if name != "audit"
     }
 
 
@@ -774,7 +775,6 @@ def _import_approvals(connection: Connection, body: bytes) -> set[UUID]:
     for row in _jsonl(body, APPROVALS_NAME):
         values = _decoded(row, approval_tables.approval.c.keys())
         values.update(
-            state=approval_tables.REQUIRES_REAPPROVAL,
             approved_by_kind=None,
             approved_by_id=None,
             approved_at=None,
```

**Cost:** The first mutation makes the full-sequence test report `approved` where
`requires_reapproval` is required. The second makes the digest-comparison test report unequal
category maps.

**Performed by:** C8 (2026-09-17), 1a1 P12 (2026-09-21)

**Note:** 1a1 P12 recaptured the hunk, unchanged in substance. A12's source snapshot gave
`digest_categories` an `ExportSnapshot` parameter in place of its `Connection`, which moved the
first hunk's context lines; both mutations were reapplied to the new code, watched go red on both
demonstrators (`assert 'approved' == 'requires_reapproval'`, and the digest maps unequal), and
reverted.
