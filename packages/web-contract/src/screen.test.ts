import { describe, expect, it } from "vitest";

import { preferenceCookie } from "./screen";

describe("preferenceCookie", () => {
  it("names a module's own preference cookie", () => {
    expect(preferenceCookie("leads", "queue")).toBe("rheo-pref-leads-queue");
    expect(preferenceCookie("current", "desk-2")).toBe("rheo-pref-current-desk-2");
  });

  it("refuses a name that could reach any other cookie", () => {
    for (const name of ["", "Queue", "a;b", "a=b", "../x", "x".repeat(41), "rheo session"]) {
      expect(preferenceCookie("leads", name)).toBeNull();
    }
    expect(preferenceCookie("", "queue")).toBeNull();
    expect(preferenceCookie("Leads", "queue")).toBeNull();
  });
});
