import type { ShellApi } from "@rheo-stream/web-contract/screen";

import { callChecked } from "../../call";
import { loadCoverage, type CoverageState } from "../../components/coverage";
import { characterCount, excerpt, formatUtc } from "../../format";
import { isRecallResult } from "../../guards";
import { RECALL } from "../../operations";

/**
 * Search: the application's one search input, as a plain GET form. `q` is trimmed
 * before anything else, and only a query the operation would accept is sent.
 */

/** The input bound, which is also `recall`'s own query length bound. */
export const QUERY_MAX_LENGTH = 1000;
/** How many results one search asks for. */
export const RECALL_K = 10;
/** How much of each body a result shows. */
export const EXCERPT_LENGTH = 280;

/**
 * The five empty-result states, keyed on `(strategy, dense_available)`. Each says
 * which search actually ran, so none claims a word or meaning search that did not.
 */
export const EMPTY_COPY = {
  hybridDegraded:
    "No word matches. Meaning search isn't available in this workspace, so memories that match only by meaning can't be found.",
  denseDegraded:
    "Meaning search isn't available in this workspace, so this search could not look at any memories.",
  lexical: "No word matches. This workspace searches by words only.",
  dense: "Nothing was close enough in meaning.",
  hybrid: "Nothing matched by words or by meaning.",
} as const;

export type EmptyKind = keyof typeof EMPTY_COPY;

export function emptyKind(strategy: string, denseAvailable: boolean): EmptyKind {
  if (strategy === "lexical") return "lexical";
  if (strategy === "dense") return denseAvailable ? "dense" : "denseDegraded";
  if (strategy === "hybrid" && !denseAvailable) return "hybridDegraded";
  return "hybrid";
}

export interface SearchResultRow {
  ref: string;
  kind: string;
  title: string;
  excerpt: string;
  when: string;
  dateTime: string;
  href: string;
}

export interface Provenance {
  strategy: string;
  denseAvailable: boolean;
  lexicalCount: number;
  denseCount: number;
}

export type ResultsState =
  | { state: "prompt" }
  | { state: "too-long" }
  | { state: "results"; rows: SearchResultRow[]; provenance: Provenance }
  | { state: "empty"; empty: EmptyKind; provenance: Provenance }
  | { state: "error" };

export interface SearchState {
  action: string;
  query: string;
  results: ResultsState;
  coverage: CoverageState | null;
}

type Query = Readonly<Record<string, string | undefined>>;

async function loadResults(shell: ShellApi, query: string): Promise<ResultsState> {
  if (query === "") {
    return { state: "prompt" };
  }
  if (characterCount(query) > QUERY_MAX_LENGTH) {
    return { state: "too-long" };
  }
  const called = await callChecked(shell, RECALL, { query, k: RECALL_K }, isRecallResult);
  if (called.state !== "ok") {
    return { state: "error" };
  }
  const { items, provenance } = called.value;
  const summary: Provenance = {
    strategy: provenance.strategy,
    denseAvailable: provenance.dense_available,
    lexicalCount: provenance.arms.lexical,
    denseCount: provenance.arms.dense,
  };
  if (items.length === 0) {
    return {
      state: "empty",
      empty: emptyKind(provenance.strategy, provenance.dense_available),
      provenance: summary,
    };
  }
  return {
    state: "results",
    provenance: summary,
    rows: items.map((item) => {
      const at = item.occurred_at ?? item.recorded_at;
      return {
        ref: item.ref,
        kind: item.kind,
        title: item.title,
        excerpt: excerpt(item.body, EXCERPT_LENGTH),
        when: formatUtc(at),
        dateTime: at,
        href: shell.href("item", { ref: item.ref }),
      };
    }),
  };
}

export async function loadSearch(shell: ShellApi, query: Query): Promise<SearchState> {
  const trimmed = (query.q ?? "").trim();
  const [results, coverage] = await Promise.all([
    loadResults(shell, trimmed),
    loadCoverage(shell),
  ]);
  return { action: shell.href("search"), query: trimmed, results, coverage };
}
