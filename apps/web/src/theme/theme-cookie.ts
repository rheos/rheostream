import type { RoutingConfig } from "@/lib/routing/config";

/**
 * The theme preference cookie. In subdomain mode every surface is its own host
 * (circuit., leads., current. and so on), so a host-only cookie made each host
 * remember its own theme, and moving between modules switched it back (#408).
 * This cookie is set on the deployment's base host instead, which every surface
 * host shares.
 */
export const THEME_COOKIE = "rheo_theme_site";

/** The host-only cookie used before #408. Still read, after the shared one, so a
 * choice made before the change is kept until the next one replaces it. */
export const LEGACY_THEME_COOKIE = "rheo_theme";

export function readThemeCookie(jar: {
  get(name: string): { value: string } | undefined;
}): string | undefined {
  return jar.get(THEME_COOKIE)?.value ?? jar.get(LEGACY_THEME_COOKIE)?.value;
}

/**
 * The cookie `Domain` that reaches every surface host: the base host in subdomain
 * mode, without its port. None in path mode, where every surface is already one
 * host, and none for a bare name or an IP address, which cannot carry a domain
 * cookie.
 */
export function themeCookieDomain(config: RoutingConfig | null): string | undefined {
  if (config === null || config.mode !== "subdomain") {
    return undefined;
  }
  const host = config.base_host.replace(/:\d+$/, "").toLowerCase();
  if (!host.includes(".") || host.startsWith("[") || /^[\d.]+$/.test(host)) {
    return undefined;
  }
  return host;
}
