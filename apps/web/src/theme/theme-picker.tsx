"use client";
import { useId, useState } from "react";
import { selectTheme } from "./theme-action";
import type { ThemeId } from "./builtins";
import styles from "./theme-picker.module.css";
export function ThemePicker({ current }: { current: ThemeId }) {
  const id = useId();
  const [family, setFamily] = useState(
    current.startsWith("novadiem") ? "novadiem" : "greenstream",
  );
  return (
    <details className={styles.picker}>
      <summary>Appearance</summary>
      <form action={selectTheme} className={styles.form}>
        <label htmlFor={`${id}-family`}>Theme</label>
        <select
          id={`${id}-family`}
          name="family"
          value={family}
          onChange={(event) => setFamily(event.target.value)}
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
            >
              <option value="dark">Dark</option>
              <option value="light">Light</option>
            </select>
          </>
        ) : (
          <p>Dark mode</p>
        )}
        <button type="submit">Apply appearance</button>
      </form>
    </details>
  );
}
