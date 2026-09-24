import { CoverageFigure } from "../../components/coverage";
import { GenericError, StateMessage } from "../../components/state-message";
import type { EntityItem } from "../../guards";
import type { BrowseState, DetailState, EntityRow, KindFilter, ListState } from "./load";
import { LIST_LIMIT } from "./load";
import styles from "./browse.module.css";

/**
 * Browse's view: synchronous, and a pure function of the loader's state. An entity
 * renders only the five fields the entity operations answer with.
 */

function memoryCount(count: number): string {
  return `${count} ${count === 1 ? "memory" : "memories"}`;
}

function KindFilters({ kinds }: { kinds: KindFilter[] }) {
  return (
    <nav aria-label="Filter entities by kind" className={styles.kinds}>
      <ul className={styles.kindList}>
        {kinds.map((kind) => (
          <li key={kind.label}>
            <a
              className={styles.kindLink}
              href={kind.href}
              aria-current={kind.current ? "page" : undefined}
            >
              {kind.label}
            </a>
          </li>
        ))}
      </ul>
    </nav>
  );
}

function EntityRows({ entities }: { entities: EntityRow[] }) {
  return (
    <ul className={styles.entityList}>
      {entities.map((entity) => (
        <li key={entity.ref}>
          <a
            className={styles.entityLink}
            href={entity.href}
            aria-current={entity.current ? "true" : undefined}
          >
            <span className={styles.entityName}>{entity.name}</span>
            <span className={styles.meta}>
              {entity.kind} · {memoryCount(entity.mentionCount)}
            </span>
          </a>
        </li>
      ))}
    </ul>
  );
}

function ListPane({ list, filtered }: { list: ListState; filtered: boolean }) {
  switch (list.state) {
    case "full":
      if (list.entities.length === 0) {
        return (
          <StateMessage name="no-entities">
            <p className={styles.stateText}>
              {filtered ? "No entities of this kind yet." : "No entities recorded yet."}
            </p>
          </StateMessage>
        );
      }
      return (
        <>
          {list.truncated ? (
            <p className={styles.notice} data-state="list-truncated">
              Showing the first {LIST_LIMIT} entities alphabetically. Filter by kind to
              narrow the list.
            </p>
          ) : null}
          <EntityRows entities={list.entities} />
        </>
      );
    case "reduced":
      return (
        <>
          <p className={styles.notice} data-state="list-reduced">
            The full entity list is too large to load at once — filter by kind.
          </p>
          <EntityRows entities={list.entities} />
        </>
      );
    case "list-too-large":
      return (
        <StateMessage name="list-too-large">
          <p className={styles.stateText}>
            The entity list is too large to load. Filter by kind to narrow it.
          </p>
        </StateMessage>
      );
    case "error":
      return <GenericError />;
  }
}

function EntityHeader({ entity }: { entity: EntityItem }) {
  return (
    <header className={styles.entityHeader}>
      <h2 className={styles.entityTitle}>{entity.name}</h2>
      <dl className={styles.facts}>
        <dt>Kind</dt>
        <dd>{entity.kind}</dd>
        <dt>Entity</dt>
        <dd>
          <code>{entity.ref}</code>
        </dd>
        {entity.backing_ref !== null ? (
          <>
            <dt>Backing record</dt>
            <dd>
              <code>{entity.backing_ref}</code>
            </dd>
          </>
        ) : null}
      </dl>
      <p className={styles.count}>{memoryCount(entity.mention_count)} you can read</p>
    </header>
  );
}

function DetailPane({ detail }: { detail: DetailState }) {
  switch (detail.state) {
    case "none":
      return (
        <StateMessage name="no-entity-selected">
          <p className={styles.stateText}>
            Choose an entity to see the memories that mention it.
          </p>
        </StateMessage>
      );
    case "window":
      return (
        <>
          <EntityHeader entity={detail.entity} />
          {detail.items.length === 0 ? (
            <StateMessage name="empty-window">
              <p className={styles.stateText}>
                No memories recorded around this entity yet.
              </p>
            </StateMessage>
          ) : (
            <ol className={styles.window}>
              {detail.items.map((item) => (
                <li key={item.ref} className={styles.windowItem}>
                  <a href={item.href}>{item.title}</a>
                  <span className={styles.meta}>
                    {item.kind} · <time dateTime={item.dateTime}>{item.when}</time>
                  </span>
                </li>
              ))}
            </ol>
          )}
          {detail.olderHref !== null || detail.newerHref !== null ? (
            <nav aria-label="Move through this entity's memories" className={styles.pager}>
              {detail.olderHref !== null ? <a href={detail.olderHref}>Older</a> : null}
              {detail.newerHref !== null ? <a href={detail.newerHref}>Newer</a> : null}
            </nav>
          ) : null}
        </>
      );
    case "entity-too-connected":
      return (
        <>
          {detail.entity !== null ? <EntityHeader entity={detail.entity} /> : null}
          <StateMessage name="entity-too-connected">
            <p className={styles.stateText}>
              This entity&apos;s memories link to too many records to show here.
            </p>
          </StateMessage>
        </>
      );
    case "entity-too-large":
      return (
        <>
          {detail.entity !== null ? <EntityHeader entity={detail.entity} /> : null}
          <StateMessage name="entity-too-large">
            <p className={styles.stateText}>
              This entity is mentioned by more memories than one view can scan. Use{" "}
              <a href={detail.searchHref}>Search</a> to find a specific memory.
            </p>
          </StateMessage>
        </>
      );
    case "entity-not-found":
      return (
        <StateMessage name="entity-not-found">
          <p className={styles.stateText}>This entity is unavailable.</p>
        </StateMessage>
      );
    case "error":
      return <GenericError />;
  }
}

export function BrowseView({ state }: { state: BrowseState }) {
  const filtered = state.kind !== null;
  return (
    <div className={styles.screen}>
      <h1 className={styles.title}>Memory</h1>
      <KindFilters kinds={state.kinds} />
      <div className={styles.panes}>
        <section aria-label="Entities" className={styles.listPane}>
          <ListPane list={state.list} filtered={filtered} />
        </section>
        <section aria-label="Selected entity" className={styles.detailPane}>
          <DetailPane detail={state.detail} />
        </section>
      </div>
      <CoverageFigure coverage={state.coverage} />
    </div>
  );
}
