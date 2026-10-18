"use server";
import { cookies } from "next/headers";
import { refresh } from "next/cache";
import { loadRoutingConfig } from "@/lib/routing/load";
import { themeId } from "./builtins";
import { LEGACY_THEME_COOKIE, THEME_COOKIE, themeCookieDomain } from "./theme-cookie";
export async function selectTheme(form: FormData): Promise<void> {
  const family = form.get("family"),
    scheme = form.get("scheme");
  const selected = themeId(
    family === "novadiem"
      ? `novadiem-${scheme === "light" ? "light" : "dark"}`
      : "greenstream-dark",
  );
  const routing = await loadRoutingConfig();
  const domain = themeCookieDomain(routing.state === "ok" ? routing.config : null);
  const jar = await cookies();
  // The new choice replaces this host's pre-#408 one rather than leaving it to
  // linger as a fallback for a year.
  if (jar.get(LEGACY_THEME_COOKIE)) jar.delete(LEGACY_THEME_COOKIE);
  jar.set(THEME_COOKIE, selected, {
    path: "/",
    httpOnly: true,
    sameSite: "lax",
    maxAge: 31536000,
    secure: process.env.NODE_ENV === "production",
    ...(domain ? { domain } : {}),
  });
  refresh();
}
