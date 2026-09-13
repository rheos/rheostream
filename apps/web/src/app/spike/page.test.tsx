import { renderToStaticMarkup } from "react-dom/server";
import { isValidElement, type ReactElement, type ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { SpikeNotes } from "@/components/spike-notes";
import { WorkspaceSwitcher } from "@/components/workspace-switcher";
import type { RoutingConfig, SurfaceConfig } from "@/lib/routing/config";
import type { SessionResult } from "@/lib/session";

/**
 * The spike page's own shape (0v, C4) — the first `.tsx` test in this repository.
 *
 * A server component needs a different approach from the route-handler tests
 * beside it: `Page` reads `cookies()` and `headers()` directly, which throw
 * outside a real request, so `next/headers` is mocked; `@/lib/session`'s
 * `fetchSession` is mocked to a fixed session; and the routing configuration is
 * an ad-hoc object literal carrying a `surfaces.modules.spike` entry, because the
 * shared `path-mode.json` / `subdomain-mode.json` fixtures carry no module
 * surfaces at all.
 *
 * `Page` is an ordinary async function, so it is called directly and the returned
 * element tree is **walked** rather than rendered. Walking is deliberate, not
 * convenience: the tree contains a `Suspense` boundary around an async child, and
 * `renderToStaticMarkup` is the synchronous legacy renderer — it cannot resolve
 * one. Creating that element does not call the component, so the note read never
 * runs here; the two synchronous pieces that *can* be rendered to markup
 * (`SpikeNotes`' two states) are rendered below.
 *
 * The mutant this file is the home for: **rendering the shipped
 * `WorkspaceSwitcher` instead of the spike's own form**. That component is
 * imported here by identity so the check is not a text match on a name that could
 * drift.
 */

const fetchSession = vi.hoisted(() => vi.fn());
vi.mock("@/lib/session", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/session")>();
  return { ...actual, fetchSession };
});

const cookieValue = vi.hoisted(() => ({ current: "abc123" as string | undefined }));
vi.mock("next/headers", () => ({
  cookies: async () => ({
    get: (name: string) =>
      name === "rheo_session" && cookieValue.current !== undefined
        ? { name, value: cookieValue.current }
        : undefined,
  }),
  headers: async () => new Headers({ host: "localhost:3000" }),
}));

const SURFACE: SurfaceConfig = { host: "circuit" };
const CONFIG: RoutingConfig = {
  mode: "path",
  scheme: "http",
  base_host: "localhost",
  surfaces: {
    shell: { host: "circuit", path: "/" },
    identity: { host: "auth", path: "/auth", fixed_path: true },
    api: SURFACE,
    mcp: SURFACE,
    docs: SURFACE,
    integration: SURFACE,
    modules: { spike: { host: "spike", path: "/spike" } },
  },
};

const WORKSPACE_A = "11111111-1111-4111-8111-111111111111";
const WORKSPACE_B = "22222222-2222-4222-8222-222222222222";

const SIGNED_IN: SessionResult = {
  state: "ok",
  actor: { kind: "account", id: "account-1" },
  activeWorkspaceId: WORKSPACE_A,
  role: "owner",
  memberships: [
    { workspace_id: WORKSPACE_A, role: "owner" },
    { workspace_id: WORKSPACE_B, role: "owner" },
  ],
};

interface Walked {
  types: unknown[];
  text: string[];
  props: Record<string, unknown>[];
}

/**
 * Every element type, every prop bag and every string in a returned tree.
 *
 * **Synchronous function components are called**, so a page composed of small
 * server components is walked as the reader sees it rather than stopping at the
 * first `<SessionBody />`. An **async** component is not: it returns a promise,
 * which this records and steps over. That is exactly the `NoteList` behind the
 * `Suspense` boundary, so the note read never runs in this file — which is also
 * why no mock for `@/lib/spike/client` is needed here.
 */
function walk(node: ReactNode, seen: Walked = { types: [], text: [], props: [] }): Walked {
  if (node === null || node === undefined || typeof node === "boolean") {
    return seen;
  }
  if (typeof node === "string" || typeof node === "number") {
    seen.text.push(String(node));
    return seen;
  }
  if (Array.isArray(node)) {
    for (const child of node) {
      walk(child, seen);
    }
    return seen;
  }
  if (!isValidElement(node)) {
    return seen;
  }
  const element = node as ReactElement<{ children?: ReactNode } & Record<string, unknown>>;
  seen.types.push(element.type);
  seen.props.push(element.props);
  if (typeof element.type === "function") {
    const rendered: unknown = (
      element.type as (props: Record<string, unknown>) => ReactNode
    )(element.props);
    if (!(rendered instanceof Promise)) {
      walk(rendered as ReactNode, seen);
      return seen;
    }
    // An async server component. Recorded above, deliberately not awaited.
    return seen;
  }
  walk(element.props.children, seen);
  return seen;
}

