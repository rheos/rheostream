// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ShellApi } from "@rheo-stream/web-contract/screen";
import type { Row } from "./data";
import {
  batchSource,
  busy,
  outcomeLabel,
  span,
  stageChip,
  statusLine,
} from "./scoring";
import { ScoringView, type ScoringTab } from "./scoring-view";

(
  globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }
).IS_REACT_ACT_ENVIRONMENT = true;

const NOW = Date.parse("2026-10-18T12:00:00Z");
const GOOD = "leads.opportunity:01a133b6-b93c-73c0-975f-cf6437992a85";
const HELD = "leads.opportunity:01a133b6-b93c-73c0-975f-cf6437992a86";
const NEXT = "leads.opportunity:01a133b6-b93c-73c0-975f-cf6437992a87";

function job(ref: string, title: string, extra: Row): Row {
  return {
    id: ref.split(":")[1],
    ref,
    title,
    created_at: "2026-10-18T10:00:00Z",
    source: "poll",
    position: null,
    score: null,
    assessed_at: null,
    retry_at: null,
    attempts: 0,
    failure_kind: null,
    ...extra,
  };
}

const VIEW: Row = {
  window_hours: 48,
  summary_days: 7,
  status: {
    state: "running",
    purpose: "scoring",
    started_at: "2026-10-18T11:59:18Z",
    ref: NEXT,
    title: "Example data pipeline",
    last_call_at: "2026-10-18T11:58:00Z",
  },
  queued_total: 1,
  waiting_total: 0,
  failed_total: 2,
  median_call_seconds: 40,
  eta_seconds: 40,
  batches: [
    {
      started_at: "2026-10-18T10:00:00Z",
      sources: ["poll"],
      counts: { scored: 1, held: 1, scoring: 1 },
      jobs: [
        job(NEXT, "Example data pipeline", { stage: "scoring" }),
        job(GOOD, "Example storefront", {
          stage: "scored",
          score: 82,
          assessed_at: "2026-10-18T11:00:00Z",
        }),
        job(HELD, "Example crowded app", { stage: "held" }),
      ],
    },
  ],
  calls: [
    {
      id: "c1",
      purpose: "scoring",
      model_id: "claude-sonnet-4-6",
      started_at: "2026-10-18T11:00:00Z",
      seconds: 38.2,
      input_tokens: 12034,
      output_tokens: 410,
      outcome: "answered",
      failure_kind: null,
      ref: GOOD,
      title: "Example storefront",
    },
    {
      id: "c2",
      purpose: "draft",
      model_id: "default",
      started_at: "2026-10-18T10:30:00Z",
      seconds: 2.1,
      input_tokens: null,
      output_tokens: null,
      outcome: "deferred",
      failure_kind: "usage_limited",
      ref: null,
      title: null,
    },
  ],
  summary: [
    {
      purpose: "scoring",
      model_id: "claude-sonnet-4-6",
      calls: 3,
      input_tokens: 36000,
      output_tokens: 1200,
      median_seconds: 41,
      failed: 1,
      deferred: 0,
    },
  ],
};

function markup(tab: ScoringTab, view = VIEW, canRetry = true): string {
  return renderToStaticMarkup(
    <ScoringView
      view={view}
      now={NOW}
      tab={tab}
      hrefs={{
        [GOOD]: "/leads/detail?ref=good",
        [NEXT]: "/leads/detail?ref=next",
      }}
      tabHrefs={{ queue: "/leads/scoring", calls: "/leads/scoring?tab=calls" }}
      reviewHref="/leads/review"
      canRetry={canRetry}
      submit={vi.fn() as unknown as ShellApi["submit"]}
    />,
  );
}

