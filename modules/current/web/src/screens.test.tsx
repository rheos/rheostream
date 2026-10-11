import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import type {
  OperationOutcome,
  ShellApi,
} from "@rheo-stream/web-contract/screen";
import { screens } from "./index";

const ok = (result: unknown): OperationOutcome => ({ state: "ok", result });
const ITEM = "current.item:01900000-0000-7000-8000-000000000001";
const OTHER = "current.item:01900000-0000-7000-8000-000000000002";
const PROJECT = "current.project:01900000-0000-7000-8000-000000000003";

function item(overrides: Record<string, unknown> = {}) {
  return {
    ref: ITEM,
    title: "Renew the example.com domain",
    status: "open",
    priority: "high",
    area: "bills",
    project_ref: PROJECT,
    due_on: "2026-01-05",
    snoozed_until: null,
    woke: false,
    overdue: true,
    blocked: true,
    event_count: 1,
    revision: 3,
    updated_at: "2026-01-02T10:00:00Z",
    ...overrides,
  };
}
const areaList = ok({
  items: [
    { key: "bills", label: "Bills", position: 1, retired: false },
    { key: "garden", label: "Garden", position: 2, retired: true },
  ],
});
const projectList = ok({
  items: [
    {
      ref: PROJECT,
      title: "Studio move",
      description: "",
      status: "active",
      area: null,
      due_on: null,
      revision: 1,
      created_at: "2026-01-01T00:00:00Z",
      updated_at: "2026-01-01T00:00:00Z",
      item_counts: { open: 2, waiting: 1, snoozed: 0, done: 4 },
    },
  ],
});
const summaryResult = ok({
  by_status: { open: 4, waiting: 1, snoozed: 2, done: 9 },
  active: 6,
  woke: 1,
  overdue: 2,
  blocked: 1,
  by_area: [{ area: "bills", active: 3 }],
});

function shell(
  answer: (name: string, input: Record<string, unknown>) => OperationOutcome | undefined,
  role = "owner",
) {
  const call = vi.fn(
    async (name: string, input: unknown) =>
      answer(name, (input ?? {}) as Record<string, unknown>) ??
      ({ state: "unavailable" } as OperationOutcome),
  );
  const submit = vi.fn(async () => ok({}));
  return {
    role,
    call,
    submit,
    href: (id: string, query?: Record<string, string | undefined>) =>
      `/current/${id}?${new URLSearchParams(
        Object.entries(query ?? {}).filter(
          (entry): entry is [string, string] => entry[1] !== undefined,
        ),
      )}`,
  } satisfies ShellApi;
}
function standard(list: (input: Record<string, unknown>) => OperationOutcome) {
  return (name: string, input: Record<string, unknown>) =>
    ({
      "current.summary.get": summaryResult,
      "current.area.list": areaList,
      "current.project.list": projectList,
      "current.item.list": list(input),
    })[name];
}

