import type { ShellApi } from "@rheo-stream/web-contract/screen";

import { callChecked } from "../../call";
import { isDedupCandidates, isMemoryItem } from "../../guards";
import { DEDUP_CANDIDATES, GET } from "../../operations";

/**
 * Possible duplicates: a read-only list. The loader calls `dedup_candidates` and then
 * `get` for titles, and nothing else; there is no merge, confirm or supersede path
 * from this screen, not even a disabled one.
 */

/** How many pairs one view asks for; two `get` calls per pair at most. */
export const PAIR_LIMIT = 10;

export interface PairSide {
  ref: string;
  /** `null` when this side's `get` refused or was unavailable. */
  title: string | null;
  href: string;
}

export interface PairRow {
  a: PairSide;
  b: PairSide;
  similarity: number;
}

export type DuplicatesState =
  | { state: "pairs"; pairs: PairRow[] }
  | { state: "empty" }
  | { state: "error" };

async function titleOf(shell: ShellApi, ref: string): Promise<string | null> {
  const called = await callChecked(shell, GET, { ref }, isMemoryItem);
  return called.state === "ok" ? called.value.title : null;
}

export async function loadDuplicates(shell: ShellApi): Promise<DuplicatesState> {
  const called = await callChecked(
    shell,
    DEDUP_CANDIDATES,
    { limit: PAIR_LIMIT },
    isDedupCandidates,
  );
  if (called.state !== "ok") {
    return { state: "error" };
  }
  const { pairs } = called.value;
  if (pairs.length === 0) {
    return { state: "empty" };
  }
  // Every title at once, one call per distinct reference: at most two per pair.
  const refs = [...new Set(pairs.flatMap((pair) => [pair.ref_a, pair.ref_b]))];
  const titles = new Map(
    await Promise.all(refs.map(async (ref) => [ref, await titleOf(shell, ref)] as const)),
  );
  const side = (ref: string): PairSide => ({
    ref,
    title: titles.get(ref) ?? null,
    href: shell.href("item", { ref }),
  });
  return {
    state: "pairs",
    pairs: pairs.map((pair) => ({
      a: side(pair.ref_a),
      b: side(pair.ref_b),
      similarity: Math.round(pair.score * 100),
    })),
  };
}
