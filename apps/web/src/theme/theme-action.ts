"use server";
import { cookies } from "next/headers";
import { refresh } from "next/cache";
import { themeId } from "./builtins";
export async function selectTheme(form: FormData): Promise<void> {
  const family = form.get("family"),
    scheme = form.get("scheme");
  const selected = themeId(
    family === "novadiem"
      ? `novadiem-${scheme === "light" ? "light" : "dark"}`
      : "greenstream-dark",
  );
  (await cookies()).set("rheo_theme", selected, {
    path: "/",
    httpOnly: true,
    sameSite: "lax",
    maxAge: 31536000,
    secure: process.env.NODE_ENV === "production",
  });
  refresh();
}
