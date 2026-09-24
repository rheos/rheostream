import { GenericError, StateMessage } from "../../components/state-message";
import type { DuplicatesState, PairSide } from "./load";
import styles from "./duplicates.module.css";

/**
 * The duplicates list's view. Links only: this view renders no form and no button,
 * because the screen offers nothing to do to a pair beyond opening either memory.
 */

function Side({ side }: { side: PairSide }) {
  return side.title === null ? (
    <span className={styles.unavailable}>unavailable</span>
  ) : (
    <a className={styles.sideLink} href={side.href}>
      {side.title}
    </a>
  );
}

function Pairs({ state }: { state: DuplicatesState }) {
  switch (state.state) {
    case "pairs":
      return (
        <ol className={styles.pairs}>
          {state.pairs.map((pair) => (
            <li key={`${pair.a.ref}|${pair.b.ref}`} className={styles.pair}>
              <div className={styles.sides}>
                <Side side={pair.a} />
                <Side side={pair.b} />
              </div>
              <span className={styles.score}>{pair.similarity}% similar</span>
            </li>
          ))}
        </ol>
      );
    case "empty":
      return (
        <StateMessage name="no-duplicates">
          <p className={styles.stateText}>
            No possible duplicates found. Pairs appear here when meaning search is on and
            two memories are close in meaning.
          </p>
        </StateMessage>
      );
    case "error":
      return <GenericError />;
  }
}

export function DuplicatesView({ state }: { state: DuplicatesState }) {
  return (
    <div className={styles.screen}>
      <h1 className={styles.title}>Possible duplicates</h1>
      <Pairs state={state} />
    </div>
  );
}
