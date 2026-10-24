/**
 * The viewer's time zone (#465). Screens render on the server, which runs in UTC,
 * so the browser's zone reaches it in a cookie: `TimeZoneSync` writes the zone the
 * browser reports, and the settings menu can fix one instead. Like the theme
 * cookie (#408) it is set on the base host, so every module host shares it.
 *
 * The value is `auto:<zone>` when it follows the browser and `set:<zone>` when the
 * viewer chose it; detection never replaces a chosen zone.
 */
export const TIME_ZONE_COOKIE = "rheo_tz_site";

export interface ViewerTimeZone {
  /** An IANA zone name such as `America/Vancouver`. */
  zone: string;
  /** True when the viewer picked it, false when it follows the browser. */
  chosen: boolean;
}

/** True for a zone name this runtime can format in. */
export function isTimeZone(value: unknown): value is string {
  if (typeof value !== "string" || value === "" || value.length > 64) return false;
  try {
    new Intl.DateTimeFormat("en", { timeZone: value });
    return true;
  } catch {
    return false;
  }
}

export function timeZoneCookieValue(zone: ViewerTimeZone): string {
  return `${zone.chosen ? "set" : "auto"}:${zone.zone}`;
}

export function readTimeZoneCookie(jar: {
  get(name: string): { value: string } | undefined;
}): ViewerTimeZone | null {
  const match = /^(auto|set):(.+)$/.exec(jar.get(TIME_ZONE_COOKIE)?.value ?? "");
  if (!match || !isTimeZone(match[2])) return null;
  return { zone: match[2], chosen: match[1] === "set" };
}

/** Every zone the runtime knows, for the settings choice. */
export function timeZoneNames(): string[] {
  const names = Intl.supportedValuesOf("timeZone");
  return names.includes("UTC") ? names : ["UTC", ...names];
}
