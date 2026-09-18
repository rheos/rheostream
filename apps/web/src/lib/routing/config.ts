/**
 * The routing configuration as the web tier sees it (C9, B10).
 *
 * This is the TypeScript half of `packages/core/src/rheo_core/routing/config.py`.
 * The two are proven equal rather than merely alike: both read the same bytes from
 * `tests/fixtures/routing/{path,subdomain}-mode.json`, and the live value arrives
 * here as `RoutingConfig.model_dump(mode="json")` over `GET /internal/v1/routing`.
 *
 * So the field names are the **JSON** field names — `base_host`, `fixed_path` —
 * not a camelCase web-side shape. `core-health.ts` remaps `contract_version` to
 * `contractVersion` because nothing else reads that body; this object, by
 * contrast, is the shared contract itself, and a rename here would put a
 * translation layer between the fixture and the functions the fixture exists to
 * pin.
 *
 * `path` is optional because the fixture omits it for `docs` and `integration`:
 * both are `external`, this application never links to them, and `url_for`
 * refuses them in both languages. The type agrees with the fixture, not the other
 * way round.
 */

export type RoutingMode = "path" | "subdomain";

export const SHELL = "shell";
export const IDENTITY = "identity";
export const API = "api";
export const MCP = "mcp";
export const DOCS = "docs";
export const INTEGRATION = "integration";

/** The surfaces every deployment has. Module surfaces are named at runtime. */
export const NAMED_SURFACES = [
  SHELL,
  IDENTITY,
  API,
  MCP,
  DOCS,
  INTEGRATION,
] as const;

export interface SurfaceConfig {
  host: string;
  path?: string;
  external?: boolean;
  reserved?: boolean;
  fixed_path?: boolean;
}

export interface Surfaces {
  shell: SurfaceConfig;
  identity: SurfaceConfig;
  api: SurfaceConfig;
  mcp: SurfaceConfig;
  docs: SurfaceConfig;
  integration: SurfaceConfig;
  modules: Record<string, SurfaceConfig>;
}

export interface RoutingConfig {
  mode: RoutingMode;
  scheme: string;
  base_host: string;
  public_host: string;
  surfaces: Surfaces;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isSurfaceConfig(value: unknown): value is SurfaceConfig {
  if (!isRecord(value)) {
    return false;
  }
  if (typeof value.host !== "string") {
    return false;
  }
  if (value.path !== undefined && typeof value.path !== "string") {
    return false;
  }
  for (const flag of ["external", "reserved", "fixed_path"] as const) {
    if (value[flag] !== undefined && typeof value[flag] !== "boolean") {
      return false;
    }
  }
  return true;
}

/**
 * Narrow a parsed JSON body to a `RoutingConfig`.
 *
 * Mirrors `core-health.ts`'s `isCoreHealthBody` rather than trusting the shape:
 * the body arrives over the network from the internal listener, and `load.ts`
 * degrades to its "unavailable" sentinel for anything that does not pass here.
 * Strict on purpose — both producers (the fixture and
 * `RoutingConfig.model_dump`) always emit every field below, including an empty
 * `modules`, so a body missing one is a contract break, not a variant to tolerate.
 */
export function isRoutingConfig(value: unknown): value is RoutingConfig {
  if (!isRecord(value)) {
    return false;
  }
  if (value.mode !== "path" && value.mode !== "subdomain") {
    return false;
  }
  if (
    typeof value.scheme !== "string" ||
    typeof value.base_host !== "string" ||
    typeof value.public_host !== "string"
  ) {
    return false;
  }
  const surfaces = value.surfaces;
  if (!isRecord(surfaces)) {
    return false;
  }
  for (const name of NAMED_SURFACES) {
    if (!isSurfaceConfig(surfaces[name])) {
      return false;
    }
  }
  const modules = surfaces.modules;
  if (!isRecord(modules)) {
    return false;
  }
  return Object.values(modules).every(isSurfaceConfig);
}