describe("the Scoring view's words", () => {
  it("says what scoring is doing", () => {
    expect(statusLine(VIEW.status as Row, NOW).text).toBe(
      'Scoring "Example data pipeline" for 42s',
    );
    expect(
      statusLine(
        {
          state: "paused",
          failure_kind: "usage_limited",
          until: "2026-10-18T13:05:00Z",
        },
        NOW,
      ).text,
    ).toBe("Paused on the usage limit. Resumes around 1:05 PM UTC.");
    expect(statusLine({ state: "idle", last_call_at: null }, NOW).text).toBe(
      "Idle.",
    );
    expect(
      statusLine({ state: "idle", last_call_at: "2026-10-18T11:48:00Z" }, NOW)
        .text,
    ).toBe("Idle. Last model call 12 min ago.");
    expect(statusLine({ state: "off" }, NOW).tone).toBe("warn");
    expect(statusLine({ state: "no_profile" }, NOW).text).toContain("profile");
  });
  it("names stages, outcomes, spans and sources", () => {
    expect(stageChip({ stage: "queued", position: 3 }, NOW).mark).toBe("#3");
    expect(
      stageChip(
        { stage: "retrying", attempts: 2, failure_kind: "output_invalid" },
        NOW,
      ).title,
    ).toContain("unreadable answer");
    expect(stageChip({ stage: "untracked" }, NOW).mark).toBe("new");
    expect(outcomeLabel({ purpose: "scoring", outcome: "answered" })).toBe(
      "Scored",
    );
    expect(
      outcomeLabel({ outcome: "deferred", failure_kind: "usage_limited" }),
    ).toBe("Deferred: usage limit");
    expect(span(42)).toBe("42s");
    expect(span(600)).toBe("10 min");
    expect(span(5400)).toBe("1h 30m");
    expect(batchSource(["poll", "manual"])).toBe("Job feed + Pasted");
    expect(busy(VIEW)).toBe(true);
    expect(busy({ status: { state: "idle" }, queued_total: 0 })).toBe(false);
  });
});

describe("the Scoring view", () => {
  it("shows the queue by import, with held jobs folded away", () => {
    const html = markup("queue");
    expect(html).toContain("Scoring &quot;Example data pipeline&quot; for 42s");
    expect(html).toContain("about 40s to clear");
    expect(html).toContain("Job feed · 10:00 AM UTC");
    expect(html).toContain(">82<");
    expect(html).toContain('href="/leads/detail?ref=good"');
    expect(html).toMatch(/<details[^>]*><summary>1 held by the filters/);
    expect(html).toContain('href="/leads/review"');
    expect(html).toContain('aria-label="Put 2 failed jobs back in line"');
    expect(html).toContain('aria-current="page"');
    // No posting text and no money anywhere on the page.
    expect(html).not.toContain("$");
  });
  it("shows the call log with tokens and times, never cost", () => {
    const html = markup("calls");
    expect(html).toContain("Last 7 days");
    expect(html).toContain("12,034 / 410");
    expect(html).toContain("38s");
    expect(html).toContain("Scored");
    expect(html).toContain("Deferred: usage limit");
    expect(html).toContain("Proposal draft");
    expect(html).toContain("36,000");
    expect(html).not.toContain("$");
    expect(html).not.toContain("Cost");
  });
  it("hides the retry button from members", () => {
    expect(markup("queue", VIEW, false)).not.toContain('aria-label="Put 2');
  });
});

describe("retrying failed jobs", () => {
  let host: HTMLDivElement;
  let root: Root;
  beforeEach(() => {
    vi.useFakeTimers();
    host = window.document.createElement("div");
    window.document.body.appendChild(host);
    root = createRoot(host);
  });
  afterEach(() => {
    act(() => root.unmount());
    host.remove();
    vi.useRealTimers();
  });

  it("calls the operation and reports a failure in place", async () => {
    const submit = vi.fn().mockResolvedValue({ state: "unavailable" });
    act(() =>
      root.render(
        <ScoringView
          view={VIEW}
          now={NOW}
          tab="queue"
          hrefs={{}}
          tabHrefs={{ queue: "/q", calls: "/c" }}
          reviewHref="/r"
          canRetry
          submit={submit as unknown as ShellApi["submit"]}
        />,
      ),
    );
    const button = host.querySelector<HTMLButtonElement>(
      'button[aria-label="Put 2 failed jobs back in line"]',
    );
    expect(button).not.toBeNull();
    await act(async () => {
      button?.click();
    });
    expect(submit).toHaveBeenCalledWith("leads.jobscoring.retry_failed", {});
    expect(host.querySelector('[role="alert"]')?.textContent).toContain(
      "Could not put the failed jobs back in line",
    );
  });
});
