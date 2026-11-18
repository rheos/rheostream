/**
 * What the screens call things, and how they write money.
 *
 * The module names every institution by a neutral codename, and a payee or account
 * the owner never labelled carries that codename as its label: `build_payees` labels
 * a bill issuer's payee with its directory key (`manual/payees.py`, step 4). So a
 * label that is exactly its own slug is a codename, and the screens never show it:
 * they say "Unnamed payee" and count it under Needs attention, where the owner can
 * give it a name. Document paths and source refs hold codenames too and are never
 * shown at all.
 */

import type { StatusLine } from "./data";

export const UNNAMED_PAYEE = "Unnamed payee";
export const UNNAMED_ACCOUNT = "Unnamed account";

/** A label the owner gave, or null when it is only the slug (a codename). */
export function ownLabel(slug: string | null, label: string | null): string | null {
  if (!label) return null;
  if (slug !== null && label === slug) return null;
  return label;
}

/** Every slug and label pair a screen read, so a label met without its slug (an open
 * item's account label) can still be recognised as a codename. */
export class Names {
  private readonly unnamed = new Set<string>();
  private readonly labels = new Map<string, string>();

  add(slug: string | null, label: string | null): this {
    if (!slug) return this;
    const own = ownLabel(slug, label);
    if (own !== null) {
      this.labels.set(slug, own);
      this.unnamed.delete(slug);
    } else if (!this.labels.has(slug)) {
      this.unnamed.add(slug);
    }
    return this;
  }

  /** The owner's label for `slug`; `unnamed` when the reads show it has none; else
   * `fallback`, for a slug no read labelled either way. */
  bySlug(slug: string | null, unnamed: string, fallback: string = unnamed): string {
    if (!slug) return fallback;
    return this.labels.get(slug) ?? (this.unnamed.has(slug) ? unnamed : fallback);
  }

  /** A label seen alone: shown unless it is one of the unnamed slugs. */
  label(label: string | null, fallback: string): string {
    if (!label || this.unnamed.has(label)) return fallback;
    return label;
  }

  get unnamedCount(): number {
    return this.unnamed.size;
  }
}

/** Sinks by what they do. Most sink ids beyond these three are codenames. */
const SINK_NAMES: Readonly<Record<string, string>> = {
  current: "Current",
  telegram: "Telegram",
  imap: "Email",
  rates: "Exchange rates",
};

export function sinkName(sink: string): string {
  return SINK_NAMES[sink] ?? "Vendor connection";
}

const MONEY = new Map<string, Intl.NumberFormat>();

/** `1234.5` in `currency` as "$1,234.50", or null when there is no figure. A code
 * the runtime does not know is written after the number. */
export function money(value: number | null, currency: string): string | null {
  if (value === null) return null;
  let format = MONEY.get(currency);
  if (!format) {
    try {
      format = new Intl.NumberFormat(undefined, {
        style: "currency",
        currency,
        currencyDisplay: "narrowSymbol",
      });
    } catch {
      format = new Intl.NumberFormat(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    }
    MONEY.set(currency, format);
  }
  const text = format.format(value);
  return format.resolvedOptions().style === "currency" ? text : `${text} ${currency}`;
}

/**
 * A status line as the screens show it: with no name in it. Line 2 lists each
 * source's last import by its key, so it keeps only the latest date; line 7 names
 * sinks by id; line 9's data checks name payees, accounts and folders in brackets or
 * after a colon, so those parts are cut. Every other line is counts and stamps.
 */
export function statusText(line: StatusLine): string {
  const { lineNo, text } = line;
  if (lineNo === 2) {
    const [imports = "", ...rest] = text.split("; ");
    const dates = imports.match(/\d{4}-\d{2}-\d{2}/g) ?? [];
    const latest = dates.sort().at(-1);
    const head = imports.startsWith("last import: none")
      ? "last import: none"
      : latest
        ? `last import ${latest}`
        : "last import";
    return [head, ...rest].join("; ");
  }
  if (lineNo === 7) {
    // "sinks off: a b; approval stale: c", or "sinks: all enabled".
    return text
      .split("; ")
      .map((part) => {
        const [label, ids] = part.split(": ");
        if (!ids || label === "sinks") return part;
        return `${label}: ${ids.split(" ").map(sinkName).join(", ")}`;
      })
      .join("; ");
  }
  if (lineNo === 9) {
    return text
      .split("; ")
      .map((part) => part.replace(/ \([^)]*\)/g, "").replace(/: .*$/, ""))
      .join("; ");
  }
  return text;
}
