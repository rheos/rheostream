/**
 * Compile-time refusals, enforced by `tsc --noEmit` (no runtime test runs them).
 *
 * The positive proof that `ComposedModule` and `rheo web compose` agree is not here.
 * It is `apps/web/src/modules.generated.ts`: the generator's real, committed output,
 * written `as const satisfies readonly ComposedModule[]` over the real module
 * packages' real exports, so `tsc` checks it end to end on every run.
 *
 * What that file can never show is a refusal: every module that ships is, by
 * construction, correctly shaped. The three `@ts-expect-error` probes below pin the
 * load-bearing refusals against a small synthetic module instead, which is the only
 * reason this file keeps one: a route whose `screen` is synchronous, takes the
 * wrong props, or names an export the module does not have must not type-check.
 */
import type { ReactNode } from "react";

import type { ComposedModule, Screen } from "./screen";

const probeWeb = {
  screens: {
    home: async (): Promise<ReactNode> => null,
  },
} as const;

const synchronousScreen = (): ReactNode => null;
const wrongPropsScreen = async (props: { other: number }): Promise<ReactNode> =>
  props.other;

// @ts-expect-error a screen must return a Promise (it is an async server component)
export const refusesSynchronous: Screen = synchronousScreen;

// @ts-expect-error a screen must accept ScreenProps
export const refusesWrongProps: Screen = wrongPropsScreen;

export const refusesMissingScreen = [
  {
    id: "compose_probe",
    surface: "compose_ui",
    navigation: [],
    routes: [
      // @ts-expect-error the generator's property access fails when the export is missing
      { id: "home", path: "/", screen: probeWeb.screens.missing },
    ],
    recordViews: [],
    forms: [],
    searchProviders: [],
  },
] as const satisfies readonly ComposedModule[];
