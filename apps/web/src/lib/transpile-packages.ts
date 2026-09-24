/**
 * The workspace packages `next.config.ts` must transpile: every `@rheo-stream/*`
 * name among a package.json's `dependencies` and `devDependencies`, sorted.
 *
 * Workspace packages ship TypeScript source, and Next only compiles a dependency's
 * source when it is listed in `transpilePackages`. Deriving the list from
 * `apps/web/package.json` means adding a module's web package there (the one hand
 * edit `rheo web compose` asks for) is the whole change; there is no second list.
 */

export const WORKSPACE_SCOPE = "@rheo-stream/";

interface PackageManifest {
  dependencies?: Readonly<Record<string, string>>;
  devDependencies?: Readonly<Record<string, string>>;
}

export function workspaceTranspilePackages(manifest: PackageManifest): string[] {
  const names = new Set([
    ...Object.keys(manifest.dependencies ?? {}),
    ...Object.keys(manifest.devDependencies ?? {}),
  ]);
  return [...names].filter((name) => name.startsWith(WORKSPACE_SCOPE)).sort();
}
