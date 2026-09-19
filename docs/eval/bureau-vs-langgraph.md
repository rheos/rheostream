# Agent-framework benchmark — Bureau vs LangGraph, and the Bureau across runtimes

**Status:** draft idea + plan, pre-registration. Not started (prep pending). Committed to `main`.
**Arena:** this repository's phased build plan.
**Owner:** maintainer.

## Why this exists (the idea)

The maintainer builds custom multi-agent orchestration (the Bureau) rather than the
market-standard LangChain / LangGraph. Two honest questions fall out of that:

1. **Framework.** On real software-build work, how does the Bureau compare to a canonical
   LangGraph agent on speed, cost, correctness, and durability?
2. **Runtime.** Which model runtime should the Bureau itself run on: Claude, Codex, or Grok?

Neither is a rigged win. Three outcomes for question 1 are all acceptable and none is assumed:
LangGraph carries ideas worth folding back into the Bureau; LangGraph is the better tool and
worth adopting; or the Bureau holds. Question 2 is genuinely open and directly informs where the
maintainer routes spend across flat-rate coding subscriptions.

### Market grounding (why this is worth the effort, and what it is *not*)

Checked against the maintainer's own scored Upwork funnel (270 recent jobs): LangChain/LangGraph
appears in ~1% of them (3 of 270), and those three are low-value. What dominates the jobs that
fit are agentic/multi-agent (32), Claude API / Claude Code (28), RAG (19), and MCP (7). So the
funnel is buying custom agent orchestration, which is what the Bureau already does.

The implication: learning LangGraph here is for keyword reach into an adjacent market, for
honest self-assessment against the standard tool, and for mining ideas back into the Bureau. It
is **not** a response to overwhelming funnel demand, because that demand is not in the funnel.
The most defensible proposal line it produces is "I benchmarked LangGraph head-to-head against my
own multi-agent framework," which is a stronger senior signal than "I know LangGraph."

## Two studies, one arena

One arena, one metric set, one telemetry wrapper. Two single-variable studies that share the
Bureau/Claude baseline cell:

| Study | Holds constant | Varies | Answers | Build cost |
|---|---|---|---|---|
| **A — framework** | model (Claude) | Bureau vs LangGraph | Is the Bureau's orchestration better than the market tool, model held equal? | LangGraph harness (real) |
| **B — runtime** | framework (Bureau) | Claude vs Codex vs Grok | Which runtime should the Bureau run on? | config only (Grok = exploratory) |

Deliberately not a full cross-product (Bureau/Claude × Bureau/Codex × LangGraph × …). That
explodes cost for little extra insight; two single-variable studies anchored on a shared baseline
cell are cleaner and cheaper.

## Why this repo is the arena

- The [build plan](../requirements/build-plan.md) carries continuously-numbered acceptance
  criteria, each checkable by inspection or by a test, mapped to FR 1-53. That is a
  framework-neutral, objective specification: every competitor gets the identical, high-quality
  input, and correctness is scored by tests, not judgement.
- The work is real work the project wants done anyway, not throwaway benchmark tasks.
- **Known limitation (incumbency):** this repo was scaffolded and cold-reviewed with the Bureau,
  so it carries Bureau-shaped conventions. That is a home-field advantage; the controls below
  address it head-on rather than hiding it.

## Competitors (pinned before any run)

**Bureau/Claude** — the existing framework on its default runtime; Conductor spawns specialists
in fresh contexts, cold review and a critic loop. Pinned by commit SHA. This is the shared
baseline cell for both studies.

**Bureau/Codex** — same framework, `scripts/run-start.sh … --runtime openai`. A first-class,
regression-tested host (codex cold-reviewer adapter, host transport contract, strict verdict
schema, e2e). Provider-neutral tiers resolve to gpt-5.6-terra / gpt-5.6-sol / gpt-6-astra.

**Bureau/Grok** — same framework, `--runtime grok`, run via **Grok Bot** (the maintainer's Grok
host path). Tiers resolve to grok-4.3 / grok-4.6. Treated as an **exploratory, asterisked** arm:
the host is the least mature (its Task tool currently accepts only `sand-default`, so the
grok-4.3/4.6 tier split can't be enforced on live spawn; the cold-reviewer helper may lack a Grok
provider path and fail the checkpoint closed). See open items on reconciling the Grok Bot
transport. SuperGrok, the consumer subscription, is not the vehicle and is not required.

