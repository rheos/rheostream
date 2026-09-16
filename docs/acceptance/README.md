# The acceptance matrix

## What this is

`phase-1-matrix.md` is phase one's exit deliverable. For each of criteria 1 through 23
and criterion 69 it records the test or CI step that demonstrates the criterion, and the
mutation that proves that demonstrator bites: a real edit that was applied to a real
working tree, watched go red, and reverted.

## What this is not

**Not a coverage report.** Coverage answers "is this line executed by some test". The
matrix answers a narrower and more useful question: "when the behaviour criterion N names
is removed, does the test that claims criterion N go red, and with what message". A line
can be covered by a dozen tests and still have nothing pinning the behaviour the criterion
cares about.

**Not a naming census.** No test function in this repository carries any of criteria 1-23 in
its **name**, including the four built with full mutation demonstrations in runs 0c1 and 0c2.
An index keyed on names would report a false gap and its obvious fix would be to sprinkle
criterion numbers into docstrings, which makes the index green without testing anything. That
was proposed, measured and rejected on 2026-09-16. Do not rebuild it.

The qualifier "in its name" is load-bearing and the looser claim is false: criterion numbers
do appear inside test bodies and docstrings, for instance the `covers=11` and `covers=14`
arguments at `tests/test_absent_behaviour.py:200,208,216`. That is the point. A census reads
names, the numbers live elsewhere, and the gap it reports is an artefact of where it looked.

**Not a claim that the guard re-ran anything.** `tests/test_acceptance_matrix.py`, which
chunk 10 writes, parses this file and checks only what is mechanically true without
running a criterion: that every criterion number is present exactly once, that every
demonstrator still resolves to a real test node or workflow step, that every mutation hunk
still applies, and that every non-`deferred` row names a performer. **It never asserts that
a mutation was actually performed.** That claim is human-signed by the chunk named in
`Performed by`, and the observed failure line in `Cost` is the evidence for it.

Even with the guard whole, a row can be green while its criterion is undemonstrated: the
hunk applies, the demonstrator resolves, and the test still does not exercise the
criterion. No mechanical check closes that gap. The honest claim for this document is "no
row is stale", not "every criterion is demonstrated".

## The per-row grammar

Copied verbatim from the run's own prompt index, so the two cannot drift:

````markdown
### Criterion <N>

**Text:** "<verbatim quote from build-plan.md>" (`build-plan.md:<start>-<end>`)

**State:** complete | partial | deferred

**Demonstrator:** `pytest:<node id>` | `vitest:<file> > <test name>` | `ci:<job> / <step name>`
(one or more, newline-separated, each on its own line prefixed `- `)

**Mutation:**
```diff
<the literal `git diff` output for this row's mutation — captured with `git diff`, never
hand-typed>
```

**Cost:** `<demonstrator>` — first observed failure line: `<the actual line pytest/vitest/the CI
log printed>`

**Performed by:** <chunk> (<YYYY-MM-DD>)[, <chunk> (<YYYY-MM-DD>)]
````

A `deferred` row (15, 16) has no `Mutation`, `Cost`, or `Performed by` — write `none` for each —
and adds a **Note:** line naming 0c4 in its own words. A `partial` row (17) carries real
`Demonstrator`/`Mutation`/`Cost`/`Performed by` for the half that IS demonstrated, plus a
**Note:** naming the blocked half and 0c4.

**Capture every mutation's diff with `git diff -- <path>` after applying it locally, before
reverting — never hand-type a hunk.** The three-command reproduce loop below and chunk 10's
`git apply --check` guard both depend on the row's fenced block being a real, applicable patch; a
hand-typed hunk risks a context-line mismatch that fails `git apply --check` even when the
mutation itself is correct.

### Three rules a parser of this file needs, fixed here so the guard has one shape

**Fields are delimited by their bolded labels.** A field owns every line from its own
`**Label:**` to the next one. `Demonstrator` and `Cost` both use `- ` bullets and both
carry demonstrator ids in backticks, so a parser that globs for `` - `pytest:… ` `` across
a whole row will read cost lines as demonstrators. Scope the search to the `Demonstrator`
field.

**A `pytest:` id may name a parametrised family.** `tests/postgres/test_context_routing.py::test_registering_a_reserved_input_field_is_refused_naming_it`
is pasteable into pytest and runs all twelve cases, but `pytest --collect-only` lists only
the twelve bracketed ids. An id resolves when it is collected exactly **or** when some
collected id begins with it followed by `[`. Naming the family rather than one case is
deliberate where the whole family bites.

**A row may carry more `Demonstrator` entries than its `Mutation` reddens.** The mutation is
one hunk; a criterion can have several true demonstrators, and `Cost` is what records which
of them actually went red. A demonstrator with no cost line is still a real demonstrator and
still has to resolve.

### How a `ci:` demonstrator is spelled

The part before the ` / ` is the **job key** in `.github/workflows/repository-checks.yml` —
`repository-checks`, `docker`, `python`, `web` — not the job's human `name:`. The part after
is the step's `name:` exactly as written. One rule, so the guard can resolve the pair from
the parsed YAML without guessing which of the two spellings a row meant.

## Reproducing any row

Save the row's fenced `Mutation` block to a file, then run these three commands from the
repository root:

```sh
git apply /tmp/row.diff
uv run pytest -q "tests/postgres/test_tokens.py::test_expired_presentation_refused_with_unchanged_snapshot"
git apply -R /tmp/row.diff
```

The second command is the row's own `Demonstrator`; criterion 9's is shown as the worked
example. Substitute the node id or ids that row names. A `ci:` row's demonstrator is a
workflow step, so run that step's own command instead: `python3 scripts/check_repository.py`,
`docker build -f Dockerfile .`, `RHEO_ROUTING_MODE=subdomain pnpm -C apps/web test`, or
`python3 scripts/check_routing_literals.py`. The third command reverts; `git checkout --
<path>` does the same job when the hunk touches one file.

**Export `RHEO_TEST_CLUSTER_DSN` before the second command.** `tests/conftest.py` reads that
variable and otherwise falls back to `localhost:5432/postgres`. On this project's development
machine 5432 is held by an unrelated service and the test cluster runs on **5433**, so a run
without it fails with a message that reads like a broken environment rather than a wrong
port. The value belongs in the gitignored `.env`, whose shape `.env.example` documents, and
`set -a; . ./.env; set +a` loads it. Under zsh the `./` is not optional: `.` with an argument
containing no slash searches `$PATH` and reports `no such file or directory: .env`.
