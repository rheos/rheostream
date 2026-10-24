import { describe, expect, it } from "vitest";
import {
  allowedTargets,
  areaKey,
  customSnooze,
  day,
  itemDetail,
  refusalText,
  snoozeAt,
  TRANSITIONS,
} from "./data";
import { formInput, type Field } from "./forms";
import { queueFilters } from "./screens";

// Local wall-clock dates, so every case reads the same in any time zone.
const friday3pm = new Date(2026, 9, 9, 15, 0); // Friday 9 October 2026
const friday8pm = new Date(2026, 9, 9, 20, 0);
const saturday8am = new Date(2026, 9, 10, 8, 0);
const saturday10am = new Date(2026, 9, 10, 10, 0);
const local = (d: Date | null) =>
  d
    ? [d.getFullYear(), d.getMonth() + 1, d.getDate(), d.getHours(), d.getMinutes()]
    : null;

describe("snooze quick picks", () => {
  it("computes each pick in the viewer's local time", () => {
    expect(local(snoozeAt("hour", friday3pm))).toEqual([2026, 10, 9, 16, 0]);
    expect(local(snoozeAt("evening", friday3pm))).toEqual([2026, 10, 9, 19, 0]);
    expect(local(snoozeAt("tomorrow", friday3pm))).toEqual([2026, 10, 10, 9, 0]);
    expect(local(snoozeAt("saturday", friday3pm))).toEqual([2026, 10, 10, 9, 0]);
    expect(local(snoozeAt("monday", friday3pm))).toEqual([2026, 10, 12, 9, 0]);
  });
  it("has no evening once 7pm has passed", () => {
    expect(snoozeAt("evening", friday8pm)).toBeNull();
  });
  it("means today on a Saturday morning and next week after 9am", () => {
    expect(local(snoozeAt("saturday", saturday8am))).toEqual([2026, 10, 10, 9, 0]);
    expect(local(snoozeAt("saturday", saturday10am))).toEqual([2026, 10, 17, 9, 0]);
  });
  it("never returns a time in the past", () => {
    for (const now of [friday3pm, friday8pm, saturday8am, saturday10am])
      for (const pick of ["hour", "evening", "tomorrow", "saturday", "monday"] as const) {
        const at = snoozeAt(pick, now);
        if (at) expect(at.getTime()).toBeGreaterThan(now.getTime());
      }
  });
  it("accepts a custom time only when it is real and later", () => {
    expect(local(customSnooze("2026-10-12T08:30", friday3pm))).toEqual([
      2026, 10, 12, 8, 30,
    ]);
    expect(customSnooze("2026-10-09T14:00", friday3pm)).toBeNull();
    expect(customSnooze("", friday3pm)).toBeNull();
    expect(customSnooze("tomorrow", friday3pm)).toBeNull();
  });
});

describe("status transitions", () => {
  it("offers only the service's own moves, plus a re-snooze", () => {
    expect(allowedTargets("done")).toEqual(["open"]);
    expect(allowedTargets("open")).toEqual(["waiting", "snoozed", "done"]);
    expect(allowedTargets("snoozed")).toContain("snoozed");
    for (const [from, targets] of Object.entries(TRANSITIONS))
      expect(targets).not.toContain(from);
  });
});

describe("update forms", () => {
  const fields: Field[] = [
    { name: "title", label: "Title", value: "Renew the domain", required: true },
    { name: "body", label: "Notes", value: "Before Friday", type: "textarea" },
    { name: "area", label: "Area", value: "bills", type: "select", clearable: true },
    { name: "due_on", label: "Due", value: "", type: "date", clearable: true },
  ];
  const base = { ref: "current.item:sample", revision: 4 };
  const same = { title: "Renew the domain", body: "Before Friday", area: "bills", due_on: "" };

  it("sends nothing when nothing changed", () => {
    expect(formInput("current.item.update", base, fields, same)).toBeNull();
  });
  it("sends only what changed, with the revision it read", () => {
    expect(
      formInput("current.item.update", base, fields, { ...same, due_on: "2026-11-02" }),
    ).toEqual({ ...base, due_on: "2026-11-02" });
  });
  it("names a blanked optional field in clear instead of sending null", () => {
    expect(
      formInput("current.item.update", base, fields, { ...same, area: "" }),
    ).toEqual({ ...base, clear: ["area"] });
  });
  it("empties a plain text field by sending it empty", () => {
    expect(
      formInput("current.item.update", base, fields, { ...same, body: "" }),
    ).toEqual({ ...base, body: "" });
  });
  it("leaves blank fields to the defaults on a create", () => {
    expect(
      formInput(
        "current.item.create",
        {},
        [
          { name: "title", label: "Title", required: true },
          { name: "area", label: "Area", type: "select" },
          { name: "due_on", label: "Due", type: "date" },
        ],
        { title: "Call the accountant", area: "", due_on: "" },
      ),
    ).toEqual({ title: "Call the accountant" });
  });
  it("derives a new area's key from its name when none is given", () => {
    expect(
      formInput(
        "current.area.create",
        {},
        [
          { name: "label", label: "Name", required: true },
          { name: "key", label: "Key" },
        ],
        { label: "Garden & Yard", key: "" },
      ),
    ).toEqual({ label: "Garden & Yard", key: "garden_yard" });
  });
});

