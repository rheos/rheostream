import { defineConfig } from "vitest/config";

/**
 * The Current screens' vitest configuration, the same shape as
 * apps/web/vitest.config.ts. `esbuild.jsx` is set here for the reason that file's
 * docstring gives: without it esbuild emits the classic `React.createElement` and
 * nothing in this package imports the `React` namespace. `environment: "node"` is
 * enough for the views, rendered with `renderToStaticMarkup`; the project picker's
 * dialog test opts into happy-dom per file.
 */
export default defineConfig({
  esbuild: { jsx: "automatic", jsxImportSource: "react" },
  test: {
    environment: "node",
    include: ["src/**/*.test.ts", "src/**/*.test.tsx"],
  },
});
