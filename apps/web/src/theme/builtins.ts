import seedDarkTheme from "./themes/seed-dark.json";

import greenStream from "./themes/greenstream-dark.json";
import novadiemDark from "./themes/novadiem-dark.json";
import novadiemLight from "./themes/novadiem-light.json";

/** Registry membership, never theme input, confers built-in authority. */
export const BUILT_IN_THEMES = {
  "seed-dark": seedDarkTheme,
  "greenstream-dark": greenStream,
  "novadiem-dark": novadiemDark,
  "novadiem-light": novadiemLight,
} as const;
export const THEME_CHOICES = [
  { id: "greenstream-dark", family: "greenstream", scheme: "dark", label: "GreenStream" },
  { id: "novadiem-dark", family: "novadiem", scheme: "dark", label: "Novadiem" },
  { id: "novadiem-light", family: "novadiem", scheme: "light", label: "Novadiem" },
] as const;
export type ThemeId = typeof THEME_CHOICES[number]["id"];
export function themeId(value: string | undefined): ThemeId {
  return THEME_CHOICES.find((theme) => theme.id === value)?.id ?? "greenstream-dark";
}
