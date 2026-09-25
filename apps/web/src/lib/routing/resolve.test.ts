import { readFileSync } from "node:fs";
import path from "node:path";

import { describe, expect, it } from "vitest";

import type { RoutingConfig, SurfaceConfig } from "@/lib/routing/config";
import { resolveRequest } from "@/lib/routing/resolve";

/**
 * `resolveRequest` in both topologies, over the shared routing fixtures with one
 * injected module surface. The surface is synthetic (`ledger`, not any shipped
 * module) so these cases do not move when a real module's host or path does.
 */

const FIXTURES = path.resolve(
  import.meta.dirname,
  "../../../../../tests/fixtures/routing",
);

const INJECTED: SurfaceConfig = { host: "ledger", path: "/ledger" };

function withModule(mode: "path" | "subdomain"): RoutingConfig {
  const config = JSON.parse(
    readFileSync(path.join(FIXTURES, `${mode}-mode.json`), "utf8"),
  ) as RoutingConfig;
  return { ...config, surfaces: { ...config.surfaces, modules: { ledger: INJECTED } } };
}

describe("resolveRequest, path mode", () => {
  const config = withModule("path");
  const resolve = (pathname: string) => resolveRequest(config, "example.test", pathname);

  it("serves the shell at the root", () => {
    expect(resolve("/")).toEqual({ kind: "shell" });
  });

  it("matches a module surface at its path and below it", () => {
    expect(resolve("/ledger")).toEqual({ kind: "module", surface: "ledger", subPath: "/" });
    expect(resolve("/ledger/")).toEqual({ kind: "module", surface: "ledger", subPath: "/" });
    expect(resolve("/ledger/search")).toEqual({
      kind: "module",
      surface: "ledger",
      subPath: "/search",
    });
    expect(resolve("/ledger/search/")).toEqual({
      kind: "module",
      surface: "ledger",
      subPath: "/search",
    });
  });

  it("matches only at a segment boundary", () => {
    expect(resolve("/ledger-extra")).toEqual({ kind: "not-found" });
    expect(resolve("/ledgers/search")).toEqual({ kind: "not-found" });
  });

  it("ignores the host", () => {
    expect(resolveRequest(config, "ledger.example.test", "/")).toEqual({ kind: "shell" });
    expect(resolveRequest(config, "elsewhere.test:3000", "/ledger")).toEqual({
      kind: "module",
      surface: "ledger",
      subPath: "/",
    });
  });

  it("serves nothing else", () => {
    expect(resolve("/unknown")).toEqual({ kind: "not-found" });
  });

  it("does not match a surface the routing config does not carry", () => {
    const bare = { ...config, surfaces: { ...config.surfaces, modules: {} } };
    expect(resolveRequest(bare, "example.test", "/ledger")).toEqual({ kind: "not-found" });
  });
});

describe("resolveRequest, subdomain mode", () => {
  const config = withModule("subdomain");

  it("serves the shell's root on the shell host, with or without a port", () => {
    expect(resolveRequest(config, "circuit.example.test", "/")).toEqual({ kind: "shell" });
    expect(resolveRequest(config, "circuit.example.test:3000", "/")).toEqual({
      kind: "shell",
    });
    expect(resolveRequest(config, "CIRCUIT.Example.Test", "/")).toEqual({ kind: "shell" });
  });

  it("gives a module host's whole path to the module, its root included", () => {
    expect(resolveRequest(config, "ledger.example.test", "/")).toEqual({
      kind: "module",
      surface: "ledger",
      subPath: "/",
    });
    expect(resolveRequest(config, "ledger.example.test:8443", "/search")).toEqual({
      kind: "module",
      surface: "ledger",
      subPath: "/search",
    });
  });

  it("does not read the module path prefix off the pathname", () => {
    expect(resolveRequest(config, "circuit.example.test", "/ledger")).toEqual({
      kind: "not-found",
    });
  });

  it("serves nothing on the shell host below its root, or on any other host", () => {
    expect(resolveRequest(config, "circuit.example.test", "/unknown")).toEqual({
      kind: "not-found",
    });
    expect(resolveRequest(config, "ledgerx.example.test", "/")).toEqual({ kind: "not-found" });
    expect(resolveRequest(config, "ledger.example.test.evil.test", "/")).toEqual({
      kind: "not-found",
    });
    expect(resolveRequest(config, "example.test", "/")).toEqual({ kind: "not-found" });
  });
});
