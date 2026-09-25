import { defineConfig } from "vitest/config";

/**
 * The Recallatron screens' vitest configuration, the same shape as
 * apps/web/vitest.config.ts. `esbuild.jsx` is set here for the reason that file's
 * docstring gives: without it esbuild emits the classic `React.createElement` and
 * nothing in this package imports the `React` namespace. `environment: "node"` is
 * enough because every view is rendered with `renderToStaticMarkup`, which needs
 * no DOM.
 */
export default defineConfig({
  esbuild: { jsx: "automatic", jsxImportSource: "react" },
  test: {
    environment: "node",
    include: ["src/**/*.test.ts", "src/**/*.test.tsx"],
  },
});