describe("queue", () => {
  it("separates woke items, marks overdue and blocked, and only reads", async () => {
    const api = shell(
      standard((input) =>
        input.woke === true
          ? ok({
              items: [
                item({
                  ref: OTHER,
                  title: "Call the landlord back",
                  status: "snoozed",
                  snoozed_until: "2026-01-01T09:00:00Z",
                  woke: true,
                  overdue: false,
                  blocked: false,
                }),
              ],
              next_offset: null,
            })
          : ok({ items: [item()], next_offset: 25 }),
      ),
    );
    const html = renderToStaticMarkup(await screens.queue({ shell: api, query: {} }));
    expect(html).toMatch(/<h2 id="woke">Woke up<\/h2>[\s\S]*Call the landlord back[\s\S]*In motion[\s\S]*Renew the example.com domain/);
    expect(html).toContain("Overdue");
    expect(html).toContain("Blocked");
    expect(html).toContain("Studio move");
    expect(html).toContain("Bills");
    expect(html).toContain("High");
    // The summary counts head the queue.
    expect(html).toMatch(/<span[^>]*>Active<\/span> <span[^>]*>6<\/span>/);
    // Paging follows next_offset.
    expect(html).toContain("offset=25");
    // Quick actions per row: Done, Snooze, Wait for an open item.
    expect(html).toContain('aria-label="Done: Renew the example.com domain"');
    expect(html).toContain('aria-label="Wait: Renew the example.com domain"');
    expect(html).toContain('aria-label="Snooze: Renew the example.com domain"');
    // A woke item can be snoozed again rather than snoozed from scratch.
    expect(html).toContain('aria-label="Snooze again: Call the landlord back"');
    expect(api.submit).not.toHaveBeenCalled();
    expect(new Set(api.call.mock.calls.map(([name]) => name))).toEqual(
      new Set([
        "current.summary.get",
        "current.area.list",
        "current.project.list",
        "current.item.list",
      ]),
    );
  });
  it("passes filters through and skips the woke split outside the active list", async () => {
    const api = shell(standard(() => ok({ items: [], next_offset: null })));
    const html = renderToStaticMarkup(
      await screens.queue({
        shell: api,
        query: { status: "done", area: "bills", overdue: "1", q: "domain" },
      }),
    );
    const lists = api.call.mock.calls.filter(([name]) => name === "current.item.list");
    expect(lists).toHaveLength(1);
    expect(lists[0]?.[1]).toMatchObject({
      status: "done",
      area: "bills",
      overdue: true,
      query: "domain",
      woke: null,
      offset: 0,
    });
    expect(html).toContain("No items match");
    expect(html).toContain("Clear filters");
  });
  it("shows an empty state that says how to start", async () => {
    const html = renderToStaticMarkup(
      await screens.queue({
        shell: shell(standard(() => ok({ items: [], next_offset: null }))),
        query: {},
      }),
    );
    expect(html).toContain("Nothing in motion");
    expect(html).toContain("/current/new");
  });
  it("shows a failure rather than an empty queue when the list cannot load", async () => {
    const html = renderToStaticMarkup(
      await screens.queue({
        shell: shell(standard(() => ({ state: "unavailable" }))),
        query: {},
      }),
    );
    expect(html).toContain("Could not load this view");
    expect(html).not.toContain("Nothing in motion");
  });
  it("offers no actions where the host gives no submit", async () => {
    const api = shell(standard(() => ok({ items: [item()], next_offset: null })));
    const readOnly: ShellApi = { role: api.role, call: api.call, href: api.href };
    const html = renderToStaticMarkup(await screens.queue({ shell: readOnly, query: {} }));
    expect(html).toContain("Renew the example.com domain");
    expect(html).not.toContain("<button type=\"button\"");
    expect(html).not.toContain("New item");
  });
});

function detail(overrides: Record<string, unknown> = {}) {
  return ok({
    ...item(),
    body: "Registrar sends the notice to <billing@example.com>.",
    provenance: "email",
    source_kind: "email.thread",
    source_ref: "thread-1",
    event_count: 3,
    created_at: "2026-01-01T08:00:00Z",
    closed_at: null,
    project: { ref: PROJECT, title: "Studio move", status: "active" },
    blockers: [{ ref: OTHER, title: "Find the registrar login", status: "open" }],
    blocking: [],
    comments: [
      {
        id: "c1",
        kind: "comment",
        body: "Asked the registrar for the invoice.",
        author_kind: "token",
        author_id: "01a0f5ed-b156-75bf-9e13-dabb96bde0dd",
        created_at: "2026-01-02T09:00:00Z",
      },
      {
        id: "c2",
        kind: "system",
        body: "Grouped a repeat email.thread signal (event 3).",
        author_kind: "system",
        author_id: null,
        created_at: "2026-01-02T09:30:00Z",
      },
    ],
    ...overrides,
  });
}
function detailShell(record: OperationOutcome, role = "owner") {
  return shell(
    (name) =>
      ({
        "current.item.get": record,
        "current.area.list": areaList,
        "current.project.list": projectList,
        "current.item.list": ok({
          items: [
            item({ ref: OTHER, title: "Find the registrar login" }),
            item({
              ref: "current.item:01900000-0000-7000-8000-000000000009",
              title: "Pay the hosting bill",
            }),
          ],
          next_offset: null,
        }),
      })[name],
    role,
  );
}

