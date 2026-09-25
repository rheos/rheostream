import type { ReactElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

/** Static markup for a view, and its visible text with entities decoded. */

export function render(element: ReactElement): string {
  return renderToStaticMarkup(element);
}

const ENTITIES: Readonly<Record<string, string>> = {
  "&amp;": "&",
  "&lt;": "<",
  "&gt;": ">",
  "&quot;": '"',
  "&#x27;": "'",
  "&#39;": "'",
};

export function textOf(markup: string): string {
  return markup
    .replace(/<[^>]*>/g, " ")
    .replace(/&(?:amp|lt|gt|quot|#x27|#39);/g, (entity) => ENTITIES[entity] ?? entity)
    .replace(/\s+/g, " ")
    .trim();
}
