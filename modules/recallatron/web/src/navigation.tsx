import type { Screen } from "@rheo-stream/web-contract/screen";
import styles from "./navigation.module.css";
export function withNavigation(screen: Screen, current: string): Screen {
  return async function MemoryScreen(props) {
    return (
      <>
        <nav className={styles.nav} aria-label="Memory">
          {[
            ["browse", "Browse"],
            ["search", "Search memory"],
            ["duplicates", "Possible duplicates"],
          ].map(([id, label]) => (
            <a
              key={id}
              href={props.shell.href(id!)}
              aria-current={id === current ? "page" : undefined}
            >
              {label}
            </a>
          ))}
        </nav>
        {await screen(props)}
      </>
    );
  };
}
