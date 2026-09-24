import seedDarkTheme from "./themes/seed-dark.json";

/**
 * The built-in theme registry.
 *
 * Built-in-ness comes from being listed here, never from a field inside the theme
 * file. `validateTheme(..., { origin: "built-in" })` lets a theme carry the
 * reserved `chrome.*` tokens, so the caller has to decide `origin` from this
 * registry: a theme file cannot promote itself by claiming it.
 *
 * Values are typed `unknown` on purpose. A JSON import is not a checked
 * `ThemeFile`, and this type makes the only way to get one `validateTheme`.
 *
 * `seed-dark` is the seed default that run 2d replaces or extends, not the final
 * visual canon. Its sources, so 2d can tell them apart at a glance:
 *
 * Verbatim from the site's DESIGN.md token table (pinned by builtins.test.ts):
 *   color.ground, color.surface-1 (DESIGN.md's "ground-raised"), color.ink,
 *   color.ink-dim, color.ink-faint, color.accent, color.accent-strong,
 *   color.hairline.
 *
 * Derived from DESIGN.md's rules, not DESIGN.md values:
 *   color.surface-2       one more step from ground along the ground to surface-1
 *                         line; held dark enough that ink-faint keeps AA on it.
 *   color.on-accent       the ground color, so text on an accent fill stays in
 *                         the palette.
 *   color.success/warning/danger/info
 *                         muted hues, lighter and less saturated than accent so
 *                         none reads as a second signal color; each clears AA as
 *                         text on every surface.
 *   color.control-bg      surface-1 (a control is a lifted surface).
 *   color.control-border  a ground-hue tint between ink-faint and ground, at least
 *                         3:1 against every surface (WCAG non-text contrast).
 *   color.control-focus   accent ("focus ring ... themed from the palette").
 *   color.control-placeholder
 *                         ink-faint (tertiary text).
 *   font.sans             the vendored Geist via --rs-font-brand, then a system
 *                         stack. DESIGN.md names Geist; the fallback is new.
 *   font.mono             an ordinary system monospace stack. New: DESIGN.md
 *                         specifies no monospace.
 *   font.size-*           a plain rem scale. New.
 *   font.tracking-display DESIGN.md's "display tracking floor around -0.037em".
 *   radius.*, space.*, size.control
 *                         a plain px/rem scale. New.
 *   size.hairline         1px, DESIGN.md's "1px section dividers".
 *   chrome.*              surface-1, ink, control-border and danger, with a
 *                         system font at the md size, so confirmation chrome
 *                         never depends on the brand font loading.
 */
export const BUILT_IN_THEMES: Readonly<Record<"seed-dark", unknown>> = {
  "seed-dark": seedDarkTheme,
};
