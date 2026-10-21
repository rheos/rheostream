# The Claude Code bridge

## What it is

`rheo-bridge` is a small command that runs on your own machine beside Claude Code. It
does two things:

- **Capture.** It reads what you type in your Claude Code sessions (the CLI and Desktop
  Code alike) and sends it to rheoStream as evidence, where Recallatron turns the
  explicit statements into memories. You never have to say "remember this".
- **Recall.** When you start a new session, it looks up the memories relevant to your
  first prompt and hands them to Claude before it answers.

It is the local producer of automatic memory. The other producer is rheo's own model
runs; both feed the same pipeline in the core.

## What you can do with it

- Set it up once: `rheo-bridge init` creates a private home directory and a machine key
  and prints the enrollment command for the operator.
- Capture one project directory, or every session on the machine (`--scope machine`).
- Install, preview (`--dry-run`) and remove the hooks without touching anything else in
  your Claude Code settings.
- Turn first-message recall on or off.
- Check `status`: session counts, whether it is holding, its token's expiry. It never
  prints a hash, a key, a path or any text.
- Run one pass by hand with `drain`, and uninstall cleanly, optionally purging what the
  server has not yet acknowledged.

## How capture works

**The hook does almost nothing, on purpose.** A standard-library-only hook runs when a
Claude Code session stops or ends. It appends one line to a local spool (the event, a
salted hash of the session id, the transcript's path and the time), starts the worker
in the background if none is running, prints nothing and exits 0. It never slows your
session down.

**The worker reads only what it was pointed at.** It reads only the transcripts the
spool names, and only inside Claude Code's project directory for the enrolled directory
(or, in machine scope, any project directory there). A path anywhere else is refused and
counted. From those transcripts it takes the lines a person typed, runs the same
`sanitize()` the server uses, and posts a batch to one operation,
`core.evidence.ingest`.

**Nothing that leaves names anything.** Each record's key is `cc1:` plus an HMAC-SHA256,
under a 32-byte machine key that never leaves your laptop, of the session id and message
id. The server sees no session id, message id, file path or project name. The enrolled
directory is known to the server only as a fingerprint of its path. Because keys do not
change with the sanitizer or the model, a resent range is answered from what the server
already holds and writes nothing new.

**A crash re-reads, never skips.** The worker reads from a byte cursor that moves only in
the same local commit as the server's acknowledgement. One pass is bounded at 1 MiB and
1,000 records, and a line still unsent after 24 hours becomes a gap.

**Enrollment binds one account, one machine, one directory.** The operator enrolls the
pair of fingerprints for an account in a workspace and pipes the result straight into
`rheo-bridge set-token`. The enrollment's purpose is `internal_analysis` and every
record it admits is visible only to the enrolled account. The bridge's token can do
exactly one thing, ingest evidence, and is refused for everything else.

**The server trusts the enrollment, not the request.** On ingest, the core finds the
enrollment bound to the presenting token, compares the two fingerprints, takes the
speaker and audience from the enrollment row, and sanitizes every record again with its
own code, whatever version the bridge ran. The answer lists which keys were accepted,
deferred, turned into gaps or dropped, and carries no text and no counts.

**Two gates, each its own decision.** Nothing is recorded until the hook is installed in
the real directory, and nothing becomes memory until the operator configures an
extraction provider. Between the two, the server defers every key and the bridge holds:
it waits 15 minutes, doubling up to 6 hours, and then probes with a single item rather
than resending its backlog. The extraction itself runs one tool-less model turn per batch,
in an empty throwaway directory, with a token that can call nothing.

**Gaps are reported, not hidden.** When a transcript shrinks, is replaced or vanishes
under the cursor, or a line ages past the local limit before it can be sent, the bridge
sends a content-free gap instead. What leaves no trace at all (every hook call for a
session failing) cannot be reported, and the design says so rather than claiming every
turn arrives.

**Revoking stops pending work.** Revoking an enrollment, or its token, or letting the
token expire, makes every still-pending row settle as unverified at its next claim.
Rotating the token keeps pending rows valid. `rheo doctor` flags a bridge token that is
revoked, expired or within 7 days of expiry.

## How recall works

A second standard-library hook runs when you submit a prompt. On the first prompt of a
session that is not a slash command, it calls `recallatron.memory.recall` with your
prompt as the query and prints the results as additional context, which Claude Code adds
to the session. Each item shows its kind, date, title, up to 400 characters of body, and
its reference, under a header that calls them dated claims to check, not instructions.

- Only items the reranker scores at or above a threshold are kept; an answer with no
  reranker scores injects nothing.
- At most 5 items and 2,000 characters, with a 3-second timeout (never more than 5).
- It runs only in interactive Claude Code, not headless `claude -p` runs, and an
  environment variable turns it off for any run.
- It fires once per session, enforced by a local marker, and every failure is silent.
- It uses its own read-only token, never the capture token.
- The recall operation keeps no audit row, so your prompt is not stored on the server.

## Why it's built this way

Memory you have to remember to save is memory you do not have. Capture makes saving
ambient, and recall means a new session starts already knowing what you decided last
week, without pasting anything.

The hard part is doing that without building a surveillance tool. So the design keeps
everything identifying on the laptop, sends keyed hashes instead of ids, lets the server
take identity only from an enrollment an operator created, captures only what a person
typed, and makes every path from "typed" to "remembered" pass through two separate
switches.

## Where to look

- Specification: [the local Claude Code bridge](../architecture/runtime-and-mcp.md#the-local-claude-code-bridge),
  [automatic memory's two producers](../architecture/runtime-and-mcp.md#automatic-memorys-two-producers),
  [automatic acceptance](../architecture/memory.md#provenance-and-links),
  [ambient capability manifest](../acceptance/ambient-capability-manifest.md)
- Code: [bridge](../../apps/bridge/src/rheo_bridge),
  [capture hook](../../apps/bridge/src/rheo_bridge/hook.py),
  [recall hook](../../apps/bridge/src/rheo_bridge/recall_hook.py),
  [worker](../../apps/bridge/src/rheo_bridge/worker.py),
  [evidence ingest](../../packages/core/src/rheo_core/evidence/ingest.py),
  [sanitize](../../packages/core/src/rheo_core/evidence/sanitize.py),
  [local authority](../../packages/core/src/rheo_core/evidence/local_authority.py)
- Tests: [bridge hook](../../tests/test_bridge_hook.py),
  [bridge worker](../../tests/test_bridge_worker.py),
  [recall hook](../../tests/test_bridge_recall_hook.py),
  [ingest round trip](../../tests/postgres/test_bridge_ingest_roundtrip.py),
  [evidence ingest](../../tests/postgres/test_evidence_ingest.py),
  [local authority](../../tests/postgres/test_local_authority.py)
