import { describe, expect, it } from "vitest";

import { canonicalTimeZone, readTimeZoneCookie, TIME_ZONE_COOKIE, timeZoneCookieValue, timeZoneNames } from "./time-zone";

function jar(value?: string) {
  return { get: (name: string) => (name === TIME_ZONE_COOKIE && value !== undefined ? { value } : undefined) };
}

describe("the time zone cookie (#465)", () => {
  it("round-trips a detected and a chosen zone", () => {
    for (const zone of [
      { zone: "America/Vancouver", chosen: false },
      { zone: "Europe/London", chosen: true },
    ]) {
      expect(readTimeZoneCookie(jar(timeZoneCookieValue(zone)))).toEqual(zone);
    }
  });

  it("ignores a missing, malformed or unknown zone", () => {
    for (const value of [undefined, "", "ab12", "auto:", "auto:Mars/Olympus", "now:UTC"]) {
      expect(readTimeZoneCookie(jar(value))).toBeNull();
    }
  });

  it("accepts only zones the runtime can format in, under one name", () => {
    expect(canonicalTimeZone("America/Vancouver")).toBe("America/Vancouver");
    for (const alias of ["UTC", "Etc/UTC", "utc"]) expect(canonicalTimeZone(alias)).toBe("UTC");
    expect(canonicalTimeZone("Not/AZone")).toBeNull();
    expect(canonicalTimeZone(42)).toBeNull();
    expect(readTimeZoneCookie(jar("auto:Etc/UTC"))).toEqual({ zone: "UTC", chosen: false });
  });

  it("offers UTC and the viewer's zone", () => {
    const names = timeZoneNames();
    expect(names).toContain("UTC");
    expect(names).toContain("America/Vancouver");
  });
});
