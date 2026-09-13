import type { OperationResult } from "@/lib/spike/client";

/**
 * The spike's note list and its empty state (0v, C4) — deleted at 0c0's branch
 * cut.
 *
 * The item type is **read off the generated document**, not restated here:
 * `OperationResult<"spike.note.list">` is derived from `api-types.ts`, which
 * `openapi-typescript` derives from `openapi.json`, which `rheo openapi` derives
 * from `spike.note.list`'s own output model. Rename a field in
 * `modules/spike/src/rheo_spike/operations.py` and this file stops compiling.
 *
 * An empty workspace is a **success**, not a refusal (FR 10): a workspace with no
 * notes answers `{"notes": []}`, and this renders the empty state for it rather
 * than an error. Telling those two apart is the whole reason the empty state is
 * one of FR 13's four required states.
 *
 * A plain synchronous server component — no `"use client"`, no state, no
 * `fetch` — so it renders identically under the `curl` driver, which runs no
 * client JavaScript at all.
 */

type Notes = OperationResult<"spike.note.list">["notes"];

export function SpikeNotes({ notes }: { notes: Notes }) {
  if (notes.length === 0) {
    return <p>notes: none yet</p>;
  }
  return (
    <ul>
      {notes.map((note) => (
        <li key={note.ref}>
          <span>{note.body}</span> <small>{note.ref}</small>
        </li>
      ))}
    </ul>
  );
}
