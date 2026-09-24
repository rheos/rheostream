import type { ReactNode } from "react";

import { GenericError, StateMessage } from "../../components/state-message";
import type { ItemDetail, ItemState, Stamp } from "./load";
import styles from "./item.module.css";

/** Item's view: synchronous, a pure function of the loader's state. */

function Time({ stamp }: { stamp: Stamp }) {
  return <time dateTime={stamp.dateTime}>{stamp.text}</time>;
}

function Memory({ item }: { item: ItemDetail }) {
  return (
    <article className={styles.article}>
      <h1 className={styles.title}>{item.title}</h1>
      <p className={styles.body}>{item.body}</p>
      <dl className={styles.facts}>
        <dt>Kind</dt>
        <dd>{item.kind}</dd>
        {item.occurred !== null ? (
          <>
            <dt>Occurred</dt>
            <dd>
              <Time stamp={item.occurred} />
            </dd>
          </>
        ) : null}
        <dt>Recorded</dt>
        <dd>
          <Time stamp={item.recorded} />
        </dd>
        <dt>Revision</dt>
        <dd>{item.revision}</dd>
        <dt>Reference</dt>
        <dd>
          <code>{item.ref}</code>
        </dd>
        {item.invalidated !== null ? (
          <>
            <dt>Invalidated</dt>
            <dd>
              <Time stamp={item.invalidated} />
            </dd>
          </>
        ) : null}
        {item.invalidationReason !== null ? (
          <>
            <dt>Invalidation reason</dt>
            <dd>{item.invalidationReason}</dd>
          </>
        ) : null}
        {item.successor !== null ? (
          <>
            <dt>Replaced by</dt>
            <dd>
              <a href={item.successor.href}>
                <code>{item.successor.ref}</code>
              </a>
            </dd>
          </>
        ) : null}
      </dl>
      {item.links.length > 0 ? (
        <section aria-label="Links" className={styles.links}>
          <h2 className={styles.linksTitle}>Links</h2>
          <ul className={styles.linkList}>
            {item.links.map((link) => (
              <li key={`${link.relation}:${link.ref}`}>
                <span className={styles.relation}>{link.relation}</span> {link.label}
              </li>
            ))}
          </ul>
        </section>
      ) : null}
    </article>
  );
}

/** A page with no memory to show still has a heading, so it has an outline. */
function Unavailable({ children }: { children: ReactNode }) {
  return (
    <div className={styles.state}>
      <h1 className={styles.stateTitle}>Memory</h1>
      {children}
    </div>
  );
}

export function ItemView({ state }: { state: ItemState }) {
  switch (state.state) {
    case "item":
      return <Memory item={state.item} />;
    case "not-found":
      return (
        <Unavailable>
          <StateMessage name="memory-not-found">
            <p className={styles.stateText}>This memory is unavailable.</p>
          </StateMessage>
        </Unavailable>
      );
    case "error":
      return (
        <Unavailable>
          <GenericError />
        </Unavailable>
      );
  }
}
