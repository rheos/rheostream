# Vertical-slice spike: where the specification and the code disagree

Run 0v built one narrow vertical slice — a throwaway module, one record type, one
HTTP route, one page, and a driver that runs the whole thing end to end — straight
through the architecture ratified in `docs/architecture/`. It was built four runs
before the build plan otherwise reaches its first domain record.

The code was never the point. `modules/spike/**`, the spike web surface and the
driver are deleted at 0c0's branch cut. The point was to find out where the
ratified documents and the running code disagree while that is still cheap, and
this note is what survives.

Fifty-one findings. Each carries its evidence — a `file:line` citation wherever there
is a line to cite — one of three dispositions, and the run that should act on it.

## How to read this

**The three dispositions.**

- **amend the spec** — a ratified architecture document says something that is not
  true of the code, or does not say something a builder needs.
- **amend the plan** — the documents are right and the build plan has no run that
  does the work, or has assigned it somewhere it cannot be done.
- **accept** — real, understood, and correct to leave as it is for release one.

**F1–F22 came from the design pass**, before a line of this run's own code existed,
and are reproduced here verbatim. They were written against `main` at `7724ff9`
with real line numbers, and their anchors were re-verified against the tree. Their
provenance, since it varies: F1–F12 came from the first design pass, F13–F18 from
its first revision, F19–F20 from its second, and F21 and F22 from the prompts
review and the two cold-review rounds after it. None of them was found during the
build. A finding found by reading is still a finding; this run's deliverable is the
list, not its provenance.

**F23–F54 came from the build, and from the independent verification of it.** They
were measured against this run's own branch, which is cut from `main` at `8f243e5`.
That is a different base from the one F1–F22 were written against: `main` moved on
while this run was paused, so a line number quoted in a design-pass finding and one
quoted in a build finding are not always read against the same tree. They are
ordered by consequence rather than by when they were found.

**F1–F22 cite working documents that are not in this repository.** Reproducing them
verbatim means keeping references like `spec.md`, `plan.md`, numbered prompts, and
internal review identifiers exactly as written. Those named the run's own working
files, which are not published. The citations that matter to a reader here are the
ones pointing into this repository — file paths, line numbers, `docs/architecture/`
documents, issues and pull requests — and those resolve normally. The rest is kept
because removing it would strip the evidence that verbatim exists to preserve.

**Confirmations are recorded under the finding they confirm, not filed again.**
Several of the design-pass findings were independently reproduced against running
code while building; F4, F18, F20, F21 and F22 each carry that confirmation under
their own number rather than appearing a second time with a new one.

**Findings already routed elsewhere say so and are not re-filed as new work.**
F7 is open PR #22's documentation half; F9 and F10 belong to issue #37 and to
design decision CF1.

## Read these first

- **F23** — after this run's own fix, a handler that commits through
  `uow.connection` still persists its write, and the operation reports `failed`. A
  caller that retries on `failed` writes twice. It falsifies F1's own prediction
  about the same code.
- **F24** — `read_only()` is derived from the process-wide operation registry, so
  installing any module widens what the `read_only` package set means for every
  token issued afterwards, in every workspace of that deployment, and the widening
  is written into the token's rows as granted.
- **F25** and **F26** — two statements about running code that are simply false:
  the OpenAPI document is not generated at startup and cannot be, and the
  reference topology's single process is load-bearing in a way nothing states,
  failing silently rather than loudly when it is split.
- **F29** — the fourth instance of F21's shape in one run: a requirement and the
  thing that must satisfy it stated in different places, with nothing connecting
  them.
- **F46** — the container image build runs in neither of the two places that could
  prove it: not on a developer's machine here, and not in CI.

## Findings from the design pass (F1–F22)

Reproduced verbatim.

### F1 — a handler *can* commit, and its write persists.

`packages/core/src/rheo_core/storage/backend.py:131` exposes `commit()` publicly and
`dispatch.py:126` hands the handler the same object. `overview.md:114` says "A handler
cannot commit on its own." Today a handler that commits persists its write and the
operation then reports `unit_of_work_closed` from the dispatcher's own second commit — a
confusing state after a successful, uncontrolled write.
**The spike's fix is narrower than `overview.md`'s sentence, and saying so is part of the
finding.** `HandlerUnitOfWork` closes the two public methods a handler would reach for
(`commit`, `rollback`) and `__enter__`. It does **not** close `uow.connection`
(`backend.py:121`), which the subclass inherits and which hands back a live SQLAlchemy
`Connection`: `view.connection.commit()` still ends the transaction. A subclass cannot
close that path — the property is the handler's only way to write at all. Closing it means
narrowing the *handler protocol*: handing handlers something that is not a `UnitOfWork`,
which changes every registered handler's signature and the `Handler` alias
(`registry.py:57`). So "a handler cannot commit on its own" is true of the two named
methods after this run and still false of the connection.
**Disposition: amend the plan.** The spike ships `HandlerUnitOfWork` as the minimum fix;
the run that owns the dispatcher should decide whether to keep a subclass or narrow the
handler protocol — and, if it wants the unconditional seal `overview.md:114` states, only
the second closes the `connection` path.

### F2 — the internal listener has no operations route.

`overview.md:132` says "The internal listener serves the same routes to the web tier with
the session header in place of the token." `apps/core/src/rheo_app_core/internal_routes.py`
serves only `/internal/v1/routing` and `/internal/v1/session`. The web tier can therefore
call no operation at all on `main`.
**Disposition: amend the plan.** The route is not in any run card's scope; this run
builds it, and the run that owns the web surface next should own it thereafter.

### F3 — `ModuleManifest` is specified in `rheo_contracts` and cannot live there.

`module-contract.md:22` names `rheo_contracts.ModuleManifest`. The manifest carries
`operations[].handler`, typed against `UnitOfWork`, and `tests/test_imports.py` refuses
any `rheo_contracts` import outside stdlib/pydantic/itself — the same collision that
already forced `OperationDeclaration.handler` out of the model
(`packages/contracts/src/rheo_contracts/manifest.py:86-95`).
**Disposition: amend the spec.** Record the manifest's home as `rheo_core.modules` with
the handler passed alongside, exactly as the operation declaration already does.

### F4 — the audit row has nowhere to be written, and `AuditSpec` cannot describe a create.

