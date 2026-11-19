"use server";
import { cookies } from "next/headers";
import { refresh } from "next/cache";
import { loadRoutingConfig } from "@/lib/routing/load";
import { themeCookieDomain } from "@/theme/theme-cookie";
import {
  canonicalTimeZone,
  readTimeZoneCookie,
  TIME_ZONE_COOKIE,
  timeZoneCookieValue,
  type ViewerTimeZone,
} from "./time-zone";

async function store(zone: ViewerTimeZone): Promise<void> {
  const routing = await loadRoutingConfig();
  // Without routing the domain is unknown, and a host-only copy written now would
  // shadow the shared cookie later (the newer duplicate is the one read).
  if (routing.state !== "ok") return;
  // The theme's domain rule is the cookie rule for any shell-wide preference.
  const domain = themeCookieDomain(routing.config);
  (await cookies()).set(TIME_ZONE_COOKIE, timeZoneCookieValue(zone), {
    path: "/",
    httpOnly: true,
    sameSite: "lax",
    maxAge: 31536000,
    secure: process.env.NODE_ENV === "production",
    ...(domain ? { domain } : {}),
  });
  refresh();
}

/** The zone the browser reports. Kept unless the viewer chose one, and only
 * written when it changes, so the page re-renders once, not on every load. */
export async function detectTimeZone(browserZone: string): Promise<void> {
  const zone = canonicalTimeZone(browserZone);
  if (!zone) return;
  const current = readTimeZoneCookie(await cookies());
  if (current?.chosen || current?.zone === zone) return;
  await store({ zone, chosen: false });
}

/** The settings choice: `auto` follows the browser again, anything else fixes it. */
export async function selectTimeZone(form: FormData): Promise<void> {
  const choice = form.get("zone"),
    browser = form.get("browser");
  const chosen = canonicalTimeZone(choice);
  if (choice === "auto") {
    // Without scripts there is no browser zone; a fixed zone must not linger on
    // as if detected, so it falls back to UTC until the browser reports one.
    const previous = readTimeZoneCookie(await cookies());
    const kept = previous && !previous.chosen ? previous.zone : null;
    await store({ zone: canonicalTimeZone(browser) ?? kept ?? "UTC", chosen: false });
  } else if (chosen) {
    await store({ zone: chosen, chosen: true });
  }
}
