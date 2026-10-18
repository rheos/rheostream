"use client";
import { useId, useState, useSyncExternalStore, useTransition } from "react";
import { selectTheme } from "./theme-action";
import type { ThemeId } from "./builtins";
import styles from "./theme-picker.module.css";

const noop = () => () => {};

/** The theme choice, as it sits inside the settings menu. Once scripts run, a
 * change applies at once by calling the action directly; until then (before
 * hydration, or with no scripts at all) an Apply button submits the form.
 *
 * The change does not submit the form: React resets a form after its action
 * runs, which put these controlled selects back to the server-rendered choice
 * while the page had already switched theme. */
export function ThemePicker({ current }: { current: ThemeId }) {
  const id = useId();
  const [family, setFamily] = useState(
    current.startsWith("novadiem") ? "novadiem" : "greenstream",
  );
  const [scheme, setScheme] = useState(current.endsWith("light") ? "light" : "dark");
  const [, startTransition] = useTransition();
  // False in the server markup and during hydration, true once scripts run.
  const live = useSyncExternalStore(
    noop,
    () => true,
    () => false,
  );

  function apply(nextFamily: string, nextScheme: string) {
    const data = new FormData();
    data.set("family", nextFamily);
    data.set("scheme", nextScheme);
    startTransition(() => selectTheme(data));
  }

  return (
    <form action={selectTheme} className={styles.form}>
      <label htmlFor={`${id}-family`}>Theme</label>
      <select
        id={`${id}-family`}
        name="family"
        value={family}
        onChange={(event) => {
          setFamily(event.target.value);
          apply(event.target.value, scheme);
        }}
      >
        <option value="greenstream">GreenStream</option>
        <option value="novadiem">Novadiem</option>
      </select>
      {family === "novadiem" ? (
        <>
          <label htmlFor={`${id}-scheme`}>Mode</label>
          <select
            id={`${id}-scheme`}
            name="scheme"
            value={scheme}
            onChange={(event) => {
              setScheme(event.target.value);
              apply(family, event.target.value);
            }}
          >
            <option value="dark">Dark</option>
            <option value="light">Light</option>
          </select>
        </>
      ) : null}
      {live ? null : <button type="submit">Apply</button>}
    </form>
  );
}