async function renderPage(options: {
  session?: SessionResult;
  searchParams?: Record<string, string | string[] | undefined>;
} = {}) {
  vi.resetModules();
  vi.stubGlobal(
    "fetch",
    vi.fn(
      async () =>
        new Response(JSON.stringify(CONFIG), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
    ),
  );
  fetchSession.mockResolvedValue(options.session ?? SIGNED_IN);
  const Page = (await import("@/app/spike/page")).default;
  const tree = await Page({ searchParams: Promise.resolve(options.searchParams ?? {}) });
  return walk(tree);
}

/** Every `<form>`'s action in the walked tree. */
function formActions(seen: Walked): string[] {
  return seen.props
    .filter((props) => props.method === "post" && typeof props.action === "string")
    .map((props) => props.action as string);
}

beforeEach(() => {
  vi.stubEnv("RHEO_CORE_INTERNAL_API_URL", "http://127.0.0.1:8100");
  vi.stubEnv("RHEO_INTERNAL_SECRET", "internal-secret");
  vi.stubEnv("RHEO_SPIKE_WORKSPACE_IDS", `${WORKSPACE_A},${WORKSPACE_B}`);
  cookieValue.current = "abc123";
  fetchSession.mockReset();
});

afterEach(() => {
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
});

describe("the spike page", () => {
  it("never renders the shipped workspace switcher", async () => {
    const seen = await renderPage();

    // F16: `WorkspaceSwitcher` is "use client" and fetches the relative
    // `switcherAction`, which in this topology resolves to Next.js on :3000
    // where no /auth/* route exists. It is inert under the curl driver and 404s
    // in a real browser, so the spike ships its own plain form instead.
    expect(seen.types).not.toContain(WorkspaceSwitcher);
    expect(seen.types.filter((type) => typeof type === "function")).not.toContain(
      WorkspaceSwitcher,
    );
  });

  it("renders its own plain form posts to the spike's own route handlers", async () => {
    const seen = await renderPage();

    // Relative, and built by `spikePath` — never an absolute `urlFor` URL, which
    // under the driver's port-free base_host would point at port 80.
    expect(formActions(seen)).toEqual(["/spike/workspace", "/spike/add"]);
    for (const action of formActions(seen)) {
      expect(action).not.toMatch(/^https?:/);
    }
  });

  it("offers every membership plus a free-text fallback once signed in", async () => {
    const seen = await renderPage();

    const values = seen.props
      .filter((props) => props.name === "target_workspace_id")
      .map((props) => props.value);
    expect(values).toContain(WORKSPACE_A);
    expect(values).toContain(WORKSPACE_B);
    // The free-text fallback carries no value of its own.
    expect(values).toContain(undefined);
  });

  it("renders the first-run form from the driver's seeded ids (D-CF1)", async () => {
    const seen = await renderPage({
      session: { state: "unauthenticated", refusal: "workspace_unselected" },
    });

    expect(seen.text.join(" ")).toContain("no workspace selected");
    expect(formActions(seen)).toEqual(["/spike/workspace"]);
    const values = seen.props
      .filter((props) => props.name === "target_workspace_id")
      .map((props) => props.value);
    expect(values).toContain(WORKSPACE_A);
    expect(values).toContain(WORKSPACE_B);
    expect(seen.types).not.toContain(WorkspaceSwitcher);
  });

  it("renders no workspace form for any other refusal", async () => {
    const seen = await renderPage({
      session: { state: "unauthenticated", refusal: "session_missing" },
    });

    expect(seen.text.join(" ")).toContain("session_missing");
    expect(formActions(seen)).toEqual([]);
  });

  it("renders the loading state as the streamed fallback around the note read", async () => {
    const seen = await renderPage();

    const fallbacks = seen.props
      .filter((props) => props.fallback !== undefined)
      .map((props) => walk(props.fallback as ReactNode).text.join(""));
    expect(fallbacks.some((text) => text.includes("loading"))).toBe(true);
  });

  it.each([
    ["add", "input_invalid"],
    ["workspace", "not_a_member"],
  ])("renders the error state when %s names a refusal", async (key, state) => {
    const seen = await renderPage({ searchParams: { [key]: state } });

    const text = seen.text.join(" ");
    expect(text).toContain("refused");
    expect(text).toContain(state);
  });

  it("renders no error state for a successful outcome", async () => {
    const seen = await renderPage({
      searchParams: { add: "succeeded", workspace: "selected" },
    });

    expect(seen.text.join(" ")).not.toContain("refused");
  });

  it("says routing is unavailable rather than guessing its own path", async () => {
    vi.resetModules();
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("ECONNREFUSED")));
    fetchSession.mockResolvedValue(SIGNED_IN);
    const Page = (await import("@/app/spike/page")).default;

    const seen = walk(await Page({ searchParams: Promise.resolve({}) }));

    expect(seen.text.join(" ")).toContain("routing: unavailable");
    expect(formActions(seen)).toEqual([]);
  });
});

/**
 * The two synchronous states, rendered to real markup.
 *
 * `SpikeNotes` has no async child, so the legacy renderer handles it — and an
 * empty workspace answering `{"notes": []}` is a **success**, which is why the
 * empty state is a rendered branch and not an error.
 */
describe("the spike's note list", () => {
  it("renders the empty state for a workspace with no notes", () => {
    expect(renderToStaticMarkup(<SpikeNotes notes={[]} />)).toContain("none yet");
  });

  it("renders the populated state", () => {
    const markup = renderToStaticMarkup(
      <SpikeNotes
        notes={[
          {
            ref: "spike:note:1",
            body: "the first note",
            created_at: "2026-09-13T00:00:00Z",
            revision: 1,
          },
        ]}
      />,
    );

    expect(markup).toContain("the first note");
    expect(markup).toContain("spike:note:1");
  });
});
