import type { ReactNode } from "react";

import type { ScreenProps, ShellApi } from "@rheo-stream/web-contract/screen";

import { callChecked } from "../../call";
import { GenericError, StateMessage } from "../../components/state-message";
import { formatUtc } from "../../format";
import { isHistoryItem, NOT_FOUND, type HistoryItem } from "../../guards";
import { HISTORY_GET } from "../../operations";
import styles from "./screen.module.css";

type HistoryItemState =
  | { state: "item"; item: HistoryItem; backHref: string }
  | { state: "not-found" | "error"; backHref: string };

export async function loadHistoryItem(
  shell: ShellApi,
  query: Readonly<Record<string, string | undefined>>,
): Promise<HistoryItemState> {
  const backHref = shell.href("search");
  const id = query.id?.trim();
  if (shell.role !== "owner" || !id) return { state: "not-found", backHref };
  const called = await callChecked(shell, HISTORY_GET, { id }, isHistoryItem);
  if (called.state === "refused" && called.code === NOT_FOUND) {
    return { state: "not-found", backHref };
  }
  if (called.state !== "ok") return { state: "error", backHref };
  return { state: "item", item: called.value, backHref };
}

export function HistoryItemView({ state }: { state: HistoryItemState }) {
  if (state.state !== "item") {
    return (
      <div className={styles.screen}>
        <h1>Historical record</h1>
        {state.state === "not-found" ? (
          <StateMessage name="history-not-found">
            <p>This historical record is unavailable.</p>
          </StateMessage>
        ) : (
          <GenericError />
        )}
        <a href={state.backHref}>Back to search</a>
      </div>
    );
  }
  const item = state.item;
  return (
    <article className={styles.screen}>
      <a href={state.backHref}>Back to search</a>
      <h1>{item.title}</h1>
      <p className={styles.warning}>
        Historical source evidence · {item.status}. This record is not an accepted memory.
      </p>
      <dl className={styles.facts}>
        <dt>Kind</dt>
        <dd>{item.kind.replaceAll("_", " ")}</dd>
        <dt>Occurred</dt>
        <dd>
          <time dateTime={item.occurred_at}>{formatUtc(item.occurred_at)}</time>
        </dd>
        {item.source_role === null ? null : (
          <>
            <dt>Speaker</dt>
            <dd>{item.source_role}</dd>
          </>
        )}
        {item.source_category === null ? null : (
          <>
            <dt>Category</dt>
            <dd>{item.source_category}</dd>
          </>
        )}
        <dt>Source</dt>
        <dd>
          <code>{item.source_namespace}:{item.external_source_key}</code>
        </dd>
      </dl>
      <div className={styles.body}>{item.body}</div>
    </article>
  );
}

export async function historyItem(props: ScreenProps): Promise<ReactNode> {
  const state = await loadHistoryItem(props.shell, props.query);
  return <HistoryItemView state={state} />;
}
