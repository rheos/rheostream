import { readFileSync } from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";

import type { RoutingConfig } from "@/lib/routing/config";

import { LEGACY_THEME_COOKIE, readThemeCookie, THEME_COOKIE, themeCookieDomain } from "./theme-cookie";

const FIXTURES = path.resolve(import.meta.dirname, "../../../../tests/fixtures/routing");
const fixture = (mode: "path" | "subdomain") =>
  JSON.parse(readFileSync(path.join(FIXTURES, `${mode}-mode.json`), "utf8")) as RoutingConfig;

function jar(values: Record<string, string>) {
  return { get: (name: string) => (name in values ? { value: values[name] } : undefined) };
}

describe("the theme cookie (#408)", () => {
  it("prefers the shared cookie and falls back to the old host-only one", () => {
    expect(readThemeCookie(jar({ [THEME_COOKIE]: "novadiem-dark", [LEGACY_THEME_COOKIE]: "greenstream-dark" }))).toBe(
      "novadiem-dark",
    );
    expect(readThemeCookie(jar({ [LEGACY_THEME_COOKIE]: "novadiem-light" }))).toBe("novadiem-light");
    expect(readThemeCookie(jar({}))).toBeUndefined();
  });

  it("scopes to the base host in subdomain mode, so every module host shares it", () => {
    expect(themeCookieDomain(fixture("subdomain"))).toBe("example.test");
    expect(themeCookieDomain({ ...fixture("subdomain"), base_host: "Example.test:8443" })).toBe("example.test");
  });

  it("stays host-only in path mode, without routing, and for hosts that cannot take a domain", () => {
    expect(themeCookieDomain(fixture("path"))).toBeUndefined();
    expect(themeCookieDomain(null)).toBeUndefined();
    for (const base_host of ["localhost:3000", "127.0.0.1", "[::1]:3000"]) {
      expect(themeCookieDomain({ ...fixture("subdomain"), base_host })).toBeUndefined();
    }
  });
});
