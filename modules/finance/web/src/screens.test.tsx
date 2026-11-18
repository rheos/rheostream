import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import type { OperationOutcome, ShellApi } from "@rheo-stream/web-contract/screen";

import { screens } from "./index";

// Every value here is invented: payees, accounts, paths and amounts are made up.
// `zz-issuer` stands for a payee the owner never labelled, whose label is its own
// key: it, and every path, must never render.
const ok = (result: unknown): OperationOutcome => ({ state: "ok", result });

const STATUS = ok({
  lines: [
    { line_no: 1, text: "as of 2026-01-05 09:30 UTC, database current" },
    { line_no: 2, text: "last import: zz-bank 2026-01-04, zz-card 2026-01-02; tick 4 min ago" },
    {
      line_no: 5,
      text:
        "confirm last derive snapshot 2026-01-05: pairing 0, held notice 0, card debit 0," +
        " payment 0, presumed paid 2, reattach 0, category 0; untied card payments 0," +
        " credits not applied 1, orphaned decisions 0",
    },
    { line_no: 6, text: "reminders: 0 queued retries, pending total unavailable, last sync 2026-01-05 09:15 UTC" },
    { line_no: 9, text: "1 source discrepancy; 1 duplicate bill month (zz-issuer 1); 1 account with no data: zz-savings" },
  ],
});
const SINKS = ok({
  sinks: [
    { sink: "current", setting_enabled: true, approval: "current", approved_at: "2026-01-01T00:00:00Z", enabled: true },
    { sink: "telegram", setting_enabled: false, approval: "none", approved_at: null, enabled: false },
  ],
});

function row(overrides: Record<string, unknown>) {
  return {
    obligation_ref: "finance.obligation:00000000-0000-4000-8000-000000000001",
    payee_slug: "example-power",
    payee_label: "Example Power",
    kind: "bill",
    period_label: "2025-12",
    statement_date: "2025-12-20",
    issued_on: null,
    due_date: "2026-01-10",
    days_until_due: 5,
    status: "open",
    primary_source: "bills/example-power/2025-12.pdf",
    manual_source: null,
    before_floor: false,
    reminder_eligible: true,
    payments_unmatchable: false,
    statement_conflict: false,
    presumed_paid_basis: null,
    amount_due: "120.00",
    minimum_due: null,
    threshold_due: "120.00",
    paid: "0.00",
    remaining: "120.00",
    threshold_remaining: "120.00",
    overdue: "0.00",
    carried: "0.00",
    due_in_window: "120.00",
    ...overrides,
  };
}

