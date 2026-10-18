"use client";
import { useId, useRef, useState } from "react";
import { selectTheme } from "./theme-action";
import type { ThemeId } from "./builtins";
import styles from "./theme-picker.module.css";

/** The theme choice, as it sits inside the settings menu. A change applies at
 * once; the Apply button remains for a page without scripts. */
export function ThemePicker({ current }: { current: ThemeId }) {
  const id = useId();
  const form = useRef<HTMLFormElement>(null);
  const [family, setFamily] = useState(
    current.startsWith("novadiem") ? "novadiem" : "greenstream",
  );
  return (
    <form ref={form} action={selectTheme} className={styles.form}>
      <label htmlFor={`${id}-family`}>Theme</label>
      <select
        id={`${id}-family`}
        name="family"
        value={family}
        onChange={(event) => {
          setFamily(event.target.value);
          // After React commits the Mode field, so the submitted form matches.
          setTimeout(() => form.current?.requestSubmit(), 0);
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
            defaultValue={current.endsWith("light") ? "light" : "dark"}
            onChange={() => form.current?.requestSubmit()}
          >
            <option value="dark">Dark</option>
            <option value="light">Light</option>
          </select>
        </>
      ) : null}
      <noscript>
        <button type="submit">Apply</button>
      </noscript>
    </form>
  );
}