describe("helpers", () => {
  it("makes area keys in the service's grammar", () => {
    expect(areaKey("Foreign affairs")).toBe("foreign_affairs");
    expect(areaKey("  2026 Taxes ")).toBe("taxes");
    expect(areaKey("Café")).toBe("cafe");
    expect(areaKey("x".repeat(60))).toHaveLength(40);
  });
  it("explains a stale write and a dependency loop", () => {
    expect(refusalText("record_stale", "item changed")).toMatch(/still here/);
    expect(refusalText("dependency_cycle", "")).toMatch(/loop/);
    expect(refusalText("something_else", "Plain text")).toBe("Plain text");
  });
  it("formats calendar dates without shifting them a day", () => {
    expect(day("2026-01-01")).toBe("Jan 1, 2026");
    expect(day("not a date")).toBe("");
  });
  it("reads a detail record and tolerates missing parts", () => {
    const detail = itemDetail({ ref: "current.item:x", title: "Sample", status: "bogus" });
    expect(detail.status).toBe("open");
    expect(detail.comments).toEqual([]);
    expect(detail.project).toBeNull();
  });
  it("bounds queue filters to what the operation accepts", () => {
    expect(
      queueFilters({ status: "nonsense", area: "Bad Key", priority: "urgent", q: "  " }),
    ).toMatchObject({ status: "active", area: null, priority: null, query: null });
    expect(
      queueFilters({ status: "done", area: "bills", overdue: "1", blocked: "0" }),
    ).toMatchObject({ status: "done", area: "bills", overdue: true, blocked: null });
  });
});

describe("snooze picks in the shell's zone (#465)", () => {
  // 3 pm Friday 9 October 2026 in Vancouver (PDT) is 22:00Z.
  const vanFriday3pm = new Date("2026-10-09T22:00:00Z");
  it("computes each pick on the shown zone's clock, whatever the browser's", () => {
    expect(snoozeAt("tomorrow", vanFriday3pm, "America/Vancouver")?.toISOString()).toBe(
      "2026-10-10T16:00:00.000Z",
    );
    expect(snoozeAt("evening", vanFriday3pm, "America/Vancouver")?.toISOString()).toBe(
      "2026-10-10T02:00:00.000Z",
    );
    // Already 11 pm in London, so no evening, and tomorrow is 9 am BST.
    expect(snoozeAt("evening", vanFriday3pm, "Europe/London")).toBeNull();
    expect(snoozeAt("tomorrow", vanFriday3pm, "Europe/London")?.toISOString()).toBe(
      "2026-10-10T08:00:00.000Z",
    );
    expect(snoozeAt("monday", vanFriday3pm, "America/Vancouver")?.toISOString()).toBe(
      "2026-10-12T16:00:00.000Z",
    );
  });
  it("crosses a clock change on the zone's clock", () => {
    // Los Angeles: BC no longer falls back from Nov 2026 (tzdata 2026b).
    const saturday = new Date("2026-10-31T22:00:00Z"); // 3 pm PDT
    expect(snoozeAt("monday", saturday, "America/Los_Angeles")?.toISOString()).toBe(
      "2026-11-02T17:00:00.000Z", // 9 am PST
    );
  });
  it("reads a custom time on the zone's clock", () => {
    expect(customSnooze("2026-10-10T09:00", vanFriday3pm, "America/Vancouver")?.toISOString()).toBe(
      "2026-10-10T16:00:00.000Z",
    );
    expect(customSnooze("2026-10-09T14:00", vanFriday3pm, "America/Vancouver")).toBeNull();
    expect(customSnooze("2026-13-01T09:00", vanFriday3pm, "America/Vancouver")).toBeNull();
  });
});
