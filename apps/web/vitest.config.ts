import { fileURLToPath } from "node:url";

import { defineConfig } from "vitest/config";

/**
 * The web tier's vitest configuration (C9).
 *
 * Created by this chunk — `apps/web` ran vitest configless until now, and
 * `core-health.test.ts` (0a's) must keep passing under it unchanged.
 *
 * Two things this file deliberately does NOT do:
 *
 * - It does not set `server.fs.allow` for the repo-root fixture. That is a Vite
 *   **dev-server** option and has no effect on `vitest run`; the routing tests read
 *   `tests/fixtures/routing/*.json` with `readFileSync` from `import.meta.dirname`
 *   instead, which needs no allowance at all.
 * - It adds no plugin and no new dependency. The `esbuild.jsx` setting below is
 *   neither: it is one Vite option, not `@vitejs/plugin-react`.
 *
 * That second bullet used to read "nothing renders a component, so no DOM
 * environment and no React transform are needed". A throwaway vertical-slice
 * surface briefly made the second half of that untrue by adding this
 * repository's first `.tsx` test; that surface was deleted again at 0c0's branch
 * cut, so no test under `include` executes JSX today. **The `esbuild.jsx` block
 * below is retained for the next `.tsx` test this repository writes, and is
 * inert until there is one** — it costs nothing while no test executes JSX
 * (measured; run 0v's finding F22, cited again below), and re-deriving it from a
 * `ReferenceError` later would cost more than keeping it. The half that never
 * stopped being true is worth keeping too: `environment: "node"` is still
 * correct, because nothing needs a DOM — `renderToStaticMarkup` does not.
 *
 * **Why `esbuild.jsx` is set here rather than in `tsconfig.json`.** Vite copies
 * the nearest tsconfig's `compilerOptions.jsx` into its esbuild call unless this
 * config sets `esbuild.jsx` itself. `apps/web/tsconfig.json` says `"preserve"`,
 * and esbuild does not honour `"preserve"` arriving from the tsconfig field: it
 * falls back to the classic runtime and emits bare `React.createElement(...)`
 * with no React import injected. No `.tsx` file in `apps/web` imports the `React`
 * namespace — Next.js supplies the automatic runtime through its own compiler,
 * which vitest does not use — so the failure is `ReferenceError: React is not
 * defined`, and it fires wherever JSX **executes**, which includes the imported
 * `page.tsx` itself and not only the test file. Importing a `.tsx` component
 * still succeeds, so a clean import is not evidence of a clean transform.
 *
 * Changing `tsconfig.json` to `"react-jsx"` would cure the same symptom by
 * altering the JSX semantics of the whole application build to satisfy the test
 * runner; `"preserve"` is what Next.js's own compiler expects. Setting it here
 * scopes the change to vitest and leaves `next build` untouched. Measured both
 * ways on this toolchain (vitest 5.0.0, esbuild 0.28.2): the shipped `.ts` tests
 * pass identically with and without the line, so it is inert for everything that
 * does not execute JSX. See run 0v's finding F22.
 *
 * `RHEO_ROUTING_MODE` needs no passthrough machinery either — vitest already
 * exposes `process.env` in the node environment. The `env` entry below is only a
 * default for a bare local `pnpm -C apps/web test`; CI overrides it per mode
 * (`RHEO_ROUTING_MODE=path` then `=subdomain`), and an exported value always wins
 * over this default.
 */
export default defineConfig({
  // The automatic JSX runtime, kept for the next test that executes JSX. See the
  // docstring above: without it esbuild emits classic `React.createElement`, and
  // nothing in `apps/web` imports the `React` namespace.
  esbuild: { jsx: "automatic", jsxImportSource: "react" },
  // The same `@/*` -> `src/*` mapping `tsconfig.json:17-19` gives the compiler and
  // Next.js the bundler. Without it every source file's `@/lib/...` import would
  // resolve under `next build` and fail under `vitest run`.
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
    },
  },
  test: {
    environment: "node",
    include: ["src/**/*.test.ts", "src/**/*.test.tsx"],
    env: {
      RHEO_ROUTING_MODE: process.env.RHEO_ROUTING_MODE ?? "path",
    },
  },
});
