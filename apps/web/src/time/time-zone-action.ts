"use server";
import { cookies } from "next/headers";
import { refresh } from "next/cache";
import { loadRoutingConfig } from "@/lib/routing/load";
import { themeCookieDomain } from "@/theme/theme-cookie";
import {
  isTimeZone,
  readTimeZoneCookie,
  TIME_ZONE_COOKIE,
  timeZoneCookieValue,
  type ViewerTimeZone,
} from "./time-zone";

async function store(zone: ViewerTimeZone): Promise<void> {
  const routing = await loadRoutingConfig();
  // The theme's domain rule is the cookie rule for any shell-wide preference.
  const domain = themeCookieDomain(routing.state === "ok" ? routing.config : null);
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
  if (!isTimeZone(browserZone)) return;
  const current = readTimeZoneCookie(await cookies());
  if (current?.chosen || current?.zone === browserZone) return;
  await store({ zone: browserZone, chosen: false });
}

/** The settings choice: `auto` follows the browser again, anything else fixes it. */
export async function selectTimeZone(form: FormData): Promise<void> {
  const choice = form.get("zone"),
    browser = form.get("browser");
  if (choice === "auto") {
    const previous = readTimeZoneCookie(await cookies());
    const zone = isTimeZone(browser) ? browser : (previous?.zone ?? "UTC");
    await store({ zone, chosen: false });
  } else if (isTimeZone(choice)) {
    await store({ zone: choice, chosen: true });
  }
}