`AuditSpec` has exactly one field, `subject_field`, naming an **input-model** field
carrying the subject reference. For any create operation the subject does not exist until
the handler has run, so `spike.note.add` — like `core.settings.set` and
`core.settings.set_member` before it — must declare `subject_field=None`. Every create in
release one will therefore write an audit row with a null subject.
**Disposition: amend the spec.** `AuditSpec` needs a second way to name the subject (the
output model's field, or the handler returning it) before 0c2 writes the first real audit
row, or the specification should state plainly that creates carry no subject reference.

**Confirmed against running code.** `spike.note.add` mints the record id inside the
handler, so no input field can name its own subject: `subject_field` is necessarily
`None`, and the audit row the slice writes for the only audited operation it has
carries a null subject reference. F4 was measured, not only reasoned about. See also
F32, which is what the branch on the other side of `subject_field` turned out to
cost.

### F5 — `modules.installed`'s default defeats the sentence that follows it.

`module-contract.md:180-182` gives `modules.installed` the default "every discovered
module" and then says a discovered module not in the list is ignored "so a private
module's presence on disk does not activate it". With the stated default, presence on
disk *does* activate it. There is also no such settings key on `main`.
**Disposition: amend the spec.** `modules.installed` should have no default: an unlisted
module is not loaded. This run ships that correction in miniature — the loader's
`RHEO_MODULES` allowlist, a process variable rather than a settings key only because of
AC 18 (D-3) — so the amendment is not speculative: it is the shape the spike runs on. The
run that lands the real key deletes the env read.

### F6 — criterion 18's production assertion has no home and no owner in code.

`module-contract.md:190-193` places the assertion in the registration path;
`registry.py:15-16` records it as 0c3's. Between now and 0c3 nothing enforces it, and a
run that installs a module before 0c3 (this one) has to invent its own gate.
**Disposition: amend the plan.** Either move the assertion forward to whichever run first
loads a distribution, or state in the plan that no distribution is loaded before 0c3.

### F7 — `RecordHead.revision` is still specified as "incremented on every write" with no compare-and-set.

 Open issue #12's docs half. `spike.note` carries a `revision` column
so the resolver can return one, and this run deliberately never increments it, because
the fix belongs to the PR #22 amendment and to 1a's first mutable record type.
**Disposition: accept** for this run; the amendment is already routed.

### F8 — "callable from a browser session" has no browser-automation harness.

`apps/web` has vitest and no Playwright; CI has one workflow and no browser service.
`make demo` proves seams with `curl`.
**Disposition: amend the plan.** Either the plan states that cookie-driven HTTP against
the real Next.js server is what "from a browser" means for release one, or a run is
assigned to add browser automation. This run takes the first reading and says so.

### F9 — CF1: a refusal carries no memberships, so no first-run screen is buildable.

`internal_routes.session_info` returns `{"state": ctx.state}` and nothing else for every
refusal, including `workspace_unselected`. A first-run workspace picker needs the
account's memberships in exactly that response.
**Disposition: amend the plan.** The one-line backend change (return `memberships`
alongside a `workspace_unselected` refusal) plus a frontend screen is the whole fix, and
it does not amend A11. This run does **not** make it; it works around it inside the spike.

### F10 — issue #37's root cause is that `base_host` is two things at once.

It is the authority for every absolute `urlFor` URL (so it must carry the port) and the
key `is_application_host` matches a port-stripped host against (so it must not). No single
value is correct for a deployment on a non-default port.
**Disposition: amend the spec.** #37's fix should split the two — an origin/host set
separate from the link authority — rather than only correcting the compose value. This run
pins the trade-off with a test.

### F11 — the reference topology cannot be run end to end on a developer's machine.

The internal listener is container-network-only, `web` is not containerized, and
`make demo` consequently never exercises the session path (it sets
`RHEO_CORE_INTERNAL_URL`, not `RHEO_CORE_INTERNAL_API_URL`, so
`internalApiCredentials()` returns null and the shell renders `routing: unavailable`).
**Disposition: amend the plan.** Containerizing `web` is already deferred to 0c; the plan
should record that no full-topology local drive exists until it lands.

### F12 — the module web-contribution composition is entirely unbuilt.

`module-contract.md:158-173` specifies `modules/<id>/web`, `apps/web/src/modules.generated.ts`
and `rheo web compose`. None exists, and the two web gates
(`scripts/check_routing_literals.py`, `scripts/check_web_platform.py`) scan `apps/web`
only, so a module's `web/` directory would be ungated the day it appears.
**Disposition: amend the plan.** Assign web composition — and extending both scan roots to
`modules/*/web` — to the run that ships the first real module's screens (1a).

### F13 — step 2's `operation_set` check is a no-op on every non-token path, and the document says so in one place while describing it as a live check in another.

`overview.md:106-107` lists it as one of the four things the registry checks on every
dispatch. But `overview.md:75`'s own context table already records the value: "`all` for a
web session and the operator". The code agrees with the table —
`context_from_session` sets `operation_set=ALL_OPERATIONS` unconditionally
(`factories.py:247`), as do `context_for_operator` (`:118`) and `context_for_harness`
(`:182`) — and `authorize` refuses `operation_not_permitted` only when the set
`is not ALL_OPERATIONS` (`registry.py:239-246`). Only `context_from_token` carries a
narrowed set (`factories.py:283`). So the check is live on the `api` and `mcp` surfaces
and structurally unreachable from every session, CLI and harness context: a session
cannot be scoped to a subset of operations in release one, by design rather than by
oversight.
**Disposition: amend the spec.** One sentence at `overview.md:106` saying the
operation-set check is a token-surface mechanism and is `all` for sessions and the
operator, so the five-step chain reads as the four-step one it is for every web request.
This finding is also the correction to **this run's own round-1 FR 2**, which listed
`operation_not_permitted` as web-observable and specified a test that could not be
written. No test is written for the dead branch here, and
`tests/postgres/test_api_surface.py::test_operation_not_permitted_403` already covers the
live one.

### F14 — nothing applies the safety class; `dispatch()` never reads it.

`overview.md:108-111` says "The registry applies the operation's safety class: read and
draft run; mutate runs with an audit record; destructive, external, and financial require
an approval…". In the code, `safety_class` is a required field on the declaration
(`rheo_contracts/manifest.py:101`) and `registry.py:188` checks at **registration** that
it is a well-typed `SafetyClass` — and that is all. `dispatch()` (`dispatch.py:91-164`)
never reads it: `read`, `draft` and `mutate` are indistinguishable at run time, and the
only class-adjacent behaviour is that a declaration carrying an `AuditSpec` reaches this
run's sink. The approval half is 0c's by design; the *mutate* half — "runs with an audit
record" — is the sentence run 0v exists to make true, and FR 4 is the first code that
makes it so.
**Disposition: amend the spec.** Split `overview.md:108`'s sentence into what is enforced
today (nothing) and what each later run adds, so a reader does not expect a refusal that
has no branch. Recording it is why FR 2 claims three web-observable refusals rather than
six.

### F15 — a module cannot install itself, and nothing in release one can install it either.

`core.module_state` and `core.module_schema_version` were shipped in 0b1 and are written
by exactly one thing in the repository: `tests/harness/registry.py:287-303`, a test
fixture. `module-contract.md` § Lifecycle specifies install/enable/disable, but no code
implements it and no run card before phase 2 owns it. This run needed the row and took the
same route the harness did — ~17 lines inside the throwaway module's own CLI — rather than
building a lifecycle subsystem that none of its five shapes required (FR 7, reduced).
**Disposition: amend the plan.** Name the run that owns module lifecycle (phase 2 by the
current reading) and record that until it lands, "install a module" means "write the row
yourself", which is not a contract anybody outside the repository could follow.

### F16 — the shipped workspace switcher is unreachable in the reference topology.

`components/workspace-switcher.tsx:37` `fetch`es the **relative** `switcherAction`
(`lib/routing/links.ts:50-52`, relative on purpose — `links.ts:18-23` explains that an
absolute URL would send a subdomain-mode POST to the identity host), but `/auth/*` is
served by `core`'s public listener while the browser's origin is Next.js. `apps/web/src/app` contains only `layout.tsx`, `page.tsx` and
`login/page.tsx`; `apps/web/next.config.ts` is `{}` with no rewrites; `deploy/` ships no
reverse proxy. So in every topology the repository can actually run today, that POST
reaches Next and 404s. The component is correct about *what* it must send and has no path
that gets it there. This is a sibling of issue #37: both are cases where the web tier's
route to `/auth/*` is assumed rather than configured.
**Disposition: amend the plan.** The deployment topology needs one of: a reverse proxy in
`deploy/` that routes `/auth/*` from the web origin to the public listener, Next rewrites
for the same, or a server-side forward like this spike's. Whichever it is belongs in
`deploy/` and in `identity-and-topology.md`, before a real screen depends on the switcher.

### F17 — what a deployment loads is decided by the image build, not by configuration.

`Dockerfile` COPYs `modules/` wholesale and runs `uv sync --frozen`, so **every**
`modules/*` distribution in the checkout is installed into the image and its `rheo.modules`
entry point is discoverable at run time. Combined with F5's default ("every discovered
module"), the set of modules a production deployment runs would be a property of the build
context rather than of the deployment's settings — and a module added to the repository for
any reason would activate itself on the next image build.
**Disposition: amend the spec.** Say that the image's installed set and the loaded set are
different things, and that the loaded set is configuration. This run holds the line with
the `RHEO_MODULES` allowlist (D-3); the permanent form is `modules.installed` with no
default (F5).

### F18 — the boundary gates do not scan `modules/`, which is where module code lives.

Three shipped AST gates, three different roots. `tests/test_boundary.py:82` sets
`_SCAN_DIRS = ("packages", "apps", "scripts", "tests")` and its B15 scan (line 181) walks
exactly those — so the rule that only `rheo_core/boundary/` may construct a
`WorkspaceContext` is **unenforced for every file under `modules/`**.
`tests/test_boundary_scans.py` has the same `_SCAN_DIRS` at line 30 and uses it bare at
line 77, but at line 204 its scope-privates scan walks `(*_SCAN_DIRS, "modules")` — so one
scan in that file was extended to modules and its sibling was not. This run's own module
is the first real code under `modules/*/src` (the four existing distributions are empty
placeholders), so the gap has been latent, not visible. It is the same shape as F12: a
gate whose root set was drawn before module code existed.
**Disposition: amend the plan.** Extend `_SCAN_DIRS` to include `modules` in both files —
one-line changes — in the run that ships the first real module (1a), together with F12's
extension of the two web scans to `modules/*/web`. This run does **not** make the change:
both files are 0b-ratified gates outside its file map, and the spike must pass them as
they are, not as it would like them to be.

**Confirmed against running code.** `tests/test_boundary.py:82`'s `_SCAN_DIRS` really
does exclude `modules/`, while `tests/test_boundary_scans.py:204` adds `"modules"` to
its own walk — one scan covers module distributions and its sibling does not. The
spike's module is the first real code under `modules/*/src` and obeys the uncovered
rule anyway, so the gap stayed latent rather than becoming a failure.

### F19 — an `AuditSpec` names what to record but not who records it, so one process-global sink captures every module's operations and core's.

`AuditSpec` has one field, `subject_field` (`rheo_contracts/manifest.py:60-68`), and the four core
mutate operations already declare `AuditSpec(subject_field=None)` (`core_ops.py:243`,
`:253`, `:312`, `:324`, as policy at `core_ops.py:21-24`). Nothing in the declaration, and
nothing in `overview.md`'s description of the audit step, says which component owns the
write. The naive seam — one installed sink, fired whenever `declaration.audit is not None`
— is therefore module-blind: install a module's sink and it fires on `core.token.issue`
and `core.settings.set` too, in workspaces that module was never installed into. In this
run that is not theoretical: the spike's sink writes `spike.audit_probe`, the schema does
not exist in a plain test workspace, the INSERT raises, `dispatch()`'s `except Exception`
rolls back, and `core.token.issue` returns `failed` in a shipped test
(`tests/postgres/test_tokens.py:106-124`) that this run may not edit. Ownership is
recoverable from the registry — `RegisteredOperation.module_id` (`registry.py:144-150`) —
but only because the *registry* records it; the audit declaration does not.
**Disposition: amend the spec.** Before 0c2 writes the first real audit row, state who owns
the write: one core-owned writer for every operation (in which case a module must never be
able to install a sink at all), or a per-owner seam. This run ships the second in miniature
— `sink_for(operation.module_id)`, with `NullAuditSink` for anything without one — so the
question is answered concretely rather than left to whoever writes `core.audit_record`
first. Whichever 0c2 picks, the property to keep is the one FR 4 exists to prove: the
*dispatcher* decides that a row is written, from the registry, and a module supplies at
most a writer.

### F20 — the internal listener does not serve "the same routes", and one generated document cannot key both surfaces.

`overview.md:132` says "The internal listener serves the same routes to the web tier with
the session header in place of the token." The shipped internal routes are
`/internal/v1/routing` and `/internal/v1/session` (`internal_routes.py:67`, `:74`) while the
bearer surface is `/api/v1/operations/{name}` (`api_routes.py:88`) — different prefixes, so
"the same routes" can only mean "the same operations". That is not a wording quibble once a
document is generated: `rheo openapi` emits one path per operation keyed
`/api/v1/operations/<name>`, `openapi-typescript` turns those keys into the type index, and
the web tier's client posts to `/internal/v1/operations/<name>`. The generated types
therefore describe a surface the only consumer never calls. It works — both listeners run
the same `dispatch()` over the same models, so the schemas are right — but it works by
agreement, not by construction, and a future divergence between the two listeners' request
shapes would be invisible to the generator.
**Disposition: amend the spec.** Either say in `overview.md` that the internal listener
mirrors the *operations*, not the paths, and that the document is keyed by the `api`
surface (so a generated client's path keys are not its request targets), or give the
document an OpenAPI `servers` entry per listener so the two paths are one contract with two
bases. The second is the better answer and belongs to whichever run first ships a real
module's screens (1a) or the `api` surface's own published document (0c3/phase 2). This run
holds the asymmetry and names it.

**Confirmed against running code.** The internal listener serves
`/internal/v1/operations/{name}` (`apps/core/src/rheo_app_core/internal_routes.py:162`);
the emitted document is keyed `/api/v1/operations/<name>`
(`apps/web/src/generated/openapi.json`). So the web client's request target and the
generated types it is indexed by come from different path keys, exactly as F20
predicted, and both sides record it in their own docstrings
(`apps/web/src/lib/spike/client.ts:51-59`). The asymmetry is held deliberately, not
fixed. F20's proposed `servers` amendment has a natural home: the new route's own
docstring already states that `overview.md:132` fixes no path, so giving the document
one base per listener would be writing down what the code already does.

### F21 — a stated requirement and the thing that must supply it are read in different places, and a narrative review of either side alone does not catch the gap.

This run hit the same shape three times, at three different layers of its own design and
build:

1. **The scope-cut module lifecycle.** FR 2's `module_disabled` branch needs a workspace
   whose `core.module_state` row already says `enabled` — a state some producer must write.
   The first design draft supplied it by importing `module-contract.md`'s full ratified
   lifecycle subsystem (`lifecycle.py`, the `storage/repositories.py` write helpers, `rheo
   module install|enable|list`) — machinery well beyond any of the run's five shapes. Only the
   round-1 cold review (blocker `r1-b7`, promoted from a warning; critique-round1.md's item 8)
   noticed that `tests/harness/registry.py`'s already-shipped ~15-line pattern satisfies the
   same requirement in full — because it weighed FR 2's actual need directly against the
   cheapest available producer, rather than reading the requirement or the ratified lifecycle
   document on its own.
2. **`r3-b1` — D-6's module-surfaces mapping.** Prompt `03` required `routing_config()` (the
   consumer) to receive "the loaded modules' surfaces", but prompt `01` (the producer) was
   never asked to keep what it loaded, and `internal_routes.routing_config()` takes no
   `Request` to fetch them another way. This gap sat in `spec.md` and `plan.md` through **two**
   full design-and-review rounds — round 1 and round 2, each reading both documents in
   full — and surfaced only when the prompts review (round 3) put `03`'s consuming line next
   to `01`'s producing chunk, in two side-by-side files.
3. **This checkpoint — prompt `04`'s own test homes.** Prompt `04`'s checkpoint requires four
   seams (`spikePath`'s throw-on-absent-surface guard, both route handlers' 303-with-outcome
   shape, the `WorkspaceSwitcher` mutant) to be mutation-verified, but the affirmative file
   map — echoing `plan.md`'s own file map, which never named these test files either —
   supplied exactly one web test path, `client.test.ts`. This gap was present from the first
   draft of prompt `04`, survived the round-3 prompts review (which read the same prompt
   folder and raised `r3-b1` plus eight warnings, but not this), and was caught only when the
   checkpoint-01 cold reviewer checked prompt `04`'s own seam list against the file map that
   must supply each seam's test a home.

None of the three was visible from reading the requirement alone, or the candidate producer
alone — each needed the two placed side by side, and two of the three survived a full
cold-review round of the documents that stated them before that juxtaposition happened. That
is a property of how this run's own ratified inputs are organized — a requirement and its
supplier routinely live in different documents, or different prompt files, with no explicit
cross-reference connecting them — not a property of any one defect. Surfacing it four runs
early, while it is still cheap to fix in a review checklist rather than in shipped code, is
exactly what run 0v exists to do.
**Disposition: amend the plan.** Add an explicit producer/consumer trace as its own checklist
step to how this run's own review rounds work, and recommend the same for the ratified
architecture documents' own review practice: for every clause that says a value, mapping, row,
or file "must be available/populated/named here," a reviewer — cold or otherwise — checks
where it is supplied and confirms that supplier is itself in scope, as a distinct step rather
than an incidental catch of a narrative read. A full read of both documents caught this zero
times out of two tries; a check built to look for exactly this shape caught it three times out
of three.

**A fourth instance, found during the build.** See F29: the typed web client was
required to hold a path-key template that this repository's own routing-literal gate
treats as a route literal, because the requirement and the gate that governs it are
stated in different places and neither read alone reveals the collision. That makes
four instances in one run. Three of the four were caught by a check built to look for
exactly this shape; none was caught by a narrative read of either side on its own.

### F22 — `apps/web` cannot run a `.tsx` test as the repository ships it, and the plan asserted the opposite without executing it.

Prompt `04` commissions `apps/web/src/app/spike/page.test.tsx`, which would be the first `.tsx`
test in the repository; all seven existing web tests are `.ts`. The prompt originally stated that
`apps/web/vitest.config.ts`'s `include` "already covers `src/**/*.test.tsx`, so no config change
is needed for the `.tsx` extension — confirmed before relying on it, not assumed." Half of that
is true and the confirmation had not in fact happened. Measured at `main` (`8f243e5`):

- `apps/web/vitest.config.ts:include` really is `["src/**/*.test.ts", "src/**/*.test.tsx"]`, so
  the file would be **collected**. That half holds.
- Vite copies `compilerOptions.jsx` from the nearest tsconfig into the esbuild call and only
  drops it when the Vite config sets `esbuild.jsx` itself (`transformWithEsbuild`'s
  `meaningfulFields` pass-through). `apps/web/vitest.config.ts` sets no `esbuild` block, and
  `apps/web/tsconfig.json:jsx` is `"preserve"`.
- esbuild does **not** honour `"preserve"` arriving from the tsconfig field. Measured directly on
  the installed esbuild: `tsconfigRaw.compilerOptions.jsx = "preserve"` emits
  `React.createElement(...)`, identical to `"react"`, while esbuild's own top-level
  `jsx: "preserve"` does preserve. So a `.tsx` file compiles to the classic runtime with **no
  React import injected**.
- **No `.tsx` file in `apps/web` imports the `React` namespace.** `layout.tsx` imports
  `type ReactNode`, `workspace-switcher.tsx` imports `{ useState, type FormEvent }`;
  `page.tsx`, `login/page.tsx` and `logout-form.tsx` import nothing from React. Next.js supplies
  the automatic runtime through its own compiler, which vitest does not use.

So the failure is not confined to the new test file and cannot be fixed by importing React
inside it: `page.test.tsx` must import `Page` from `page.tsx` and `WorkspaceSwitcher` from
`@/components/workspace-switcher`, and both are transformed the same way. The expected symptom is
`ReferenceError: React is not defined`.

**Confirmed against the run's own toolchain, not inferred.** The first measurement was taken
against what happened to be installed in the working tree — vite 5.4.21, esbuild 0.21.5,
vitest 2.1.9 — which PR #41 (`8f243e5`) supersedes and which had never been installed in this
repository. That qualifier is now discharged. `make install` was then run in this run's
worktree at `8f243e5`, giving **vitest 5.0.0 and esbuild 0.28.2** for the first time in this
repository's history, and ran a throwaway `.tsx` probe under it. It failed with
`ReferenceError: React is not defined`, at the JSX expression, exactly as described above. The
probe was deleted and the worktree left clean.

Two refinements that measurement produced. First, **importing** a shipped `.tsx` component
succeeds — the probe's second case imported `WorkspaceSwitcher` and passed, because its JSX sits
in a function body that never ran. The error fires when JSX **executes**, which is what
`page.test.tsx` does when it renders `Page`. A clean import is not evidence of a clean transform.
Second, the remedy is **verified rather than proposed**: with
`esbuild: { jsx: "automatic", jsxImportSource: "react" }` the probe passed, and the seven shipped
web test files stayed green either way — 84 tests with the line, 84 without — so it is inert for
everything that exists today.

One honest limit on how much this finding weighs: this is the run's own prompt disagreeing with
the tree, not a ratified document doing so, which makes it smaller than most of F1–F21. But it is
the same failure mode the run exists to surface, and it is the first thing 0v proved by running
rather than by reading: a claim about how the shipped code behaves, written from reading, that a
build would otherwise have discovered at the worst possible moment.

**Disposition: amend the plan.** `apps/web/vitest.config.ts` is added to C4's affirmative file
map for exactly one line, `esbuild: { jsx: "automatic", jsxImportSource: "react" }`, which was
verified on the same Vite path to emit `react/jsx-runtime` output with the import injected and is
inert for the seven existing `.ts` tests. Prompt `04` step 8 now states the measurement, requires
the coder to run the test before concluding anything, and forbids the larger fixes
(`@vitejs/plugin-react`, a DOM environment, any new dependency) that this one line makes
unnecessary. **C4 must still run it and record the result**; the prediction is now a
confirmed measurement, but the chunk's own method is to confirm the failure before applying the
fix and confirm the fix after, and a divergence from this would itself be a finding. The wider lesson belongs with F21: the original sentence carried its own
audit trail ("confirmed before relying on it, not assumed") and the confirmation had still not
been performed, so a claim's self-description is not evidence that anyone checked it.

**Confirmed against running code, and sharpened.** Removing the one-line `esbuild`
setting from `apps/web/vitest.config.ts` made all twelve tests in
`apps/web/src/app/spike/page.test.tsx` fail with `ReferenceError: React is not
defined`, and the stack names `apps/web/src/app/spike/page.tsx:249` — the imported
page's own `return`, with the test file appearing only further down the stack as the
caller. So the failure is in the component under test rather than in the test file,
and `import React from "react"` written inside the test could never have fixed it.
Restoring the line returned all twelve to green.

## Findings from the build (F23–F54)

These are what building the slice added. Every one was found by running code, or by
verifying a claim about running code, rather than by reading it. They are ordered by
consequence.

### F23 — the escape hatch F1 leaves open produces a durable write reported to the caller as a failure.

F1 predicts the symptom of the one path it says a subclass cannot close: "a handler
that commits persists its write and the operation then reports `unit_of_work_closed`
from the dispatcher's own second commit". That is true of `uow.commit()`, the path
this run closes with `HandlerUnitOfWork`
(`packages/core/src/rheo_core/storage/backend.py:156`). It is false of
`uow.connection.commit()`, the path F1 explicitly leaves open.

Measured (`tests/test_handler_uow.py:214`,
`test_the_connection_is_not_sealed_and_that_is_the_known_gap`):
`Connection.commit()` succeeds and the connection stays usable. The `UnitOfWork`
never learns, so its own `transaction.commit()` raises SQLAlchemy's
`InvalidRequestError`, and `packages/core/src/rheo_core/operations/dispatch.py:202`'s
`except Exception` folds that into `state="failed"`, `error_code="handler_failed"`,
`error_text="InvalidRequestError"`.

So after this run the remaining escape hatch produces a durable, uncontrolled write
that the caller is told failed. That is worse than F1's "confusing state": a caller
that retries on `failed` — the reasonable thing to do with a refusal naming an
exception class — writes twice, and nothing in the refusal tells it not to.

This is the clearest thing the spike produced: a ratified document's own prediction
about running code, falsified by running it.

**Disposition: amend the spec.** `overview.md:114`'s sentence is what this
contradicts, and F1's own account of the residual gap needs the same correction. The
run that owns the dispatcher (0c0) should choose between keeping the subclass and
narrowing the handler protocol knowing that the residual path fails this way, not
the way F1 describes.

### F24 — installing a module widens what the `read_only` package set means, deployment-wide, and the widening is stored as granted.

`read_only()` (`packages/core/src/rheo_core/tokens/sets.py:103-113`) is "every
registered operation of `SafetyClass.READ`", computed by iterating `REGISTRY.names()`
— the process-wide operation registry. `agent_default()` (`:116-121`) and
`cli_full()` (`:124-128`) read the same table. `packages/core/src/rheo_core/tokens/issue.py:214`
computes `package_union = read_only() | agent_default() | cli_full()` and `:258`
writes the result into `access_token_operation` rows.

So a named package set is a property of the process's registry, not of the workspace
a token is issued for. Install any module, in any workspace of a deployment, and
`read_only` means something wider for every token issued afterwards — including
tokens for workspaces that module was never installed into. Already-issued tokens
are safe, because the expansion is snapshotted into rows at issue time. New ones
silently carry operations that `authorize` will later refuse `module_disabled`,
while the stored row says the operation was granted.

Nothing in `module-contract.md` or `identity-and-topology.md` says that installing a
module changes what a named package set means. It has been latent rather than
visible because no module had ever been installed: this run is the first code in the
repository to install one, and `tests/postgres/test_tokens.py:174` and `:202` pin the
set exactly, so they go red the moment a second module registers a `read` operation.

This run's own plan reasoned through `cli_full()`, `agent_default()` and the token
snapshot and concluded registration growth was safe as the suite stands. It never
mentioned `read_only()`, and `read_only()` is the one that was not safe.

**Disposition: amend the spec.** Say whether a package set is deployment-wide or
per-workspace, and if it is deployment-wide, say plainly that installing a module is
an authorization-surface change. The decision is needed before the first real module
ships (1a), and it interacts with F5, F15 and F17: what is installed, what is
loaded, and what a token means are three different sets today with one word covering
all three.

### F25 — the OpenAPI document is not generated at startup, and cannot be.

`overview.md:131` says the document is generated from the registry's input and output
models "at startup". `apps/core`'s `startup.py` contains no OpenAPI reference at all.
The generator is a CLI command, `rheo openapi`, producing a committed artifact — and
it has to be. The byte-exact CI drift gate requires the document to depend on exactly
the installed distributions and the module allowlist, whereas generating it at
startup would bind it to a running process with settings, a data root and a database.
Generating it from a command is also what lets `apps/cli` emit it without depending
on `apps/core`.

This is not covered by F20, which is about the sentence on the next line — the one
about *paths* — rather than this one.

**Disposition: amend the spec.** One clause at `overview.md:131` saying the document
is generated by a command into a committed artifact and checked for drift in CI. Any
run can land it; the natural one is whichever first publishes the `api` surface's own
document (0c3, or phase 2).

### F26 — the reference topology's single process is load-bearing, and splitting it fails silently.

Found by rehearsal rather than by reading. Running `public_app` and `internal_app` as
two uvicorn processes makes `GET /internal/v1/routing` answer `"modules": {}` — a
well-formed document that is wrong. `load_modules()` runs in `public_app`'s lifespan
(`apps/core/src/rheo_app_core/startup.py:94`) and the internal listener reads
`module_surfaces()` from the same process's registry
(`apps/core/src/rheo_app_core/internal_routes.py:36`, `:104`).

The architecture justifies one process by its shared singletons. It does not say that
splitting the two listeners produces a silently wrong routing document rather than an
error. Downstream, the web tier's path helper then throws on an absent surface and
the page 500s, several layers away from the cause.

**Disposition: amend the spec.** State that the internal listener's routing document
is only correct in the process that loaded the modules, so a deployment that splits
the listeners must load modules in both. This run's driver asserts the surface is
present after startup, which turns the silent failure into a named one; the same
assertion belongs in whichever run containerizes `web` and lands the full topology
(0c).

### F27 — the end-to-end driver needs four host ports and two of them cannot be moved.

The specification says the driver preflights three host ports. It needs four:
postgres, the web server, the public listener on 8000 and the internal listener on
8100. `apps/core/src/rheo_app_core/serve.py:26-27` hardcodes both port numbers inside
its `uvicorn.Config` calls, and that file reads no environment variable at all —
unlike `make demo`, where `RHEO_CORE_PORT` moves a compose host-side publish. So
there is no knob, and the driver cannot run on any host where 8000 or 8100 is
occupied by something else.

That is not hypothetical. This run's own last green end-to-end drive used 8010 and
8110, because host port 8000 was held by an unrelated long-running service that was
not this run's to stop. The shipped recipe differs from the proven one by exactly two
integer literals, `serve.py` is byte-identical to `main`, and the unmodified driver
was separately run on the real ports and produced the correct preflight refusal. So
the end-to-end driver is **proven modulo two integers**, which is a weaker claim than
"proven" and is the honest one.

**Disposition: amend the spec** — the preflight covers four ports, two of them fixed
— plus a follow-up in whichever run next owns `serve.py` (0c) making it read
`RHEO_CORE_PORT` and `RHEO_INTERNAL_PORT` with the current values as defaults. A
knob added to the recipe alone would change nothing, which is its own kind of defect.

### F28 — the prescribed codegen command cannot run, and its failure was invisible through a pipe.

The command the plan carried in three places —
`pnpm -C apps/web exec openapi-typescript apps/web/src/generated/openapi.json -o apps/web/src/generated/api-types.ts`
— exits 1. `-C apps/web` sets the working directory, and a repository-root-relative
path is then resolved under it and doubled. Dropping `-C` is not the fix either: the
root `node_modules/.bin` holds only `eslint`, while `apps/web/node_modules/.bin`
holds `openapi-typescript`. The corrected form,
`pnpm -C apps/web exec openapi-typescript src/generated/openapi.json -o src/generated/api-types.ts`,
exits 0 and reproduces the committed `apps/web/src/generated/api-types.ts` byte for
byte — which also proves the generator is deterministic. It is now what `make codegen`
runs.

It would have failed on its first CI run, because the drift gate runs
`make codegen && git diff --exit-code -- apps/web/src/generated`.

**How it stayed invisible is worth more than the fix.** The failure was first
observed through `... | tail -20`, which reported exit 0 while the command had
exited 1; only `set -o pipefail` showed the truth. A pipeline returns its last
command's status, so a failing command piped into `tail`, `head` or `tee` looks
like success to any `&&` chain built on it.

**Disposition: amend the plan.** Closed in code this run; the places that carried the
broken form are corrected so it is not reintroduced.

### F29 — the typed client cannot hold the path-key template it is required to hold.

`scripts/check_routing_literals.py:118` matches `/api/` in any `.ts` file under
`apps/web/src` outside `lib/routing/` and `generated/`, and it strips comments before
matching (`:151`, `:285`) precisely so that prose cannot hide a real link. A
**type-level** template literal is therefore a route literal by that scan's own
definition. The client as the plan described it would have held
`` `/api/v1/operations/${Name}` `` inside `lib/spike/client.ts`, and could not have
passed its own chunk's green checkpoint, which runs that scan.

Resolved by moving `OperationPathKey<Name>` into
`apps/web/src/lib/routing/load.ts:80` — inside the allowlisted directory — and
importing the type (`apps/web/src/lib/spike/client.ts:6`, `:55`, `:59`). Inference
through the alias still works and the type-level mutant still bites.

This is the **fourth** instance of F21's shape in this run: the requirement (the
client holds the template) and the gate that governs it (the routing-literal scan)
are stated in different places, and reading either alone does not reveal the
collision.

**Disposition: amend the plan.** When the first real module's screens land (1a) the
scan roots extend to `modules/*/web` (F12), and the same question arrives there: a
module's own typed client will want the same template, and the allowlisted directory
lives in `apps/web`, not in the module.

### F30 — a defect in process-global state can show a clean pass when its own test file is run alone.

Produced by verification rather than by the build. A mutant in the spike-install
fixture's teardown — the whole teardown replaced by `pass` — was run against
`tests/postgres/test_tokens.py` on its own and came back 19 passed: a false green.
Run that file by itself and nothing requests the fixture, so the module never
registers and `read_only()` is unaffected (F24). Re-run against the whole suite, the
same mutant reproduces exactly the two expected failures.

The consequence is general and outlives the spike. A developer reproducing a CI
failure file by file sees green, and any future gate that narrows pytest to a subset
of files to save time stops detecting this class of defect entirely.

**Disposition: amend the plan.** Record that assertions over process-global tables —
the operation registry, the resolver registry, the audit sinks, the module surfaces —
are only meaningful in a whole-suite run, and that per-file reproduction is not
evidence about them.

### F31 — a fixture that writes four process-global tables unwrote two of them.

The spike-install fixture restored both registries at teardown but never called
`reset_surfaces()` or `reset_sinks()` — the two resets shipped for exactly this
purpose, and named in the fixture's own docstring. The leak was observable through a
shipped route: `GET /internal/v1/routing` reported a phantom `spike` module surface
for the rest of the pytest session, in every file, after any test that used the
fixture. That would have let the web surface's own wiring assertions pass against a
surface nothing had wired — a gate that cannot fail, introduced one chunk before the
chunk it would have fooled.

Fixed during the run (`tests/conftest.py:332-333`), and proven in both directions
with a throwaway probe: with the resets in place a following probe sees both tables
empty; with them removed, both probe assertions fail and name the leaked surface and
the leaked sink.

**Disposition: amend the plan.** The fixture contract should say that every
process-global table a fixture writes is unwritten at teardown, rather than the two
that happened to be obvious. The fixture goes away at 0c0's branch cut; the rule
does not.

### F32 — `AuditSpec.subject_field`'s non-`None` branch has no consumer, so two behaviours inside it were chosen by the build rather than by the specification.

Every shipped declaration passes `subject_field=None` (`core_ops.py:243`, `:253`,
`:312`, `:324`), and F4 explains why every create must. The dispatcher still has to
resolve the field, so the branch exists and was driven from a local registry rather
than shipped untested. Two choices inside it are now 0c2's to inherit: a `str` field
is parsed through `RecordRef.parse`, and an unparseable value raises into
`dispatch()`'s `except Exception` — rolling back and reporting `failed` — instead of
falling back to `None`. The second means a mistake in audit metadata fails an
operation that otherwise succeeded.

**Disposition: amend the spec.** Decide both deliberately before 0c2 writes the first
real audit row, or resolve F4 and delete the branch.

### F33 — a module's entry-point name has to be its module id, and nothing ratified says so.

The allowlist gates module ids, so the loader must filter either before or after
`EntryPoint.load()`. Filtering afterwards means importing every module distribution
on disk in order to decide it should not have been imported, which undercuts F17's
own point that the installed set and the loaded set are different things. So the
loader filters on `entry_point.name` and then refuses a manifest whose `module_id`
disagrees with the name it was published under
(`packages/core/src/rheo_core/modules/loader.py:106-117`). The spike's packaging
agrees with that rule — but it would have agreed by coincidence had the loader not
enforced it.

**Disposition: amend the spec.** State the rule in `module-contract.md` beside the
entry-point group, because the permanent `modules.installed` key (F5) faces the
identical choice and would otherwise be free to make it differently.

### F34 — the response contract has no outcome word for "the route was reached and refused the input".

`WorkspaceSwitchInput.target_workspace_id` is a UUID, so FastAPI answers anything
that is not a UUID with a 422 and a `{"detail": [...]}` body carrying no `state`
field at all. The web contract enumerates `selected`, three named refusals, and
`unavailable` "when the forward could not reach the listener at all", and the
free-text fallback on the shipped form makes a non-UUID reachable by typing. So a
reachable input error is currently reported to the user as an unreachable listener.
The handler was built exactly as specified rather than inventing a state, and the
behaviour is pinned by a test.

**Disposition: amend the spec.** The contract needs a word for a validation refusal.
1a meets this the moment it puts a form over a typed input.

### F35 — the outcome union cannot be narrowed by its own `state` field.

`{state: "succeeded", result} | {state: string, error} | {state: "unavailable"}` does
not discriminate. The refusal member's `state` has to be `string`, because the
dispatcher's refusal vocabulary is open and restating it in the web tier is exactly
what generated types exist to avoid — but `string` includes `"succeeded"`, so
TypeScript narrows nothing. `tsc` caught it. Shipped with a `succeeded()` type
predicate discriminating on `"result" in outcome` instead.

**Disposition: amend the spec.** Name the discriminant the web tier is meant to use.
1a meets it the moment it writes a second consumer.

### F36 — one form field named twice for two controls makes the second control dead.

The page's single form carries `target_workspace_id` once per membership radio and
once for the free-text fallback. `FormData.get` returns the first entry, so a checked
radio always beats a typed id and the fallback can never be used in the signed-in
case: a control that looks like it works and silently does not. Fixed here with
last-non-empty-wins and mutation-verified. The design decision specifies both
controls and the field name; it does not say how the two compose.

**Disposition: amend the spec.**

### F37 — a module distribution may not import the secret store, not even an exception type.

`tests/test_boundary_scans.py:123`'s
`test_no_module_distribution_imports_the_secret_store` caught the spike's own
developer CLI importing `SecretRefusal` purely in order to catch it — which is
exactly what the operator CLI's own command runner does. The gate is right, and the
"mirror the operator CLI" guidance quietly conflicts with it. The consequence: a
module-owned command cannot render a secret-store refusal as one clean line; it gets
a traceback instead. That is fine for a developer-only throwaway. When phase 2 gives
real modules commands, either the refusal type needs a home outside
`rheo_core.secrets`, or module commands must route through a core-owned entry point.
The documents say neither.

**Disposition: amend the spec.**

### F38 — a handler must not create its own schema, and the test harness is the wrong prior art here.

`tests/harness/registry.py:161` and `:179` call `ensure_note_table` on every write. A
real module's handler must not: `authorize` refuses `module_disabled` before a
handler runs unless the workspace carries an enabled module-state row, and the only
writers of that row create the schema in the same transaction — so a lazy `CREATE`
inside a handler would hide a broken install instead of surfacing it. The spike's
handlers create nothing.

**Disposition: accept** — a deliberate divergence from the harness pattern, recorded
so that 1a does not copy the harness's shape into a real module.

### F39 — neither the operation registry nor the resolver registry has a public reset, so test code reaches a private.

`rheo_core.audit` has `reset_sinks()` and `rheo_core.modules` has `reset_surfaces()`,
both added this run precisely because a process-wide table a test writes must be
un-writable afterwards. `OperationRegistry` and `ResolverRegistry` have neither a
reset nor a deregister, so the spike fixture clears the keys it added from
`REGISTRY._operations` and `RESOLVERS._resolvers` at teardown, documented in place
with the reason.

**Disposition: amend the plan.** Whichever run next owns
`packages/core/src/rheo_core/operations/registry.py` should add the missing reset
beside the other two. Until then, the reach-into-a-private goes away with the spike.

### F40 — the audit seam needed its own refusal type because the natural one is unimportable from it.

The obvious sibling for a conflicting sink install is `RegistrationRefused`, but
importing it closes a real cycle: `rheo_core.operations.refusals` runs
`operations/__init__.py`, which imports `dispatch`, which imports `rheo_core.audit`.
`AuditSinkRefused` is defined locally instead
(`packages/core/src/rheo_core/audit/sink.py:44`), with the reason recorded in place.
A second refusal type now lives outside `operations/refusals.py`.

**Disposition: accept** for release one; 0c2 folds it into the real audit writer's
vocabulary.

### F41 — one storage refusal state and one storage class are not re-exported from `rheo_core.storage`.

The other ten storage refusal states are in `storage/__init__.py`'s `__all__`.
`HANDLER_MAY_NOT_COMMIT` (`packages/core/src/rheo_core/storage/backend.py:47`) and
`HandlerUnitOfWork` (`:156`) are not, so callers import them from the module path —
which `dispatch.py` and several shipped tests already do for `UnitOfWork`. That file
was outside this run's scope and was left alone rather than widened into. The
asymmetry is deliberate rather than an oversight, and it is worth closing when a run
next owns that file (0c0).

**Disposition: amend the plan.**

### F42 — both operation surfaces call the synchronous, database-blocking `dispatch()` inside an `async def`.

The new internal route does, and so does the shipped `api_routes.run_operation` it
was modelled on. Neither hands the call to a worker thread, so a slow operation
blocks the event loop for every other request that listener is serving. The spike
copied the shipped pattern deliberately rather than diverging from it in a throwaway
chunk, and the property belongs to both surfaces rather than to the new one.

**Disposition: accept** for release one, named here so it is a known property rather
than a discovery under load. Whichever run first cares about concurrency on either
listener should move both together, so the two surfaces do not diverge.

### F43 — the end-to-end driver could not be run twice in a row, and `make demo` has the identical latent defect.

`next start` spawns `next-server` as a grandchild, so killing the `pnpm` process
returns before the listening socket is released and a second immediate run fails its
own port preflight. Fixed here with a bounded wait in the driver's cleanup
(`Makefile:315`), proven by a back-to-back green run. `make demo`'s cleanup kills only
`WEB_PID` and does not wait (`Makefile:111`), so it carries the same defect; it was
deliberately left alone as outside this run's scope.

**Disposition: amend the plan.** `make demo` gets the same fix in whichever run next
owns the Makefile (0c).

### F44 — `$(MAKE)` inside a single-line recipe makes `make -n` execute it.

GNU make runs any recipe line containing the literal `$(MAKE)` even under `-n`, and a
backslash-continued driver is one line. So `make -n spike` executed the whole driver
— containers, servers and all — instead of printing it. Shipped as
`"$${MAKE:-make}"`.

Worth recording alongside the fix: the first attempt to verify it was itself a
vacuous check. Grepping the `make -n` output for `PASS` and `FAIL` returned 33 hits,
which looked like proof the fix had failed. It had not — `make -n` prints the recipe,
and the recipe source contains `echo "PASS ..."` and `echo "FAIL ..."` strings, so
the grep matched for the wrong reason. The decisive re-check was zero bare
`PASS`/`FAIL` lines at the start of a line, zero preflight banner lines actually
emitted, and no new containers.

**Disposition: accept** — a general trap for single-line driver recipes, recorded
rather than fixed anywhere else.

### F45 — React's server-rendered comment separators defeat the obvious assertion on rendered text.

The spike page renders `workspace: {id} ({role})`, which Next serves as
`workspace: <!-- -->01a0…<!-- --> (<!-- -->owner<!-- -->)`. A driver grepping for
`workspace: <id>` fails against a page that is entirely correct.

**Disposition: accept** — a property of React's output, recorded for anything else
that asserts on a rendered interpolation.

### F46 — the container image build runs in neither place that could prove it.

`docker build` could not be run on the machine this run was built on: a Docker
credential-helper configuration stalls on `ghcr.io`, with `docker pull` timing out
past five minutes under the normal configuration and succeeding in seconds with an
empty `DOCKER_CONFIG`. That much is an environment problem rather than a code defect.
The part that is not: **CI does not run `docker build` either.**
`.github/workflows/repository-checks.yml` has no such step. So the image build is
proven on neither a developer's machine here nor in CI, while
`pnpm -C apps/web build` — the first half of `make build` — passed every time.

**Disposition: amend the plan.** Either give the container build a CI job in
whichever run next owns the workflow (0c), or state that it is a release-time step
and say where it runs.

### F47 — a test that asserts nothing is discoverable goes red the moment something is installed.

The module-loader test could have asserted that `discovered()` returns empty against
the real environment, which was true right up until this run installed the first
module distribution. It asserts instead that its three fabricated module ids are
disjoint from whatever happens to be discoverable — the same guard, and stable across
an installed module.

**Disposition: accept** — recorded so the assertion is not "fixed" back into the
brittle form, and because 1a installs the first real module and meets the same
choice.

### F48 — the testing-conventions document the build instructions name is not in this repository.

`docs/` holds `README.md`, `architecture/`, `eval/`, `ideas/`, `requirements/` and
`workspace-layout.md`. There is no `docs/conventions/`, and the document naming this
project's test-seam conventions lives outside the repository. Every chunk of this run
was handed the resolved location explicitly; a contributor following the path as
written would find nothing.

**Disposition: amend the plan.** Either the conventions document belongs in the
repository, where contributors can read it, or the instructions should stop citing a
path that does not resolve here.

### F49 — the absence of CI check runs shortly after a push is not evidence of failure.

Two pushes produced no check runs at all — not queued, not failed, never created —
while earlier commits on the same branch had run green. Actions permissions, the
workflow's state, the `pull_request` trigger and the pull request's head commit all
checked out healthy. An empty diagnostic commit produced check runs within about 25
seconds, and a run for the previously unscheduled commit appeared at the same moment.
GitHub Actions was lagging by a long interval, not failing.

**Disposition: accept** — recorded because the wrong reading ("CI is broken on this
branch", "a draft pull request stopped triggering workflows") was available and cheap
to reach. Any future gate that treats "no checks present" as "checks failed" would be
wrong here.

### F50 — the spike's route handlers answer 503, not 303, when the routing document is unavailable.

A deliberate deviation from a prescribed "303, with no exception". With no routing
configuration there is no page path to redirect to, and inventing one is exactly the
silent degradation the path helper exists to refuse. The response contract's "no
exception" governs operation outcomes; a handler that cannot name its own page is a
different case. Asserted in the page's own tests.

**Disposition: accept** — flagged rather than buried, because it is the one place the
shipped behaviour differs from the written contract by the builder's judgement.

### F51 — a model's default JSON-schema reference template does not match what the generated document carries.

The acceptance criterion for the generated document says its schemas must match
`NoteList.model_json_schema()`. Called with its default `ref_template`, that method
emits `#/$defs/NoteHead` references — which the document deliberately does not use.
It emits `#/components/schemas/{model}` instead, so that references resolve inside an
assembled OpenAPI document rather than inside a standalone model schema. The two are
the same schema with different reference bases, so a literal equality check between
them fails against a document that is entirely correct.

Shorthand rather than a defect, but it needs writing down before anyone builds that
comparison — and 0c3 or phase 2, publishing the `api` surface's own document, is
exactly who would.

**Disposition: amend the spec.** Name the `ref_template` in the clause, so that
"match `model_json_schema()`" means the comparison that can actually pass.

### F52 — a gate runner kept in a git-ignored directory does not exist inside a git worktree.

This repository's canonical gate runner is `.bureau/regression/run.sh`, a one-line
script that invokes `make lint typecheck test check`. `.gitignore` matches `.bureau/`,
so the directory is deliberately untracked. `git worktree add` populates a worktree
from tracked content only, which means the runner is **present in the main checkout and
absent from every worktree cut from it**.

Anything that resolves the project's gates by executing that path gets exit 127, a
missing interpreter target, not a failing suite. From an exit code alone those two are
indistinguishable, and the failure mode is worse than a plain error because it looks
like the gates ran and went red. Checking the runner from the main checkout, where the
file does exist, confirms it works and hides the problem entirely.

This is not hypothetical for this project: an earlier run spent a checkpoint
adjudicating a gate that came back red with exit 2 and could not be reproduced, and
this is a credible cause.

**Disposition: amend the plan.** Either track the runner, or have anything that depends
on it resolve the path in the repository root rather than in the worktree, and
distinguish "runner missing" from "suite failed" before reporting a red.

### F53 — the repository's default ports collide with ordinary developer machines, and the default test DSN points wherever port 5432 happens to lead.

`tests/conftest.py` defaults `RHEO_TEST_CLUSTER_DSN` to `localhost:5432`. On the machine
this run was built on, port 5432 was held by an unrelated project's Postgres container,
which rejected this project's credentials. The suite does not fail with "no database
here"; it fails with an authentication error against **someone else's database**, which
reads as a credential problem in this repository rather than as a port collision.

The same shape defeats `make spike` harder, and that half is recorded as F27: the
composition entry point hardcodes 8000 and 8100 and reads no environment, so the driver
cannot run at all while anything else holds 8000. Between them, this project assumes
three standard ports are free on a developer's machine and gives a usable override for
only one of them.

Worth stating as one finding because the two halves share a cause. The compose file
already parameterises its Postgres port, which is the pattern the rest should follow.

**Disposition: amend the plan.** Give the test DSN and the composition entry point the
same override the compose file already has, and make a connection failure name the port
it tried.

### F54 — a boundary guard that greps raw diff text matches the diff's own metadata.

The benchmark-boundary guard protecting this run scans for a list of forbidden symbols.
It greps the **raw text of the diff**, which includes git's own hunk headers. A hunk
header carries the enclosing syntactic context, so a diff touching a GitHub Actions
workflow produces lines like `@@ -82,6 +82,26 @@ jobs:` — and the guard read `jobs` as
evidence that this run had built a job subsystem. It had built nothing of the sort; the
match came from YAML belonging to an unrelated file, reproduced by git as context.

The same scan cannot distinguish an implementation from a sentence saying the thing was
deliberately not implemented. That matters more here than it would elsewhere, because
this document's entire purpose is to name what was cut, so the more honestly the cut is
documented the louder the guard complains. Four symbols matched on this run and every
one was prose or a comment stating the negative, verified line by line.

A guard that fires on its own documentation trains people to wave it through, which is
how a guard stops working. The structural checks alongside it stayed meaningful
throughout — the core schema still defines exactly six tables, and the two boundary
tests are untouched — because they assert about the shape of the code rather than about
the presence of a word.

**Disposition: amend the plan.** Scan added lines rather than raw diff text, and prefer
identifier-shaped symbols over English words for anything whose absence is also
discussed in prose.

## What this run adds to the trust boundary

`POST /internal/v1/operations/{name}` is the one piece of this run that is **not**
deleted at 0c0's branch cut, and it is the most security-relevant thing the run adds.
It is named here on purpose, so that it is met in a document rather than discovered
in a diff. It is not a numbered finding: it is not a disagreement between the
specification and the code, and it carries no disposition.

**What it does.** It dispatches any registered operation, by name, against a real
session. It is the web tier's only way to reach `dispatch()` at all — before this run
the internal listener served `/internal/v1/routing` and `/internal/v1/session` and
nothing else, so no operation was callable from a browser session in any topology the
repository could run (that is F2). It answers with the same envelope and the same
status mapping as the bearer `api` surface (`api_routes.envelope`,
`api_routes.outcome_status`), so the two surfaces cannot drift apart in how an
outcome is reported.

**What constrains it.** Three properties, each with its own test in
`tests/postgres/test_internal_operations.py`:

- It hangs off the same router as the other two internal routes
  (`apps/core/src/rheo_app_core/internal_routes.py:86`), so `require_internal_secret`
  gates it identically: `X-Rheo-Internal` is compared in constant time against a
  secret resolved fresh, at request time, from `internal.secret_ref`. With no header,
  or the wrong one, the request is 401 before the session is read at all
  (`test_no_internal_secret_is_401_before_the_session_is_even_read`,
  `test_a_wrong_internal_secret_is_401`). The listener is container-network only;
  compose never publishes port 8100.
- **Every** `context_from_session` refusal becomes a 401 *before* `dispatch()` is
  called, so the dispatcher is never handed anything but a real `WorkspaceContext`.
  The test for this spies on the dispatch symbol, asserts it was never reached across
  three different refusal shapes, and then ends with a positive control — a good
  session, 200, exactly one call — so the negative assertion cannot pass vacuously
  (`test_a_session_refusal_never_reaches_the_dispatcher`).
- It reads **no** workspace identifier from anywhere in the request: no path, query
  or body key naming a workspace, database, DSN, connection string or schema. The
  path carries the operation name, the query string and the body carry the operation's
  own input, and the workspace comes from the session row's `active_workspace_id` by
  way of `context_from_session` and from nowhere else. A payload that also names a
  workspace has those keys dropped by the operation's input model, because
  `RESERVED_INPUT_FIELDS` is refused at *registration* — no registered operation can
  declare one to read — and the write still lands in the session's own workspace. The
  test sends the workspace-naming keys through both the query string and the body,
  because the route merges both (`test_a_workspace_naming_payload_is_ignored`).

**It is not attack surface invented here.** `docs/architecture/overview.md:132`
already requires this listener to serve the operations the bearer surface serves; it
had simply never been built. But that line specifies *that*, not *how*. It fixes no
path, no headers, no refusal mapping, and no rule for merging a query string with a
body. So the shipped path `/internal/v1/operations/{name}`, the three headers it
reads, the mapping of every session refusal to 401, and the query-first-body-second
merge are **this run's own choices** — consistent with the architecture documents,
but not dictated by them. They are recorded in the route's own docstring as well as
here, because whoever inherits this route should know it is a decision rather than a
transcription. F20's proposed `servers` amendment is the natural place to make the
two listeners one contract with two bases.

One property it shares with the surface it mirrors, named rather than left implicit:
it calls the synchronous, database-blocking `dispatch()` directly inside an
`async def`, exactly as the bearer surface does. That is F42.

## Checked and found correct

Stated plainly, because a check that finds nothing is a real result and this run was
built to report one rather than to manufacture a concern.

- **The module-keyed audit sink holds.** The design predicted a specific failure for
  a single global sink slot: install the spike's sink and `core.token.issue` fails
  with a spike-schema error in a shipped test. With the spike registered and its sink
  installed for a whole test file's run, that failure did not occur —
  `core.token.issue` resolved to the null sink exactly as designed, and the only
  failures were F24's package-set assertions. Positive evidence for the seam,
  obtained by accident.
- **The HTTP contract matched what the web surface expected, with nothing to adapt**:
  the envelope shape, all eight path keys (five core and three spike), and the path
  template. The F20 asymmetry is held rather than fixed, and both sides say so in
  their own docstrings.
- **Three invented fields were dropped before they shipped.** A first generator
  emitted per-operation `x-rheo-module` and `x-rheo-safety-class` extensions and a
  top-level `x-rheo-modules` list. Nothing consumed any of them, and a field naming
  the loaded modules, the machine or the time would make the byte-exact CI diff report
  on the environment rather than on the contract. Removed; `x-rheo-generated` is the
  only extension the document carries.
- **One prescribed signature was checked rather than assumed.**
  `modules: Mapping[str, SurfaceConfig] = {}` looks like a mutable-default violation
  under this repository's lint configuration. It is not — the rule does not fire on a
  `Mapping`-annotated `{}` default — and that was established on a minimal
  reproduction rather than reasoned about.
