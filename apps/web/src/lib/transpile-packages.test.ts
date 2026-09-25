import { describe, expect, it } from "vitest";

import nextConfig from "../../next.config";
import { workspaceTranspilePackages } from "./transpile-packages";

/**
 * The derivation itself, and the real config it feeds: `next.config.ts` is
 * imported, not re-implemented, so a hand-kept list creeping back in would have to
 * agree with `apps/web/package.json` to pass.
 */

describe("transpilePackages", () => {
  it("carries every @rheo-stream/* package apps/web depends on", () => {
    const derived = nextConfig.transpilePackages ?? [];
    expect(derived).toEqual(
      expect.arrayContaining(["@rheo-stream/recallatron-web", "@rheo-stream/web-contract"]),
    );
    expect(derived.every((name) => name.startsWith("@rheo-stream/"))).toBe(true);
  });

  it("reads both dependency blocks and nothing outside the scope", () => {
    expect(
      workspaceTranspilePackages({
        dependencies: { "@rheo-stream/one": "workspace:*", next: "^16" },
        devDependencies: { "@rheo-stream/two": "workspace:*", "@rheo-streamer/x": "1" },
      }),
    ).toEqual(["@rheo-stream/one", "@rheo-stream/two"]);
  });

  it("is empty for a manifest with no dependency blocks", () => {
    expect(workspaceTranspilePackages({})).toEqual([]);
  });
});
