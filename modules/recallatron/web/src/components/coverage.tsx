import type { ShellApi } from "@rheo-stream/web-contract/screen";

import { callChecked } from "../call";
import { isEmbeddingCoverageReport } from "../guards";
import { EMBEDDING_COVERAGE } from "../operations";
import styles from "./coverage.module.css";

/**
 * How much of the workspace meaning search can reach, for the owner only.
 *
 * `null` means the figure is not shown at all: a non-owner never triggers the call and
 * the view renders nothing in its place, not a hidden element and not an error. The
 * operation is owner-only too, so this is the screen matching the permission rather
 * than the only thing enforcing it.
 */
export type CoverageState =
  | { state: "counts"; embedded: number; live: number }
  | { state: "off" }
  | { state: "error" };

export async function loadCoverage(shell: ShellApi): Promise<CoverageState | null> {
  if (shell.role !== "owner") {
    return null;
  }
  const called = await callChecked(shell, EMBEDDING_COVERAGE, {}, isEmbeddingCoverageReport);
  if (called.state !== "ok") {
    return { state: "error" };
  }
  const { embedded, live } = called.value;
  if (embedded === null || live === null) {
    return { state: "off" };
  }
  return { state: "counts", embedded, live };
}

function coverageText(coverage: CoverageState): string {
  switch (coverage.state) {
    case "counts":
      return `${coverage.embedded} of ${coverage.live} memories are searchable by meaning`;
    case "off":
      return "meaning search is off: no embedding provider is configured";
    case "error":
      return "meaning search coverage is unavailable right now";
  }
}

export function CoverageFigure({ coverage }: { coverage: CoverageState | null }) {
  if (coverage === null) {
    return null;
  }
  return (
    <p className={styles.figure} data-coverage={coverage.state}>
      {coverageText(coverage)}
    </p>
  );
}
