import { compileTheme, validateTheme } from "@rheo-stream/web-contract/theme";

/**
 * The root layout's theme path: validate `raw` as a built-in theme, then compile
 * the checked copy `validateTheme` returns. It lives here rather than in
 * `layout.tsx` because a layout module may only export what Next.js allows, and
 * the test for the refusal branch has to run this exact function.
 *
 * A theme that fails validation throws, naming every error, so a broken built-in
 * theme stops boot or the build. It never renders an unstyled page.
 */
export function builtInThemeCss(raw: unknown): string {
  const result = validateTheme(raw, { origin: "built-in" });
  if (!result.ok) {
    const detail = result.errors
      .map((error) => `${error.token ?? error.field ?? "theme"}: ${error.reason}`)
      .join("; ");
    throw new Error(`built-in theme failed validation: ${detail}`);
  }
  return compileTheme(result.theme);
}
