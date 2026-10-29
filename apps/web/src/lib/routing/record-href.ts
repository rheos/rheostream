import type { ComposedModule } from "@rheo-stream/web-contract/screen";

import type { RoutingConfig } from "@/lib/routing/config";
import { normalizePath, resolveRequest } from "@/lib/routing/resolve";
import { urlFor } from "@/lib/routing/url-for";

const RECORD_REF =
  /^([a-z][a-z0-9_]*)\.([a-z][a-z0-9_]*):[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

/**
 * Resolve a canonical record reference through its provider-published route.
 *
 * The reference chooses only its owner and record type. The provider's composed
 * metadata supplies the surface, route and query key, while the deployment's
 * routing configuration supplies the host and path. A URL is therefore available
 * only when the provider is composed, enabled, served, and still declares the
 * mapped route.
 */
export function recordHref(
  config: RoutingConfig | null | undefined,
  providers: readonly ComposedModule[],
  enabledModuleIds: readonly string[],
  ref: string,
): string | null {
  if (config === null || config === undefined || typeof ref !== "string") {
    return null;
  }
  const parsed = RECORD_REF.exec(ref);
  const owner = parsed?.[1];
  const recordType = parsed?.[2];
  if (owner === undefined || recordType === undefined) {
    return null;
  }

  const provider = providers.find((candidate) => candidate.id === owner);
  if (
    provider === undefined ||
    !enabledModuleIds.includes(provider.id) ||
    !Object.hasOwn(config.surfaces.modules, provider.surface)
  ) {
    return null;
  }
  const mapping = provider.recordRoutes.find(
    (candidate) => candidate.recordType === recordType,
  );
  if (mapping === undefined) {
    return null;
  }
  const route = provider.routes.find((candidate) => candidate.id === mapping.routeId);
  if (route === undefined) {
    return null;
  }

  try {
    const base = urlFor(config, provider.surface, route.path);
    const candidate = new URL(base);
    const resolved = resolveRequest(config, candidate.host, candidate.pathname);
    if (
      resolved.kind !== "module" ||
      resolved.surface !== provider.surface ||
      normalizePath(resolved.subPath) !== normalizePath(route.path)
    ) {
      return null;
    }
    const params = new URLSearchParams();
    params.set(mapping.refQuery, ref);
    return `${base}?${params.toString()}`;
  } catch {
    return null;
  }
}
