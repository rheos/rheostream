import type { AreaIcon } from "./data";

/** Line icons on a 24-unit grid, drawn for Current. Decorative: the control
 * that carries one names itself. */
const PATHS: Record<AreaIcon | ActionIcon, string[]> = {
  // area icons
  hammer: [
    "M14 6l4 4",
    "M12.5 4.5l2-2 7 7-2 2z",
    "M13 8L3.5 17.5a2 2 0 0 0 3 3L16 11",
  ],
  coins: ["M3 6h18v12H3z", "M12 9.5a2.5 2.5 0 1 0 0.01 0z", "M6.5 12h.01", "M17.5 12h.01"],
  piggy: [
    "M19 9.5c.8.3 1.5.9 2 1.5v3h-2c-.5 1-1.2 1.8-2 2.4V19h-3v-1.5h-3V19H8v-2.6A6 6 0 0 1 11 6h3c1.5 0 2.9.5 4 1.3L20 6z",
    "M16 11h.01",
    "M3 10c0 1.5 1 2.5 2.5 2.5",
  ],
  shield: [
    "M12 3l7 3v6c0 4.5-3 7.5-7 9-4-1.5-7-4.5-7-9V6z",
    "M9 12l2 2 4-4",
  ],
  cap: ["M2 9l10-5 10 5-10 5z", "M6 11v5c3 2.5 9 2.5 12 0v-5", "M22 9v6"],
  waves: [
    "M2 7c2-1.5 3.5-1.5 5 0s3 1.5 5 0 3.5-1.5 5 0 3 1.5 5 0",
    "M2 12c2-1.5 3.5-1.5 5 0s3 1.5 5 0 3.5-1.5 5 0 3 1.5 5 0",
    "M2 17c2-1.5 3.5-1.5 5 0s3 1.5 5 0 3.5-1.5 5 0 3 1.5 5 0",
  ],
  house: ["M3 11l9-7 9 7", "M5 9.5V20h14V9.5", "M10 20v-6h4v6"],
  globe: [
    "M12 3a9 9 0 1 0 0.01 0z",
    "M3 12h18",
    "M12 3c2.5 2.5 3.5 5.5 3.5 9s-1 6.5-3.5 9c-2.5-2.5-3.5-5.5-3.5-9s1-6.5 3.5-9z",
  ],
  tag: ["M3 12V4h8l10 10-8 8z", "M7.5 8h.01"],
  // actions
  check: ["M5 12.5l4.5 4.5L19 7.5"],
  eye: [
    "M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z",
    "M12 9a3 3 0 1 0 0.01 0z",
  ],
  eyeOff: [
    "M3 3l18 18",
    "M10.6 5.1A10 10 0 0 1 12 5c6.5 0 10 7 10 7a17 17 0 0 1-3 3.9",
    "M6.6 6.6C3.6 8.4 2 12 2 12s3.5 7 10 7c1.9 0 3.5-.6 4.9-1.4",
    "M9.9 9.9a3 3 0 0 0 4.2 4.2",
  ],
  alarm: [
    "M12 6a7 7 0 1 0 0.01 0z",
    "M12 9.5V13l2 2",
    "M4 4l2.5-2",
    "M20 4l-2.5-2",
  ],
  undo: ["M4 9h11a5 5 0 0 1 0 10h-3", "M8 5L4 9l4 4"],
  sunrise: ["M12 3v4", "M5 10l1.5 1.5", "M19 10l-1.5 1.5", "M3 18h18", "M7 18a5 5 0 0 1 10 0", "M2 22h20"],
  search: ["M11 4a7 7 0 1 0 0.01 0z", "M20 20l-4-4"],
  plus: ["M12 5v14", "M5 12h14"],
  chevron: ["M6 9l6 6 6-6"],
  layers: ["M12 3l9 5-9 5-9-5z", "M3 13l9 5 9-5"],
  flag: ["M5 21V4", "M5 4h11l-2 4 2 4H5"],
  grid: ["M4 4h7v7H4z", "M13 4h7v7h-7z", "M4 13h7v7H4z", "M13 13h7v7h-7z"],
  folder: [
    "M3 6a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z",
  ],
  close: ["M6 6l12 12", "M18 6L6 18"],
  late: ["M12 3a9 9 0 1 0 0.01 0z", "M12 7.5V12", "M12 16h.01"],
  blocked: ["M12 3a9 9 0 1 0 0.01 0z", "M5.6 5.6l12.8 12.8"],
  signals: ["M12 3l9 5-9 5-9-5z", "M3 13l9 5 9-5"],
};
export type ActionIcon =
  | "check"
  | "eye"
  | "eyeOff"
  | "alarm"
  | "undo"
  | "sunrise"
  | "search"
  | "plus"
  | "chevron"
  | "layers"
  | "flag"
  | "grid"
  | "folder"
  | "close"
  | "late"
  | "blocked"
  | "signals";

export function Icon({
  name,
  className,
}: {
  name: AreaIcon | ActionIcon;
  className?: string;
}) {
  return (
    <svg
      className={className}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
    >
      {PATHS[name].map((d) => (
        <path key={d} d={d} />
      ))}
    </svg>
  );
}
