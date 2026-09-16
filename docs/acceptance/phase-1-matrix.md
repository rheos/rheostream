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

**Cost:** `pytest:tests/test_data_root.py::test_rheo_local_is_accepted_only_when_named_explicitly` — first observed failure line: `E               rheo_core.storage.data_root.DataRootRefusal: data_root_in_checkout: data root <pytest tmp>/checkout/.rheo-local is inside the source checkout <pytest tmp>/checkout; only <pytest tmp>/checkout/.rheo-data is allowed, and only when RHEO_DATA_ROOT names it explicitly` (the tmp prefix is pytest's per-run directory). `tests/test_ignored_artifacts.py::test_checkout_local_artifacts_are_ignored` stays green under this hunk; see the note.

**Performed by:** C1 (2026-09-16)

**Note:** the criterion has two halves and only one of them is mutable from inside this run's
`allowed_paths`. The half this hunk breaks is "the checkout-local fallback directory": move
the directory the deployment writes into and the ignore rules no longer match anything it
produces, which is the criterion's failure in its most direct form. The other half, "matched
by the repository ignore rules", is enforced by `.gitignore` alone, and `.gitignore` is not a
declared path for run 0c3, so no mutation of it was attempted. That demonstrator is listed
because it is real and is what the criterion's own "verified by a test that creates one of
each" sentence points at; its mutation is left for a run whose scope reaches the ignore file.

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
-    return `${config.scheme}://${config.base_host}${joinPrefix(surfacePath, path)}`;
-  }
-  // Subdomain mode: every surface drops its prefix once it has its own host —
-  // except `identity`, whose `fixed_path` holds `/auth/*` on every application
-  // host in both modes.
-  const prefix = spec.fixed_path === true ? surfacePath : "";
-  const host = `${spec.host}.${config.base_host}`;
-  return `${config.scheme}://${host}${joinPrefix(prefix, path)}`;
+  return `${config.scheme}://${config.base_host}${joinPrefix(surfacePath, path)}`;
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
-        return f"{config.scheme}://{config.base_host}{_join_prefix(spec.path, path)}"
-    prefix = spec.path if spec.fixed_path else ""
-    host = f"{spec.host}.{config.base_host}"
-    return f"{config.scheme}://{host}{_join_prefix(prefix, path)}"
+    return f"{config.scheme}://{config.base_host}{_join_prefix(spec.path, path)}"
 
 
 def identity_path(config: RoutingConfig, path: str) -> str:
```

**Cost:**
- `ci:web / Routing-literal gate (criterion 22)` — first observed failure line: `Hard-coded route literal: apps/web/src/components/logout-form.tsx`, exit status 1.
- `ci:web / Vitest (subdomain mode)` — first observed failure line: `AssertionError: expected 'example.test' to be 'auth.example.test' // Object.is equality`. 15 tests failed.
- `ci:web / Vitest (path mode)` — first observed failure line: `AssertionError: expected 'example.test' to be 'auth.example.test' // Object.is equality`. 5 tests failed.
- `pytest:tests/test_routing.py::test_the_identity_callback_is_the_documented_url` — first observed failure line: `E       AssertionError: assert 'https://example.test/login' == 'https://circ...le.test/login'`. 9 failed, 34 passed.

**Performed by:** C1 (2026-09-16)

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
