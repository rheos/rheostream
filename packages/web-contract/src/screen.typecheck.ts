/**
 * Compile-time checks, enforced by `tsc --noEmit` (no runtime test runs them).
 *
 * `goldenModules` reproduces the shape `rheo web compose` generates (the golden
 * text in tests/test_web_compose.py), with local stand-ins for the imported
 * module packages. If `ComposedModule` and the generator ever disagree on a key
 * or a type, this file stops compiling. The `@ts-expect-error` lines pin the
 * load-bearing refusal: a route whose `screen` is missing or has the wrong
 * signature must not type-check.
 */
import type { ReactNode } from "react";

import type { ComposedModule, Screen, ScreenProps } from "./screen";

const probeWeb = {
  screens: {
    home: async ({ shell, query }: ScreenProps): Promise<ReactNode> =>
      `${shell.role}:${query.q ?? ""}`,
    settings: async (): Promise<ReactNode> => null,
  },
  components: { NoteView: () => null, NoteForm: () => null },
} as const;

export const goldenModules = [
  {
    id: "compose_probe",
    surface: "compose_ui",
    navigation: [
      { id: "home", label: "Home", path: "/", roles: ["member", "owner"] },
      { id: "settings", label: "Settings", path: "/settings", roles: ["owner"] },
    ],
    routes: [
      { id: "home", path: "/", screen: probeWeb.screens.home },
      { id: "settings", path: "/settings", screen: probeWeb.screens.settings },
    ],
    recordViews: [
      { recordType: "compose_probe.note", component: probeWeb.components.NoteView },
    ],
    forms: [
      { operation: "compose_probe.note.add", component: probeWeb.components.NoteForm },
    ],
    searchProviders: [{ id: "find", operation: "compose_probe.note.find" }],
  },
] as const satisfies readonly ComposedModule[];

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
