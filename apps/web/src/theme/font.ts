import localFont from "next/font/local";

/**
 * Geist Variable, vendored and self-hosted: no fetch at build time or run time.
 *
 * Provenance: `files/geist-latin-wght-normal.woff2` and `LICENSE` (committed as
 * `fonts/OFL.txt`, unmodified) from the npm package `@fontsource-variable/geist`
 * 5.3.0, whose metadata gives the source as the Google Fonts repository and the
 * licence as SIL OFL 1.1, Copyright 2024 The Geist Project Authors
 * (https://github.com/vercel/geist-font). This is the Latin subset with the full
 * weight axis; characters outside it fall through to the system stack that
 * follows `var(--rs-font-brand)` in the theme's `font.sans`.
 *
 * Exposed only as the CSS variable `--rs-font-brand`, which the theme's
 * `font.sans` token references. Nothing styles with the class name directly.
 */
export const brandFont = localFont({
  src: "./fonts/geist-latin-wght-normal.woff2",
  weight: "100 900",
  style: "normal",
  display: "swap",
  variable: "--rs-font-brand",
});
