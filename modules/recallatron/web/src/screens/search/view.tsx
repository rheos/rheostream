import { CoverageFigure } from "../../components/coverage";
import { GenericError, StateMessage } from "../../components/state-message";
import type { HistoryState, Provenance, ResultsState, SearchState } from "./load";
import { EMPTY_COPY, QUERY_MAX_LENGTH } from "./load";
import styles from "./search.module.css";

/** Search's view: synchronous, a pure function of the loader's state. */

function ProvenanceLine({ provenance }: { provenance: Provenance }) {
  return (
    <p className={styles.provenance} data-provenance="">
      Strategy: <code>{provenance.strategy}</code>. Meaning search available:{" "}
      {provenance.denseAvailable ? "yes" : "no"}.
    </p>
  );
}

function Results({ results }: { results: ResultsState }) {
  switch (results.state) {
    case "prompt":
      return (
        <StateMessage name="prompt">
          <p className={styles.stateText}>
            Search your memories by words, or by meaning where meaning search is on.
          </p>
        </StateMessage>
      );
    case "too-long":
      return (
        <StateMessage name="too-long" tone="error">
          <p className={styles.stateText}>
            A search can be at most {QUERY_MAX_LENGTH} characters. Shorten it and try again.
          </p>
        </StateMessage>
      );
    case "empty":
      return (
        <>
          <ProvenanceLine provenance={results.provenance} />
          <StateMessage name={`empty-${results.empty}`}>
            <p className={styles.stateText}>{EMPTY_COPY[results.empty]}</p>
          </StateMessage>
        </>
      );
    case "results":
      return (
        <>
          <ProvenanceLine provenance={results.provenance} />
          <ol className={styles.results}>
            {results.rows.map((row) => (
              <li key={row.ref} className={styles.result}>
                <a className={styles.resultTitle} href={row.href}>
                  {row.title}
                </a>
                <span className={styles.meta}>
                  {row.kind} · <time dateTime={row.dateTime}>{row.when}</time> ·{" "}
                  <code>{row.ref}</code>
                </span>
                <p className={styles.excerpt}>{row.excerpt}</p>
              </li>
            ))}
          </ol>
        </>
      );
    case "error":
      return <GenericError />;
  }
}

function HistoryResults({ history }: { history: HistoryState }) {
  if (history.state === "hidden") return null;
  return (
    <section aria-label="Historical records" className={styles.section}>
      <h2 className={styles.subheading}>Historical records</h2>
      <p className={styles.historyNote}>
        A historical record is source evidence, not an accepted memory.
      </p>
      {history.state === "results" ? (
        <ol className={styles.results}>
          {history.rows.map((row) => (
            <li key={row.id} className={styles.result}>
              <a className={styles.resultTitle} href={row.href}>
                {row.title}
              </a>
              <span className={styles.meta}>
                {row.kind.replaceAll("_", " ")} · {row.status} · <time dateTime={row.dateTime}>{row.when}</time>
              </span>
              <p className={styles.excerpt}>{row.excerpt}</p>
            </li>
          ))}
        </ol>
      ) : history.state === "empty" ? (
        <p className={styles.historyNote}>No historical matches.</p>
      ) : history.state === "error" ? (
        <GenericError />
      ) : null}
    </section>
  );
}

export function SearchView({ state }: { state: SearchState }) {
  return (
    <div className={styles.screen}>
      <h1 className={styles.title}>
        {state.history.state === "hidden" ? "Search memory" : "Search Recallatron"}
      </h1>
      <form className={styles.form} role="search" method="get" action={state.action}>
        <label className={styles.label} htmlFor="memory-search-q">
          {state.history.state === "hidden" ? "Search memories" : "Search memories and history"}
        </label>
        <input
          className={styles.input}
          id="memory-search-q"
          type="search"
          name="q"
          maxLength={QUERY_MAX_LENGTH}
          defaultValue={state.query}
        />
        <button type="submit">Search</button>
      </form>
      <section aria-label="Search results" className={styles.section}>
        {state.history.state === "hidden" ? null : (
          <h2 className={styles.subheading}>Memories</h2>
        )}
        <Results results={state.results} />
      </section>
      <HistoryResults history={state.history} />
      <CoverageFigure coverage={state.coverage} />
    </div>
  );
}