**LangGraph/Claude** — built the idiomatic way the market uses it, not a bespoke harness:
`langgraph.prebuilt.create_react_agent` or a hand-written `StateGraph` with a `ToolNode`; a
checkpointer (`MemorySaver` dev, `PostgresSaver` durable) for state and resume; file/shell/test
and git/open-PR tools; `ChatAnthropic` as the model (control 1); LangSmith tracing on, one
project per trial; invocable behind FastAPI; pinned library and model versions. Building this
harness is the critical-path dependency for Study A and is itself the LangGraph learning
deliverable.

## Controls (what keeps it honest)

1. **Model held constant in Study A** — `ChatAnthropic`, identical model id both sides. Study A
   compares orchestration, not models. (Study B deliberately varies the runtime instead; see the
   note below.)
2. **Same starting commit** — every competitor branches from an identical pre-task commit, each
   in its own worktree. None sees another's work.
3. **Same spec input** — the target criterion text plus the repo's `AGENTS.md` / `CLAUDE.md`
   conventions. No extra hints to anyone.
4. **Same tool surface** — edit files, run tests, run git, open a PR. Equalize capability so the
   comparison is orchestration/runtime, not tool access.
5. **Incumbency probed** — run at least one tier on a neutral repo. If the gap moves off home
   turf, report it.
6. **Blind quality scoring** — a cold judge model scores code quality on unlabeled diffs.
   Correctness is objective (criteria tests), so this is secondary.
7. **N ≥ 3 trials per cell** — agentic runs are high-variance; report median and spread, never a
   single run.
8. **Pre-registered** — task selection, criteria, and metrics fixed in this document before any
   run.

**Study B honesty note.** Swapping `--runtime` swaps the tier→model map, the reasoning-effort
knobs, *and* the host transport at once. So Study B compares "the Bureau as it runs on Claude"
vs "on Codex" vs "on Grok," not Opus-vs-GPT-vs-Grok in a vacuum. That is the correct unit,
because it is exactly the decision the maintainer actually makes.

## Task matrix

Rows are difficulty tiers mapped to this repo's real criteria; columns are the competitors in
the study; each cell is N trials.

| Tier | Task | Source criterion | Objective score |
|---|---|---|---|
| **A — bug fix** | Reintroduce a known regression; make the test green again | exactly-once outbox delivery (criterion 11) | criterion test pass/fail + diff size |
| **B — small feature** | One self-contained criterion, from scratch | worker leases + bounded retries + cancellation (criterion 12); or GitHub OAuth + role check (criterion 8) | criterion test suite pass rate |
| **C — larger build** | A multi-criterion subsystem | durable work end-to-end: outbox + worker + operation records + audit (criteria 11-14) | subsystem criteria pass rate + integration |

The criteria named above illustrate tier *size*; they are not the actual tasks. The real tasks
bind to the benchmark phase when it is chosen (see next section), and Tier A needs landed code to
break.

## Which phase the benchmark runs on

The benchmark does **not** run on Phase 1 (nearly built already) and **not** on the immediate
next phase. It runs on a **future, not-yet-built phase, chosen later**, after the Bureau has
built the system through several more phases.

Rationale:

- **Realism.** By then the task is "add a well-specified feature into a mature, conventions-
  established codebase," which mirrors real client work (you join an existing system) and is a
  fairer test of orchestration than building on a near-greenfield skeleton.
- **No idle time.** The wait is exactly the window needed to build the LangGraph harness (see the
  sequence below), so nothing sits idle.
- **Incumbency, unchanged.** The codebase conventions stay Bureau-authored either way; that is
  already a documented limitation, controlled by the neutral-repo probe. A mature codebase just
  means every competitor joins the same established base from the identical commit, adding no
  per-task advantage.

Binding tiers to the chosen phase, done at selection time:

- **Larger build (Tier C):** a whole module-shaped phase is the clean unit. Candidates as they
  arrive: Phase 4 (optional job-search workflow), Phase 5 (the work-in-motion module).
- **Small feature (Tier B):** a self-contained slice of the chosen phase.
- **Bug fix (Tier A):** a seeded regression in already-landed code (Phase 1 onward), so it can run
  once any phase has landed.