const OWED = ok({
  as_of: "as of 2026-01-05 09:30 UTC",
  today: "2026-01-05",
  window_days: 14,
  currency: "USD",
  rows: [
    row({
      obligation_ref: "finance.obligation:00000000-0000-4000-8000-000000000002",
      payee_label: "Sample Water",
      period_label: "2025-11",
      due_date: "2026-01-01",
      days_until_due: -4,
      before_floor: true,
      reminder_eligible: false,
      amount_due: "45.10",
      remaining: "45.10",
      threshold_remaining: "45.10",
      overdue: "45.10",
      due_in_window: "0.00",
    }),
    row({}),
    row({
      obligation_ref: "finance.obligation:00000000-0000-4000-8000-000000000003",
      payee_label: "Demo Card",
      kind: "card_statement",
      status: "presumed_paid",
      days_until_due: -2,
      due_date: "2026-01-03",
      reminder_eligible: false,
    }),
    row({
      obligation_ref: "finance.obligation:00000000-0000-4000-8000-000000000004",
      payee_slug: "zz-issuer",
      payee_label: "zz-issuer",
      period_label: "2025-12",
      due_date: "2026-02-04",
      days_until_due: 30,
      primary_source: "bills/zz-issuer/2025-12.pdf",
    }),
  ],
  more: 2,
  due_in_window: "120.00",
  overdue: "45.10",
  carried: "0.00",
  headline: null,
  credits: {
    count: 1,
    sums: [{ currency: "USD", amount: "15.00", cad_amount: null }],
    items: [
      {
        line_ref: "finance.txn:1",
        tax_year: 2025,
        kind: "unapplied_credit",
        occurred_on: "2025-12-02",
        account_label: "Test Chequing",
        direction: "in",
        currency: "USD",
        amount: "-15.00",
        state: "open",
        reason: "no_bill_to_apply",
        payee_label: "Example Power",
        payee_slug: "example-power",
        candidate_count: 0,
        candidate_refs: [],
        line_ref_extra: null,
      },
    ],
  },
  gaps: [
    { path: "bills/zz-issuer/2025-10.pdf", issuer_label: "zz-issuer", kind: "bill", status: "unparsed", period_start: "2025-10-01", period_end: "2025-10-31" },
  ],
  gap_count: 1,
});
const TRACE = ok({
  obligations: [
    {
      row: { obligation_ref: "finance.obligation:00000000-0000-4000-8000-000000000001" },
      basis_class: null,
      presumed_basis_kind: null,
      presumed_at: null,
      applications: [
        { application_ref: "a", payment_ref: "p", currency: "USD", paid_on: "2025-12-28", source: "bank_line", state: "applied", basis_ref: null, auto: true, applied_amount: "60.00", payment_amount: "60.00", evidence: [{ evidence_ref: "e", role: "statement", txn_ref: null, document_path: "statements/zz-bank/2025-12.pdf", line_no: 4, drop_ref: null }] },
      ],
    },
  ],
  credits: { count: 0, sums: [], items: [] },
});
const DOCUMENTS = ok({
  documents: [
    { path: "statements/test-card/2025-11.pdf", kind: "statement", status: "rejected", period_start: "2025-11-01", period_end: "2025-11-30", official: true, extraction: "text" },
    { path: "bills/example-power/2025-10.pdf", kind: "bill", status: "parsed", period_start: null, period_end: null, official: true, extraction: "text" },
    { path: "bills/zz-issuer/2025-10.pdf", kind: "bill", status: "unparsed", period_start: "2025-10-01", period_end: "2025-10-31", official: true, extraction: "none" },
  ],
});
const COVERAGE = ok({
  months: [
    { account: "test-chequing", account_label: "Test Chequing", month: "2025-11-01", state: "have", reason: null },
    { account: "test-chequing", account_label: "Test Chequing", month: "2025-12-01", state: "missing", reason: "running_balance_break" },
    { account: "test-card", account_label: "Test Card", month: "2025-12-01", state: "rejected", reason: null },
  ],
  no_data_accounts: 1,
});
const OPEN_ITEMS = ok({
  items: [
    { line_ref: "finance.txn:9", tax_year: 2025, kind: "needs_source", occurred_on: "2025-12-12", account_label: "Test Card", direction: "out", currency: "USD", amount: "-9.99", state: "open", reason: "no_receipt", payee_label: null, payee_slug: null, candidate_count: 0, candidate_refs: [] },
  ],
  summary: [
    { kind: "needs_source", count: 1, sums: [] },
    { kind: "unapplied_credit", count: 1, sums: [] },
  ],
});

const RESULTS: Record<string, OperationOutcome> = {
  "finance.status.get": STATUS,
  "finance.sink.state": SINKS,
  "finance.owed.list": OWED,
  "finance.owed.trace": TRACE,
  "finance.document.list": DOCUMENTS,
  "finance.coverage.get": COVERAGE,
  "finance.openitem.list": OPEN_ITEMS,
};

function fakeShell(
  role = "owner",
  results: Record<string, OperationOutcome> = RESULTS,
): ShellApi & { call: ReturnType<typeof vi.fn> } {
  return {
    role,
    timeZone: "UTC",
    call: vi.fn(
      async (operation: string): Promise<OperationOutcome> =>
        results[operation] ?? { state: "unavailable" },
    ),
    href: (routeId: string, query?: Record<string, string | undefined>) =>
      `/${routeId}${query?.year ? `?year=${query.year}` : ""}`,
  };
}

async function render(name: keyof typeof screens, shell: ShellApi, query = {}) {
  return renderToStaticMarkup(await screens[name]({ shell, query }));
}

// The module's READ operations, which `rheo web compose` writes as its allowlist.
const ALLOWED = new Set([
  "finance.coverage.get",
  "finance.document.list",
  "finance.openitem.list",
  "finance.owed.list",
  "finance.owed.trace",
  "finance.sink.state",
  "finance.status.get",
  "finance.txn.list",
]);

