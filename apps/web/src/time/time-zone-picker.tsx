"use client";
import { useEffect, useId, useState, useSyncExternalStore, useTransition } from "react";
import styles from "@/theme/theme-picker.module.css";
import { detectTimeZone, selectTimeZone } from "./time-zone-action";
import type { ViewerTimeZone } from "./time-zone";

const noop = () => () => {};

function browserZone(): string {
  return Intl.DateTimeFormat().resolvedOptions().timeZone;
}

/** Tells the server the browser's zone when it follows the browser and the
 * cookie is missing or out of date (a first visit, or the laptop moved). */
export function TimeZoneSync({ current }: { current: ViewerTimeZone | null }) {
  useEffect(() => {
    const zone = browserZone();
    if (!current?.chosen && current?.zone !== zone) void detectTimeZone(zone);
  }, [current]);
  return null;
}

/** The time zone choice in the settings menu, built like `ThemePicker`: a change
 * applies at once once scripts run, an Apply button submits it before that. */
export function TimeZonePicker({
  current,
  zones,
}: {
  current: ViewerTimeZone | null;
  zones: readonly string[];
}) {
  const id = useId();
  const [choice, setChoice] = useState(current?.chosen ? current.zone : "auto");
  const [, startTransition] = useTransition();
  // False in the server markup and during hydration, true once scripts run.
  const live = useSyncExternalStore(
    noop,
    () => true,
    () => false,
  );
  const following = current && !current.chosen ? current.zone : null;

  return (
    <form action={selectTimeZone} className={styles.form}>
      <label htmlFor={`${id}-zone`}>Time zone</label>
      <select
        id={`${id}-zone`}
        name="zone"
        value={choice}
        onChange={(event) => {
          setChoice(event.target.value);
          const data = new FormData();
          data.set("zone", event.target.value);
          data.set("browser", browserZone());
          startTransition(() => selectTimeZone(data));
        }}
      >
        <option value="auto">{following ? `Automatic (${following})` : "Automatic"}</option>
        {zones.map((zone) => (
          <option key={zone} value={zone}>
            {zone.replaceAll("_", " ")}
          </option>
        ))}
      </select>
      {live ? null : <button type="submit">Apply</button>}
    </form>
  );
}
