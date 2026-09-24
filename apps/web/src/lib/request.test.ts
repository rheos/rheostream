import { describe, expect, it } from "vitest";

import { requestHost } from "@/lib/request";

describe("requestHost", () => {
  it("prefers x-forwarded-host over host", () => {
    const headers = new Headers({ host: "upstream:3000", "x-forwarded-host": "circuit.example.test" });
    expect(requestHost(headers)).toBe("circuit.example.test");
  });

  it("falls back to host, then to undefined", () => {
    expect(requestHost(new Headers({ host: "example.test:3000" }))).toBe("example.test:3000");
    expect(requestHost(new Headers())).toBeUndefined();
  });
});
