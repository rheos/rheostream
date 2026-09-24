import { readFileSync } from "node:fs";
import path from "node:path";

import type { ComposedModule } from "@rheo-stream/web-contract/screen";
import { describe, expect, it } from "vitest";

import { enabledModuleIds, isWorkspaceStatus, type WorkspaceStatus } from "@/lib/operations";
import { MODULES } from "@/modules.generated";
import { composeNavigation, findModuleRoute, visibleModules } from "@/shell/compose";

/**
 * The run's outcome metric: enabling a module makes its navigation and routes
 * appear, and nothing else does. Composition runs over the **real** committed
 * `MODULES` and the two synthetic `core.workspace.status` fixtures, so a composed
 * module shows only when it is also routed and enabled here. The role dimension
 * uses an injected fixture instead, because every shipped entry is open to both
 * roles and the real list could never show the filter working.
 */

const FIXTURES = path.resolve(import.meta.dirname, "../../../../tests/fixtures/web");

function status(name: "enabled" | "absent"): WorkspaceStatus {
  const value: unknown = JSON.parse(
    readFileSync(path.join(FIXTURES, `workspace-status-${name}.json`), "utf8"),
  );
  if (!isWorkspaceStatus(value)) {
    throw new Error(`workspace-status-${name}.json is not a workspace status`);
  }
  return value;
}

const ENABLED = enabledModuleIds(status("enabled"));
const ABSENT = enabledModuleIds(status("absent"));

/** The routing config's module surfaces, as `routing_config()` would carry them. */
const ROUTED = { recallatron: { host: "recallatron", path: "/recallatron" } };

const ROUTES = ["/", "/search", "/item", "/duplicates"] as const;

describe("workspace-status fixtures", () => {
  it("both pass the guard, and only the enabled one enables anything", () => {
    expect(isWorkspaceStatus(status("enabled"))).toBe(true);
    expect(isWorkspaceStatus(status("absent"))).toBe(true);
    expect(ENABLED).toEqual(["recallatron"]);
    expect(ABSENT).toEqual([]);
  });

  it("the guard refuses a status missing a module field", () => {
    const broken = { ...status("enabled"), modules: [{ module_id: "recallatron" }] };
    expect(isWorkspaceStatus(broken)).toBe(false);
  });
});

describe("composition over the real MODULES", () => {
  it("shows Recallatron's navigation and routes when it is enabled and routed", () => {
    const options = { routingModules: ROUTED, enabledModuleIds: ENABLED };
    const navigation = composeNavigation(MODULES, { ...options, role: "member" });
    expect(navigation.map((entry) => [entry.surface, entry.label, entry.path])).toEqual([
      ["recallatron", "Memory", "/"],
      ["recallatron", "Search memory", "/search"],
      ["recallatron", "Possible duplicates", "/duplicates"],
    ]);
    const visible = visibleModules(MODULES, options);
    for (const route of ROUTES) {
      expect(findModuleRoute(visible, "recallatron", route)).toBeTypeOf("function");
    }
    expect(findModuleRoute(visible, "recallatron", "/unknown")).toBeUndefined();
  });

  it("shows none of it in a workspace where it was never installed", () => {
    const options = { routingModules: ROUTED, enabledModuleIds: ABSENT };
    expect(composeNavigation(MODULES, { ...options, role: "owner" })).toEqual([]);
    const visible = visibleModules(MODULES, options);
    for (const route of ROUTES) {
      expect(findModuleRoute(visible, "recallatron", route)).toBeUndefined();
    }
  });

  it("shows none of it when enabled but its surface is missing from routing", () => {
    const options = { routingModules: {}, enabledModuleIds: ENABLED };
    expect(composeNavigation(MODULES, { ...options, role: "owner" })).toEqual([]);
    const visible = visibleModules(MODULES, options);
    for (const route of ROUTES) {
      expect(findModuleRoute(visible, "recallatron", route)).toBeUndefined();
    }
  });

  it("offers the recall search provider", () => {
    const recallatron = MODULES.find((each) => each.id === "recallatron");
    expect(recallatron?.searchProviders).toContainEqual({
      id: "recall",
      operation: "recallatron.memory.recall",
    });
  });
});

describe("the role filter, over an injected module", () => {
  const screen = async () => null;
  const injected: readonly ComposedModule[] = [
    {
      id: "ledger",
      surface: "ledger",
      navigation: [
        { id: "entries", label: "Entries", path: "/", roles: ["member", "owner"] },
        { id: "settings", label: "Ledger settings", path: "/settings", roles: ["owner"] },
      ],
      routes: [
        { id: "entries", path: "/", screen },
        { id: "settings", path: "/settings", screen },
      ],
      recordViews: [],
      forms: [],
      searchProviders: [],
    },
  ];
  const options = { routingModules: { ledger: {} }, enabledModuleIds: ["ledger"] };

  it("includes an owner-only entry for an owner", () => {
    const labels = composeNavigation(injected, { ...options, role: "owner" }).map(
      (entry) => entry.label,
    );
    expect(labels).toEqual(["Entries", "Ledger settings"]);
  });

  it("excludes it for a member", () => {
    const labels = composeNavigation(injected, { ...options, role: "member" }).map(
      (entry) => entry.label,
    );
    expect(labels).toEqual(["Entries"]);
  });

  it("does not gate the route itself by role", () => {
    const visible = visibleModules(injected, options);
    expect(findModuleRoute(visible, "ledger", "/settings")).toBe(screen);
  });
});