describe("item detail", () => {
  it("renders fields, escaped notes, comment authors from the record and dependencies", async () => {
    const api = detailShell(detail());
    const html = renderToStaticMarkup(
      await screens.item({ shell: api, query: { ref: ITEM } }),
    );
    expect(html).toContain("&lt;billing@example.com&gt;");
    expect(html).toContain("Agent 01a0f5ed");
    expect(html).toContain("System");
    expect(html).toContain("From email");
    expect(html).toContain("email.thread: thread-1");
    expect(html).toContain("Find the registrar login");
    expect(html).toContain("Remove the link to Find the registrar login");
    // An existing blocker is not offered again; another active item is.
    expect(html).toMatch(/<option value="current\.item:[^"]*09">Pay the hosting bill<\/option>/);
    expect(html).toContain('<option value="" selected="">Choose an item</option>');
    expect(html).not.toMatch(/<option value="current\.item:[^"]*02">/);
    expect(html).toContain("Still blocked. You can complete it anyway.");
    expect(html).toContain("Edit details");
    // The retired area stays readable but is not offered for new choices.
    expect(html).not.toContain("Garden (retired)");
    expect(api.submit).not.toHaveBeenCalled();
  });
  it("offers only reopen for a done item", async () => {
    const html = renderToStaticMarkup(
      await screens.item({
        shell: detailShell(detail({ status: "done", closed_at: "2026-01-03T00:00:00Z" })),
        query: { ref: ITEM },
      }),
    );
    expect(html).toContain(">Reopen</button>");
    expect(html).not.toContain(">Wait</button>");
    expect(html).not.toContain(">Snooze</button>");
    expect(html).not.toContain(">Done</button>");
  });
  it("offers back to open and no repeat wait for a waiting item", async () => {
    const html = renderToStaticMarkup(
      await screens.item({
        shell: detailShell(detail({ status: "waiting" })),
        query: { ref: ITEM },
      }),
    );
    expect(html).toContain(">Back to open</button>");
    expect(html).not.toContain(">Wait</button>");
    expect(html).toContain(">Snooze</button>");
    expect(html).toContain(">Done</button>");
  });
  it("keeps a retired area selectable on the item that already holds it", async () => {
    const html = renderToStaticMarkup(
      await screens.item({
        shell: detailShell(detail({ area: "garden" })),
        query: { ref: ITEM },
      }),
    );
    expect(html).toContain('<option value="garden" selected="">Garden (retired)</option>');
  });
  it("says the record is unavailable when it was deleted", async () => {
    const html = renderToStaticMarkup(
      await screens.item({
        shell: detailShell({ state: "refused", code: "not_found", text: "item unavailable" }),
        query: { ref: ITEM },
      }),
    );
    expect(html).toContain("Record unavailable");
  });
  it("asks for an item without calling anything when none is named", async () => {
    const api = detailShell(detail());
    const html = renderToStaticMarkup(await screens.item({ shell: api, query: {} }));
    expect(html).toContain("No item selected");
    expect(api.call).not.toHaveBeenCalled();
  });
});

describe("new item, projects and areas", () => {
  it("prefills the project when the new-item link names one", async () => {
    const html = renderToStaticMarkup(
      await screens.newItem({
        shell: shell(standard(() => ok({ items: [] }))),
        query: { project: PROJECT },
      }),
    );
    expect(html).toContain(`<option value="${PROJECT}" selected="">Studio move</option>`);
    expect(html).toContain("Add item");
  });
  it("lists projects with item counts by status", async () => {
    const html = renderToStaticMarkup(
      await screens.projects({ shell: shell(standard(() => ok({ items: [] }))), query: {} }),
    );
    expect(html).toContain("Studio move");
    expect(html).toContain("2 open, 1 waiting, 4 done");
    expect(html).toContain("Create project");
  });
  it("shows a project's items with their quick actions", async () => {
    const api = shell((name) =>
      ({
        "current.project.get": ok((projectList as { result: { items: unknown[] } }).result.items[0]),
        "current.area.list": areaList,
        "current.item.list": ok({ items: [item()], next_offset: null }),
      })[name],
    );
    const html = renderToStaticMarkup(
      await screens.project({ shell: api, query: { ref: PROJECT } }),
    );
    expect(html).toContain("Renew the example.com domain");
    expect(html).toContain(`/current/new?project=${encodeURIComponent(PROJECT)}`);
    const list = api.call.mock.calls.find(([name]) => name === "current.item.list");
    expect(list?.[1]).toMatchObject({ project_ref: PROJECT, status: "active" });
  });
  it("refuses area management to members without reading anything", async () => {
    const api = shell(standard(() => ok({ items: [] })), "member");
    const html = renderToStaticMarkup(await screens.areas({ shell: api, query: {} }));
    expect(html).toContain("Only workspace owners manage areas");
    expect(api.call).not.toHaveBeenCalled();
  });
  it("hides the Areas tab from members", async () => {
    const html = renderToStaticMarkup(
      await screens.queue({
        shell: shell(standard(() => ok({ items: [] })), "member"),
        query: {},
      }),
    );
    expect(html).not.toContain("/current/areas");
  });
  it("lets the owner rename, retire and restore areas", async () => {
    const html = renderToStaticMarkup(
      await screens.areas({ shell: shell(standard(() => ok({ items: [] }))), query: {} }),
    );
    expect(html).toContain('aria-label="Retire Bills"');
    expect(html).toContain('aria-label="Restore Garden"');
    expect(html).toContain("3 active items");
    expect(html).toContain("Add area");
  });
});
