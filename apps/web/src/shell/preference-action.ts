"use server";

import { preferenceCookie } from "@rheo-stream/web-contract/screen";
import { cookies } from "next/headers";

/** The longest preference value kept; anything longer is dropped, not cut. */
const MAX_PREFERENCE_CHARS = 1024;

/** Saves one module preference as a host-only, httpOnly cookie (#369), so the
 * next server render of that module's screen can read it through
 * `ShellApi.preference`. The module id is bound by the shell; a name that is not
 * a preference name, or a value too long or not text, saves nothing. It is a
 * convenience: a refused save leaves the screen working on its defaults. */
export async function savePreference(
  moduleId: string,
  name: string,
  value: string,
): Promise<void> {
  const cookie = preferenceCookie(moduleId, name);
  if (cookie === null || typeof value !== "string") return;
  if (value.length > MAX_PREFERENCE_CHARS) return;
  (await cookies()).set(cookie, value, {
    path: "/",
    httpOnly: true,
    sameSite: "lax",
    maxAge: 31536000,
    secure: process.env.NODE_ENV === "production",
  });
}
