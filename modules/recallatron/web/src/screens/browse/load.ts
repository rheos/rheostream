import type { ShellApi } from "@rheo-stream/web-contract/screen";

import { callChecked, type Called } from "../../call";
import { loadCoverage, type CoverageState } from "../../components/coverage";
import { formatUtc } from "../../format";
import {
  isEntityItem,
  isEntityList,
  isReadWindow,
  NOT_FOUND,
  REFERENCE_SCAN_LIMIT,
  WINDOW_SCAN_LIMIT,
  type EntityItem,
  type EntityList,
  type ReadWindow,
} from "../../guards";
import { ENTITY_GET, ENTITY_LIST, READ } from "../../operations";

/**
 * Browse: the entity list on the left, one entity's memories on the right. All state
 * is in the query string (`kind`, `entity`, `around`), so every selection is a link.
 */

/** The closed entity vocabulary, in the order the kind filter shows it. */
export const ENTITY_KINDS = [
  "person",
  "organization",
  "project",
  "topic",
  "place",
  "thing",
] as const;

/** `entity.list`'s maximum. There is no cursor, so the list is bounded rather than paged. */
export const LIST_LIMIT = 50;
/** The one retry after a reference-budget refusal. */
export const LIST_RETRY_LIMIT = 10;
/** `read`'s maximum context: a targetless window is the newest `2 * 10 + 1` members. */
export const READ_CONTEXT = 10;

export interface KindFilter {
  label: string;
  href: string;
  current: boolean;
}

export interface EntityRow {
  ref: string;
  kind: string;
  name: string;
  mentionCount: number;
  href: string;
  current: boolean;
}

export type ListState =
  | { state: "full"; entities: EntityRow[]; truncated: boolean }
  | { state: "reduced"; entities: EntityRow[] }
  | { state: "list-too-large" }
  | { state: "error" };

export interface WindowItem {
  ref: string;
  kind: string;
  title: string;
  when: string;
  dateTime: string;
  href: string;
}

export type DetailState =
  | { state: "none" }
  | {
      state: "window";
      entity: EntityItem;
      items: WindowItem[];
      olderHref: string | null;
      newerHref: string | null;
    }
  | { state: "entity-too-connected"; entity: EntityItem | null }
  | { state: "entity-too-large"; entity: EntityItem | null; searchHref: string }
  | { state: "entity-not-found" }
  | { state: "error" };

export interface BrowseState {
  /** The kind filter in force, or `null` for every kind. */
  kind: string | null;
  kinds: KindFilter[];
  list: ListState;
  detail: DetailState;
  coverage: CoverageState | null;
}

type Query = Readonly<Record<string, string | undefined>>;

function present(value: string | undefined): string | undefined {
  const trimmed = value?.trim();
  return trimmed ? trimmed : undefined;
}

async function listEntities(
  shell: ShellApi,
  kind: string | undefined,
  limit: number,
): Promise<Called<EntityList>> {
  const input = kind === undefined ? { limit } : { kind, limit };
  return callChecked(shell, ENTITY_LIST, input, isEntityList);
}

async function loadList(
  shell: ShellApi,
  kind: string | undefined,
  selected: string | undefined,
): Promise<ListState> {
  const rows = (list: EntityList): EntityRow[] =>
    list.items.map((item) => ({
      ref: item.ref,
      kind: item.kind,
      name: item.name,
      mentionCount: item.mention_count,
      href: shell.href("browse", { kind, entity: item.ref }),
      current: item.ref === selected,
    }));

  const first = await listEntities(shell, kind, LIST_LIMIT);
  if (first.state === "ok") {
    return {
      state: "full",
      entities: rows(first.value),
      truncated: first.value.items.length >= LIST_LIMIT,
    };
  }
  if (first.state !== "refused" || first.code !== REFERENCE_SCAN_LIMIT) {
    return { state: "error" };
  }
  const retry = await listEntities(shell, kind, LIST_RETRY_LIMIT);
  if (retry.state === "ok") {
    return { state: "reduced", entities: rows(retry.value) };
  }
  if (retry.state === "refused" && retry.code === REFERENCE_SCAN_LIMIT) {
    return { state: "list-too-large" };
  }
  return { state: "error" };
}

function windowItems(shell: ShellApi, window: ReadWindow): WindowItem[] {
  return window.items.map((item) => {
    const at = item.occurred_at ?? item.recorded_at;
    return {
      ref: item.ref,
      kind: item.kind,
      title: item.title,
      when: formatUtc(at),
      dateTime: at,
      href: shell.href("item", { ref: item.ref }),
    };
  });
}

async function loadDetail(
  shell: ShellApi,
  kind: string | undefined,
  entityRef: string | undefined,
  around: string | undefined,
): Promise<DetailState> {
  if (entityRef === undefined) {
    return { state: "none" };
  }
  // The entity check and the window are independent calls over the same container, so
  // they run together; the entity's answer still takes precedence below.
  const readInput =
    around === undefined
      ? { container_ref: entityRef, context: READ_CONTEXT }
      : { container_ref: entityRef, target_ref: around, context: READ_CONTEXT };
  const [entity, window] = await Promise.all([
    callChecked(shell, ENTITY_GET, { entity_ref: entityRef }, isEntityItem),
    callChecked(shell, READ, readInput, isReadWindow),
  ]);

  if (entity.state === "refused") {
    if (entity.code === NOT_FOUND) return { state: "entity-not-found" };
    if (entity.code === REFERENCE_SCAN_LIMIT) {
      return { state: "entity-too-connected", entity: null };
    }
    return { state: "error" };
  }
  if (entity.state !== "ok") {
    return { state: "error" };
  }

  if (window.state === "refused") {
    switch (window.code) {
      case REFERENCE_SCAN_LIMIT:
        return { state: "entity-too-connected", entity: entity.value };
      case WINDOW_SCAN_LIMIT:
        return {
          state: "entity-too-large",
          entity: entity.value,
          searchHref: shell.href("search"),
        };
      case NOT_FOUND:
        return { state: "entity-not-found" };
      default:
        return { state: "error" };
    }
  }
  if (window.state !== "ok") {
    return { state: "error" };
  }

  const items = windowItems(shell, window.value);
  const first = items[0];
  const last = items[items.length - 1];
  const recentre = (ref: string) =>
    shell.href("browse", { kind, entity: entityRef, around: ref });
  return {
    state: "window",
    entity: entity.value,
    items,
    olderHref: first !== undefined && window.value.window_start > 0 ? recentre(first.ref) : null,
    newerHref:
      last !== undefined && window.value.window_end < window.value.total
        ? recentre(last.ref)
        : null,
  };
}

function kindFilters(
  shell: ShellApi,
  kind: string | undefined,
  selected: string | undefined,
): KindFilter[] {
  const all: KindFilter = {
    label: "All kinds",
    href: shell.href("browse", { entity: selected }),
    current: kind === undefined,
  };
  return [
    all,
    ...ENTITY_KINDS.map((each) => ({
      label: each,
      href: shell.href("browse", { kind: each, entity: selected }),
      current: kind === each,
    })),
  ];
}

export async function loadBrowse(shell: ShellApi, query: Query): Promise<BrowseState> {
  const kind = present(query.kind);
  const entityRef = present(query.entity);
  const around = present(query.around);
  const [list, detail, coverage] = await Promise.all([
    loadList(shell, kind, entityRef),
    loadDetail(shell, kind, entityRef, around),
    loadCoverage(shell),
  ]);
  return {
    kind: kind ?? null,
    kinds: kindFilters(shell, kind, entityRef),
    list,
    detail,
    coverage,
  };
}
