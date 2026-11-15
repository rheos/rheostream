import { describe, expect, it } from "vitest";

import {
  bucket,
  coverage,
  obligation,
  owed,
  relativeDays,
  shortDate,
  sinks,
  statusFacts,
  statusLines,
  traces,
} from "./data";

// Every value here is invented. The wording of the status lines is the module's
// own fixed wording (`rheo_finance.reports.ops.status`).
const LINES = statusLines({
  lines: [
    { line_no: 1, text: "as of 2026-01-05 09:30 UTC, database current" },
    { line_no: 2, text: "last import: sample-bank 2026-01-04; tick 7 min ago" },
    { line_no: 3, text: "documents: 2 unparsed, 1 rejected, 1 needs manual entry, 3 bill gaps" },
    { line_no: 4, text: "missing account-months: 4 across 2 accounts" },
    {
      line_no: 5,
      text:
        "confirm last derive snapshot 2026-01-05: pairing 1, held notice 0, card debit 2," +
        " payment 0, presumed paid 3, reattach 0, category 4; untied card payments 2," +
        " credits not applied 1, orphaned decisions 0",
    },
    {
      line_no: 6,
      text: "reminders: 0 queued retries, pending total unavailable, last sync 2026-01-05 09:15 UTC",
    },
    { line_no: 7, text: "sinks off: telegram" },
  ],
});

describe("statusFacts", () => {
  it("reads the tick, the sync and the confirm snapshot from the fixed wording", () => {
    const facts = statusFacts(LINES);
    expect(facts.asOf).toBe("as of 2026-01-05 09:30 UTC");
    expect(facts.databaseCurrent).toBe(true);
    expect(facts.tickMinutes).toBe(7);
    expect(facts.noTickYet).toBe(false);
    expect(facts.lastSync).toBe("2026-01-05 09:15 UTC");
    expect(facts.syncFailedSince).toBeNull();
    expect(facts.queuedRetries).toBe(0);
    expect(facts.confirmAsOf).toBe("2026-01-05");
    expect(facts.confirm).toEqual([
      { kind: "pairing", count: 1 },
      { kind: "held notice", count: 0 },
      { kind: "card debit", count: 2 },
      { kind: "payment", count: 0 },
      { kind: "presumed paid", count: 3 },
      { kind: "reattach", count: 0 },
      { kind: "category", count: 4 },
    ]);
    expect(facts.backlog).toEqual([
      { label: "untied card payments", count: 2 },
      { label: "credits not applied", count: 1 },
      { label: "orphaned decisions", count: 0 },
    ]);
  });

  it("reads a failed sync and a missing tick", () => {
    const facts = statusFacts(
      statusLines({
        lines: [
          { line_no: 2, text: "last import: none; no tick yet" },
          {
            line_no: 6,
            text: "reminders not synced since 2026-01-04 18:00 UTC; 2 queued retries, pending total unavailable",
          },
          { line_no: 9, text: "tick stale: last ran 3 hours ago" },
        ],
      }),
    );
    expect(facts.noTickYet).toBe(true);
    expect(facts.tickMinutes).toBeNull();
    expect(facts.tickStale).toBe(true);
    expect(facts.syncFailedSince).toBe("2026-01-04 18:00 UTC");
    expect(facts.queuedRetries).toBe(2);
    expect(facts.lastSync).toBeNull();
  });

  it("leaves everything unknown when the wording does not match", () => {
    const facts = statusFacts(statusLines({ lines: [{ line_no: 5, text: "confirm last derive snapshot unavailable; untied card payments 1" }] }));
    expect(facts.confirm).toEqual([]);
    expect(facts.confirmAsOf).toBeNull();
    expect(facts.tickMinutes).toBeNull();
  });
});

describe("parsers", () => {
  it("tolerate missing and mistyped fields", () => {
    expect(statusLines(null)).toEqual([]);
    expect(sinks({ sinks: [{ sink: 3 }] })).toEqual([
      { sink: "", settingEnabled: false, approval: "none", approvedAt: null, enabled: false },
    ]);
    const o = owed({ rows: [{}], credits: "x" });
    expect(o.rows).toHaveLength(1);
    expect(o.credits.count).toBe(0);
    expect(coverage({ months: [{ state: "odd" }] }).months[0]?.state).toBe("missing");
  });

  it("never carry an amount, even when one is sent", () => {
    const o = obligation({ obligation_ref: "finance.obligation:1", amount_due: "12.34", remaining: "5.00" });
    expect(JSON.stringify(o)).not.toContain("12.34");
    expect(JSON.stringify(o)).not.toContain("5.00");
  });

  it("key traces by obligation ref", () => {
    const map = traces({
      obligations: [
        {
          row: { obligation_ref: "finance.obligation:1" },
          presumed_basis_kind: "later_statement",
          applications: [
            { paid_on: "2026-01-02", source: "bank_line", state: "applied", auto: true, evidence: [] },
          ],
        },
      ],
    });
    expect(map.get("finance.obligation:1")?.payments[0]?.paidOn).toBe("2026-01-02");
  });
});

describe("bucket", () => {
  const base = obligation({ status: "open", days_until_due: 3 });
  it("sorts rows by days to the due date and the window", () => {
    expect(bucket({ ...base, daysUntilDue: -1 }, 14)).toBe("overdue");
    expect(bucket({ ...base, daysUntilDue: 0 }, 14)).toBe("soon");
    expect(bucket({ ...base, daysUntilDue: 14 }, 14)).toBe("soon");
    expect(bucket({ ...base, daysUntilDue: 15 }, 14)).toBe("later");
    expect(bucket({ ...base, daysUntilDue: null }, 14)).toBe("undated");
  });
  it("keeps presumed-paid and disputed rows apart, however late", () => {
    expect(bucket({ ...base, status: "presumed_paid", daysUntilDue: -30 }, 14)).toBe("parked");
    expect(bucket({ ...base, status: "disputed", daysUntilDue: 2 }, 14)).toBe("parked");
    expect(bucket({ ...base, status: "partial", daysUntilDue: -2 }, 14)).toBe("overdue");
  });
});

describe("dates", () => {
  it("formats calendar dates without a time zone", () => {
    expect(shortDate("2026-11-24", 2026)).toBe("Nov 24");
    expect(shortDate("2025-01-03", 2026)).toBe("Jan 3, 2025");
    expect(shortDate(null)).toBe("");
  });
  it("says how far away a due date is", () => {
    expect(relativeDays(0)).toBe("due today");
    expect(relativeDays(1)).toBe("due tomorrow");
    expect(relativeDays(10)).toBe("in 10 days");
    expect(relativeDays(-1)).toBe("1 day overdue");
    expect(relativeDays(-4)).toBe("4 days overdue");
    expect(relativeDays(null)).toBe("no due date");
  });
});
