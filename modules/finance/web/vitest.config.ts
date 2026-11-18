import { defineConfig } from "vitest/config";

/**
 * The Finance screens' vitest configuration, the same shape as Current's. The
 * views are server components with no client script, rendered with
 * `renderToStaticMarkup`, so the node environment is enough.
 */
export default defineConfig({
  esbuild: { jsx: "automatic", jsxImportSource: "react" },
  test: {
    environment: "node",
    include: ["src/**/*.test.ts", "src/**/*.test.tsx"],
  },
});
