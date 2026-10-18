"use server";
import { cookies } from "next/headers";
import { refresh } from "next/cache";
import { loadRoutingConfig } from "@/lib/routing/load";
import { themeId } from "./builtins";
import { THEME_COOKIE, themeCookieDomain } from "./theme-cookie";
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
  (await cookies()).set(THEME_COOKIE, selected, {
    path: "/",
    httpOnly: true,
    sameSite: "lax",
    maxAge: 31536000,
    secure: process.env.NODE_ENV === "production",
    ...(domain ? { domain } : {}),
  });
  refresh();
}
