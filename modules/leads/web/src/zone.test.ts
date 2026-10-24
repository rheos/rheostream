import { describe, expect, it } from "vitest";
import { date, dayIn, moment, zoneOf } from "./data";
import { followupFor } from "./queue";
import { clock } from "./scoring";

// 03:30Z on Oct 24 is 8:30 pm on Oct 23 in Vancouver (PDT, UTC-7).
const EVENING = "2026-10-24T03:30:00Z";
const VAN = "America/Vancouver";

describe("times in the viewer's zone (#465)", () => {
  it("formats moments in the zone without a label, and labels UTC", () => {
    expect(moment(EVENING, "", VAN)).toBe("Oct 23, 2026, 8:30 PM");
    expect(moment(EVENING, "")).toBe("Oct 24, 2026, 3:30 AM UTC");
    expect(date(EVENING, VAN)).toBe("Oct 23, 2026");
  });

  it("never shifts a calendar date", () => {
    expect(date("2026-10-25", VAN)).toBe("Oct 25, 2026");
  });

  it("drops the weekday only when the moment is today in the zone", () => {
    const now = Date.parse("2026-10-24T05:00:00Z"); // 10 pm Oct 23 in Vancouver
    expect(clock(EVENING, now, VAN)).toBe("8:30 PM");
    expect(clock(EVENING, Date.parse("2026-10-24T08:00:00Z"), VAN)).toBe("Fri 8:30 PM");
  });

  it("switches to PST with the clocks in November", () => {
    expect(moment("2026-11-02T04:30:00Z", "", VAN)).toBe("Nov 1, 2026, 8:30 PM");
  });

  it("counts a follow-up overdue by the viewer's today", () => {
    const now = Date.parse("2026-10-24T05:00:00Z");
    const item = { followup: { due_on: "2026-10-23", action: "Call" } };
    expect(followupFor(item, now, VAN)?.overdue).toBe(false);
    expect(followupFor(item, now)?.overdue).toBe(true);
    expect(dayIn(now, VAN)).toBe("2026-10-23");
  });

  it("falls back to UTC when the shell has no zone", () => {
    expect(zoneOf({})).toBe("UTC");
    expect(zoneOf({ timeZone: VAN })).toBe(VAN);
  });
});
