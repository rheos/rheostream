import { describe, expect, it } from "vitest";
import type { ComposedModule } from "@rheo-stream/web-contract/screen";
import { MODULES } from "../modules.generated";
import { formAllowed } from "./form-gate";
const contribution: ComposedModule = {
  id: "probe",
  surface: "probe",
  routes: [],
  navigation: [],
  recordRoutes: [],
  recordViews: [],
  searchProviders: [],
  readOperations: ["probe.read"],
  submitOperations: ["probe.save"],
  forms: [
    { operation: "probe.save", component: null },
    { operation: "probe.erase", component: null },
  ],
};
describe("form grants", () => {
  it("allows an explicit generated grant only for an enabled and routed module", () => {
    expect(
      formAllowed([contribution], "probe", "probe.save", ["probe"], {
        probe: {},
      }),
    ).toBe(true);
    expect(
      formAllowed([contribution], "probe", "probe.save", [], { probe: {} }),
    ).toBe(false);
    expect(
      formAllowed([contribution], "probe", "probe.save", ["probe"], {}),
    ).toBe(false);
  });
  it.each([
    "probe.read",
    "probe.erase",
    "core.record.delete",
    "foreign.save",
    "probe.unknown",
  ])("refuses %s before dispatch", (operation) =>
    expect(
      formAllowed([contribution], "probe", operation, ["probe"], { probe: {} }),
    ).toBe(false),
  );
  it("requires both declaration and generated grant", () =>
    expect(
      formAllowed(
        [{ ...contribution, forms: [] }],
        "probe",
        "probe.save",
        ["probe"],
        { probe: {} },
      ),
    ).toBe(false));
});

describe("generated Leads handoff form grants", () => {
  const routedSurfaces = { leads: {} };

  it.each(["leads.handoff.request", "leads.handoff.restage"])(
    "allows %s through the generated provider contract",
    (operation) =>
      expect(
        formAllowed(MODULES, "leads", operation, ["leads"], routedSurfaces),
      ).toBe(true),
  );

  it.each([
    "leads.handoff.get",
    "leads.handoff.list",
    "leads.handoff.current_view",
  ])("keeps read operation %s out of the form gate", (operation) =>
    expect(
      formAllowed(MODULES, "leads", operation, ["leads"], routedSurfaces),
    ).toBe(false),
  );
});