describe.each(["owed", "attention", "coverage"] as const)("%s", (name) => {
  it("renders the owner-only notice for a member and reads nothing", async () => {
    const shell = fakeShell("member");
    const html = await render(name, shell);
    expect(html).toContain("Finance is for the workspace owner");
    expect(shell.call).not.toHaveBeenCalled();
  });

  it("calls only the module's allowlisted reads", async () => {
    const shell = fakeShell();
    await render(name, shell);
    for (const [operation] of shell.call.mock.calls) expect(ALLOWED.has(operation)).toBe(true);
  });

  it("shows the status strip", async () => {
    const html = await render(name, fakeShell());
    expect(html).toContain("Ticked 4 min ago");
    expect(html).toContain("Synced to Current at 09:15 UTC");
    expect(html).toContain("Sending to Current");
  });

  it("never shows a key, a path or a source ref", async () => {
    const html = await render(name, fakeShell(), { year: "2025" });
    expect(html).not.toMatch(/zz-/);
    expect(html).not.toMatch(/bills\/|statements\/|finance\.obligation:|finance\.txn:/);
  });

  it("says when the service does not answer", async () => {
    const html = await render(name, fakeShell("owner", {}));
    expect(html).toContain("Status unavailable");
    expect(html).toContain("Could not load");
  });
});

describe("owed", () => {
  it("leads with what is due and groups the rows", async () => {
    const html = await render("owed", fakeShell());
    expect(html).toContain("$45.10 overdue, $120.00 due in the next 14 days.");
    expect(html).toContain("Overdue");
    expect(html).toContain("Due in the next 14 days");
    expect(html).toContain("Presumed paid or disputed");
    expect(html).toContain("before floor");
    expect(html).toContain("4 days overdue");
    expect(html).toContain("in 5 days");
    expect(html).toContain("Gets a reminder in Current");
    expect(html).toContain("$120.00");
    expect(html).toContain("2 more bills past the list");
  });

  it("puts the figures and payments behind the row's info button", async () => {
    const html = await render("owed", fakeShell());
    expect(html).toContain('popover="auto"');
    expect(html).toContain('role="dialog"');
    expect(html).toContain("Left to pay");
    expect(html).toContain("A parsed document");
    expect(html).toContain("Dec 28, 2025, $60.00, applied, matched automatically");
  });

  it("names an unlabelled payee as unnamed, never by its key", async () => {
    const html = await render("owed", fakeShell());
    expect(html).toContain("Unnamed payee");
  });

  it("keeps a presumed-paid row out of the overdue wording", async () => {
    const html = await render("owed", fakeShell());
    expect(html).not.toContain("2 days overdue");
  });

  it("calls a past-due bill with its threshold paid past due, not overdue", async () => {
    const shell = fakeShell("owner", {
      ...RESULTS,
      "finance.owed.list": ok({
        ...(OWED as { result: Record<string, unknown> }).result,
        rows: [row({ days_until_due: -3, due_date: "2026-01-02", kind: "card_statement", threshold_remaining: "0.00", overdue: "0.00", remaining: "300.00" })],
        overdue: "0.00",
        due_in_window: "0.00",
      }),
    });
    const html = await render("owed", shell);
    expect(html).toContain("Past due, minimum paid");
    expect(html).not.toContain("Overdue");
    expect(html).toContain("Nothing due in the next 14 days.");
  });
});

describe("attention", () => {
  it("lists only the groups with something in them, and names the clear ones", async () => {
    const html = await render("attention", fakeShell());
    expect(html).toContain("Waiting for you to confirm");
    expect(html).toContain("presumed paid");
    expect(html).toContain("Rejected documents");
    expect(html).toContain("Unparsed documents");
    expect(html).toContain("1 is a bill not on the owed list");
    expect(html).toContain("Credits not applied");
    expect(html).toContain("$15.00");
    expect(html).toContain("$9.99");
    expect(html).toContain("Payees and accounts without a name");
    expect(html).toContain("Missing account-months");
    expect(html).toContain("Dec 2025");
    expect(html).toContain("Needs source");
    expect(html).toContain("All clear: documents needing manual entry.");
    expect(html).toContain("1 source discrepancy");
    expect(html).toContain("1 duplicate bill month");
    expect(html).toContain("1 account with no data");
    // Unapplied credits come from the owed read, not twice.
    expect(html.match(/Credits not applied/g)).toHaveLength(1);
  });
});

describe("coverage", () => {
  it("draws the year asked for, one row per account", async () => {
    const shell = fakeShell();
    const html = await render("coverage", shell, { year: "2025" });
    expect(shell.call).toHaveBeenCalledWith("finance.coverage.get", { year: 2025 });
    expect(html).toContain("Test Chequing");
    expect(html).toContain("Test Card");
    expect(html).toContain("Dec: Missing: running balance break");
    expect(html).toContain("1 account has no data at all");
    expect(html).toContain('href="/coverage?year=2024"');
  });

  it("falls back to this year for a year it cannot read", async () => {
    const shell = fakeShell();
    await render("coverage", shell, { year: "nonsense" });
    const year = new Date().getUTCFullYear();
    expect(shell.call).toHaveBeenCalledWith("finance.coverage.get", { year });
  });
});
