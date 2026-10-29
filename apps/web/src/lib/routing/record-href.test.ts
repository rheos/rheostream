import { readFileSync } from "node:fs";
import path from "node:path";

import type { ComposedModule } from "@rheo-stream/web-contract/screen";
import { describe, expect, it } from "vitest";

import type { RoutingConfig, SurfaceConfig } from "@/lib/routing/config";
import { recordHref } from "@/lib/routing/record-href";

const REPO = path.resolve(import.meta.dirname, "../../../../..");
const ITEM_REF = "records.item:12345678-1234-5678-9abc-1234567890ab";

function fixture(mode: "path" | "subdomain"): RoutingConfig {
  const base = JSON.parse(
    readFileSync(path.join(REPO, `tests/fixtures/routing/${mode}-mode.json`), "utf8"),
  ) as RoutingConfig;
  return withSurface(base);
}

function withSurface(
  config: RoutingConfig,
  surface: SurfaceConfig = { host: "archive", path: "/archive" },
): RoutingConfig {
  return {
    ...config,
    surfaces: {
      ...config.surfaces,
      modules: { ...config.surfaces.modules, records_ui: surface },
    },
  };
}

const screen = async () => null;
const PROVIDER: ComposedModule = {
  id: "records",
  surface: "records_ui",
  navigation: [],
  routes: [{ id: "item-detail", path: "/items/detail", screen }],
  recordRoutes: [{ recordType: "item", routeId: "item-detail", refQuery: "record_ref" }],
  recordViews: [],
  forms: [],
  searchProviders: [],
  readOperations: [],
};

describe("recordHref", () => {
  it.each([
    ["path", "https://example.test/archive/items/detail"],
    ["subdomain", "https://archive.example.test/items/detail"],
  ] as const)("uses provider metadata and %s routing", (mode, base) => {
    expect(recordHref(fixture(mode), [PROVIDER], [PROVIDER.id], ITEM_REF)).toBe(
      `${base}?record_ref=records.item%3A12345678-1234-5678-9abc-1234567890ab`,
    );
  });

  it.each([
    "",
    "records.item:1234",
    "records.item:12345678123456789abc1234567890ab",
    "records.item:12345678-1234-5678-9ABC-1234567890ab",
    "records.item:{12345678-1234-5678-9abc-1234567890ab}",
    " records.item:12345678-1234-5678-9abc-1234567890ab",
    "records.item:12345678-1234-5678-9abc-1234567890ab/../../current",
    "records/item:12345678-1234-5678-9abc-1234567890ab",
  ])("rejects non-canonical reference %j", (ref) => {
    expect(recordHref(fixture("path"), [PROVIDER], [PROVIDER.id], ref)).toBeNull();
  });

  it("returns null when routing or the provider is absent", () => {
    expect(recordHref(undefined, [PROVIDER], [PROVIDER.id], ITEM_REF)).toBeNull();
    expect(recordHref(fixture("path"), [], [PROVIDER.id], ITEM_REF)).toBeNull();
  });

  it("returns null when the provider is disabled or its surface is not served", () => {
    expect(recordHref(fixture("path"), [PROVIDER], [], ITEM_REF)).toBeNull();
    const unrouted = fixture("path");
    expect(
      recordHref(
        { ...unrouted, surfaces: { ...unrouted.surfaces, modules: {} } },
        [PROVIDER],
        [PROVIDER.id],
        ITEM_REF,
      ),
    ).toBeNull();
  });

  it("returns null when the provider publishes no type mapping", () => {
    expect(
      recordHref(
        fixture("path"),
        [{ ...PROVIDER, recordRoutes: [] }],
        [PROVIDER.id],
        ITEM_REF,
      ),
    ).toBeNull();
  });

  it("returns null when the published route is no longer declared", () => {
    expect(
      recordHref(
        fixture("path"),
        [
          {
            ...PROVIDER,
            routes: [{ id: "other-detail", path: "/wrong", screen }],
          },
        ],
        [PROVIDER.id],
        ITEM_REF,
      ),
    ).toBeNull();
  });

  it("returns null rather than linking through an unserved routing surface", () => {
    const config = withSurface(fixture("path"), {
      host: "archive",
      path: "/archive",
      external: true,
    });
    expect(recordHref(config, [PROVIDER], [PROVIDER.id], ITEM_REF)).toBeNull();
  });

  it("does not accept a caller-controlled owner, type, route, path, or host", () => {
    const hostile =
      "https.item:12345678-1234-5678-9abc-1234567890ab?host=evil.example&route=admin";
    expect(recordHref(fixture("path"), [PROVIDER], [PROVIDER.id], hostile)).toBeNull();
    expect(
      recordHref(
        fixture("path"),
        [PROVIDER],
        [PROVIDER.id],
        "records.project:12345678-1234-5678-9abc-1234567890ab",
      ),
    ).toBeNull();
  });
});
