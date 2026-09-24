import type { ShellApi } from "@rheo-stream/web-contract/screen";

import { callChecked } from "../../call";
import { formatUtc } from "../../format";
import { isMemoryItem, NOT_FOUND } from "../../guards";
import { GET } from "../../operations";

/** Item: one memory by its reference, `?ref=`. */

export interface Stamp {
  text: string;
  dateTime: string;
}

export interface ItemLink {
  ref: string;
  relation: string;
  label: string;
}

export interface ItemDetail {
  ref: string;
  kind: string;
  title: string;
  body: string;
  occurred: Stamp | null;
  recorded: Stamp;
  revision: number;
  invalidated: Stamp | null;
  invalidationReason: string | null;
  successor: { ref: string; href: string } | null;
  links: ItemLink[];
}

export type ItemState =
  | { state: "item"; item: ItemDetail }
  | { state: "not-found" }
  | { state: "error" };

type Query = Readonly<Record<string, string | undefined>>;

function stamp(iso: string): Stamp {
  return { text: formatUtc(iso), dateTime: iso };
}

export async function loadItem(shell: ShellApi, query: Query): Promise<ItemState> {
  const ref = query.ref?.trim();
  if (!ref) {
    // Nothing to look up is the same answer as a reference that resolves to nothing.
    return { state: "not-found" };
  }
  const called = await callChecked(shell, GET, { ref }, isMemoryItem);
  if (called.state === "refused" && called.code === NOT_FOUND) {
    return { state: "not-found" };
  }
  if (called.state !== "ok") {
    return { state: "error" };
  }
  const memory = called.value;
  return {
    state: "item",
    item: {
      ref: memory.ref,
      kind: memory.kind,
      title: memory.title,
      body: memory.body,
      occurred: memory.occurred_at === null ? null : stamp(memory.occurred_at),
      recorded: stamp(memory.recorded_at),
      revision: memory.revision,
      invalidated: memory.invalidated_at === null ? null : stamp(memory.invalidated_at),
      invalidationReason: memory.invalidation_reason,
      successor:
        memory.superseded_by === null
          ? null
          : {
              ref: memory.superseded_by,
              href: shell.href("item", { ref: memory.superseded_by }),
            },
      links: memory.links.map((link) => ({
        ref: link.ref,
        relation: link.relation,
        label: link.display ?? link.ref,
      })),
    },
  };
}
