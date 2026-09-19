# Run 1a0: module contract planning artifacts

Run 1a0 closed out as a planning run on 2026-09-19 (UTC). It produced a ratified
specification, an eight-phase plan and eight scoped prompts for the module
contract (public criteria 24, 25, 26 and 33), and no implementation. The
follow-on build run executes `prompts.md` in order; issue #93 tracks it.

The three files here are verbatim copies of the run's artifacts at close-out.
They were committed so the plan survives the machine it was written on; the
run directory that produced them is not versioned. Paths such as
`RUN_DIR/state.json` refer to that run directory, not to this folder.

- [spec.md](spec.md): requirements, acceptance criteria 1-14, architecture decisions.
- [plan.md](plan.md): the eight phases and their sequencing notes.
- [prompts.md](prompts.md): the scoped prompts, one per phase.

Criterion 24 is closed only in its module-contract half by this plan. The
interface-contribution half belongs to the memory UI run.
