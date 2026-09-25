import { defineConfig } from "vitest/config";

/**
 * The shared web contract's vitest configuration, the same shape as
 * apps/web/vitest.config.ts minus what this package does not need: no JSX is
 * executed here (the package holds types, a JSON contract and two pure
 * functions), so there is no `esbuild.jsx` block and no `@/*` alias.
 */
export default defineConfig({
  test: {
    environment: "node",
    include: ["src/**/*.test.ts", "theme/**/*.test.ts"],
  },
});