The exact phase and criteria are pre-registered in this doc at selection time, before any run.

## Metrics (recorded per trial)

- **Correctness** — % of target acceptance criteria passing.
- **Silent-failure survival** — for the stateful tiers, did the non-obvious edge case (double
  write / lost event / missed cancellation) ship undetected.
- **Cost** — total tokens and dollars. Bureau via its run accounting (transcript dedup; per-model
  pricing already in `config/model-pricing.json`, including a Grok byte estimate); LangGraph via
  LangSmith usage.
- **Wall-clock** — submit to PR open.
- **Human-wait** — time blocked on a human checkpoint (Bureau checkpoints, LangGraph HITL
  interrupts).
- **Rework loops** — critic / reflect iterations to green.
- **Durability** — after the PR lands, run the next unit on top and measure churn on the prior
  PR's lines plus any regression in earlier criteria.
- **Quality (blind)** — cold-judge rubric score on the unlabeled diff.

## Instrumentation

A thin shared telemetry wrapper writes one run-ledger record per trial (JSON, same schema for
every competitor): start/end commit, wall-clock, tokens, dollars, PR URL, per-criterion results,
rework loops. Reuse the `bureau-run-eval` metric definitions so numbers line up with existing
framework evaluations.

- Bureau (all runtimes): read `.bureau/runs/<run>/state.json`, `log.md`, `accounting.json`. The
  accounting is already multi-runtime, so Study B's cost data comes almost for free.
- LangGraph: one LangSmith project per trial plus a callback that writes the same ledger fields.

## Expected results (pre-registered, may be wrong)

- **Study A** — hypothesis is a crossover: LangGraph cheaper and faster on Tiers A and B, the
  Bureau's independent review earning out on durability and silent-failure avoidance at Tier C.
  Recorded up front so a result either way is legible, including the one where this is wrong.
- **Study B** — genuinely open. No prediction on whether Claude or Codex builds better or cheaper
  on the Bureau. That uncertainty is the point.

## Sequence

The run is gated on two things, not a calendar: the Bureau must be several phases in (a mature
codebase), **and** the test prep must be finished. It fires at the first suitable future phase
after both hold.

**Prep (now, while the Bureau keeps building):**

- Build the canonical LangGraph SWE-agent harness and the shared telemetry wrapper.
- Define the seeded-regression fixture for Tier A.
- These are the gating deliverables; the benchmark cannot run until they exist.

**Select (once prep is done and the Bureau is several phases in):**

- Pick the next suitable future phase and pre-register its tasks in this doc.

**Run (at that phase):**

- **Run 1 (config-only): Study B first** — Bureau/Claude vs Bureau/Codex, one tier, 3 trials
  each. The most actionable answer for spend, and it exercises the arena and telemetry wrapper
  before the harness comparison.
- **Run 2:** Study A at the same tier (Bureau/Claude vs LangGraph/Claude).
- **Run 3:** fill Tiers A and C for both studies; run the durability pass.

The Bureau/Grok arm slots into Study B once its transport and maturity gaps are confirmed. The
publishable article derives from the run ledger and artifacts (article draft lives in the devweb
repo, not here).

## Open decisions (for the maintainer)

- **Grok Bot transport.** GROK.md currently frames the Grok host as "Cursor Grok Bot," but the
  maintainer runs Grok via Grok Bot without a Cursor subscription. Confirm the actual Grok Bot
  path and update GROK.md if its wording is stale, before the Grok arm runs.
- **Codex still active?** Study B's Codex arm assumes a live Codex (Plus) runtime. Confirm.
- **Tier B first cut:** proposed criterion 12 (worker leases + bounded retries + cancellation) —
  self-contained, genuinely stateful, strong test hooks. Confirm or swap.
- **Pins:** model ids per runtime, LangGraph checkpointer choice, library versions.
- **Trial budget:** 3 per cell is the floor; token cost scales roughly linearly.
- **Publication — resolved (public).** This doc lives in the public repo, the job-funnel note
  included; the maintainer chose to keep it public (2026-09-09).

## Non-goals

- Not a general "which framework wins" verdict; scoped to software-build agents on this repo.
- Not a LangGraph tutorial.
