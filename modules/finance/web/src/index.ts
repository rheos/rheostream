import { createElement } from "react";
import type { Screen, ShellApi } from "@rheo-stream/web-contract/screen";

import { AttentionView } from "./attention";
import { CoverageView } from "./coverage";
import { OwnerOnly } from "./frame";
import { loadAttention, loadCoverage, loadOwed, loadUpcoming } from "./load";
import { OwedView } from "./owed";
import { PaidForm } from "./paid";
import { UpcomingView } from "./upcoming";

/**
 * Finance's screens, keyed by the `screen` identifier each route in the module's
 * manifest names. Every one is an async server component (the one client script is
 * the Paid and Undo button on Owed, `PaidForm`), and every one is the owner's alone: a
 * member gets the owner-only state and no read is made, though core would refuse each
 * read for a member anyway.
 */

/** This year in the viewer's zone, or UTC when the host does not know it. */
export function thisYear(shell: ShellApi, now: Date = new Date()): number {
  const year = new Intl.DateTimeFormat("en-CA", {
    timeZone: shell.timeZone ?? "UTC",
    year: "numeric",
  }).format(now);
  return Number(year);
}

function yearFrom(value: string | undefined, fallback: number): number {
  const n = Number(value);
  return Number.isInteger(n) && n >= 1900 && n <= 2999 ? n : fallback;
}

const owed: Screen = async ({ shell }) =>
  shell.role !== "owner"
    ? createElement(OwnerOnly)
    : createElement(OwedView, { shell, state: await loadOwed(shell) });

const attention: Screen = async ({ shell }) =>
  shell.role !== "owner"
    ? createElement(OwnerOnly)
    : createElement(AttentionView, {
        shell,
        state: await loadAttention(shell),
        year: thisYear(shell),
      });

const coverage: Screen = async ({ shell, query }) => {
  if (shell.role !== "owner") return createElement(OwnerOnly);
  const year = yearFrom(query.year, thisYear(shell));
  return createElement(CoverageView, { shell, state: await loadCoverage(shell, year) });
};

const upcomingScreen: Screen = async ({ shell, query }) =>
  shell.role !== "owner"
    ? createElement(OwnerOnly)
    : createElement(UpcomingView, {
        shell,
        state: await loadUpcoming(shell),
        account: typeof query.account === "string" ? query.account : null,
      });

export const screens = { owed, upcoming: upcomingScreen, attention, coverage } satisfies Record<string, Screen>;

/** The one form component the manifest names, for both owner writes. */
export const components = { PaidForm };
