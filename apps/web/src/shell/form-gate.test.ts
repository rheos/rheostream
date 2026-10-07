import { describe, expect, it } from "vitest";
import type { ComposedModule } from "@rheo-stream/web-contract/screen";
import { formAllowed } from "./form-gate";
const contribution: ComposedModule = {
  id: "probe",
  surface: "probe",
  routes: [],
  navigation: [],
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
