import { unstable_doesMiddlewareMatch } from "next/experimental/testing/server";
import { describe, expect, it } from "vitest";

import { config } from "./middleware";

/**
 * Which paths skip the sign-in middleware. The icons (#616) must load on the
 * sign-in page's tab, so they skip it; anything that merely starts with an
 * icon's name is still a workspace path and stays protected.
 */
function matches(path: string): boolean {
  return unstable_doesMiddlewareMatch({ config, url: `https://leads.example.com${path}` });
}

describe("middleware matcher", () => {
  it.each(["/favicon.ico", "/icon.svg", "/icon.svg?6b1c2d3e4f5a6b7c", "/apple-icon.png", "/login", "/healthz"])(
    "lets %s through without sign-in",
    (path) => {
      expect(matches(path)).toBe(false);
    },
  );

  it.each(["/", "/queue", "/icon.svg/extra", "/favicon.icon", "/apple-icon.pngx", "/faviconXico"])(
    "still guards %s",
    (path) => {
      expect(matches(path)).toBe(true);
    },
  );
});
