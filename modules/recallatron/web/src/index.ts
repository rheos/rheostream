import { withNavigation } from "./navigation";
import type { Screen } from "@rheo-stream/web-contract/screen";

import { browse } from "./screens/browse/screen";
import { duplicates } from "./screens/duplicates/screen";
import { historyItem } from "./screens/history-item/screen";
import { item } from "./screens/item/screen";
import { search } from "./screens/search/screen";

/**
 * Recallatron's screens, keyed by the `screen` identifier each route in the module's
 * manifest names. The shell's generated module list reads these as property accesses
 * (`screens.browse`), so a renamed or missing export fails its type check.
 *
 * Each screen is an async server component with no client script. It is built from a
 * `load*` data loader and a synchronous `*View`, exported too so each half is testable
 * on its own.
 */
export const screens = Object.fromEntries(Object.entries({ browse, search, item, duplicates, historyItem }).map(([name, screen]) => [name, withNavigation(screen, name)])) as Record<"browse" | "search" | "item" | "duplicates" | "historyItem", Screen>;

export { browse, duplicates, historyItem, item, search };
export { loadBrowse, type BrowseState } from "./screens/browse/load";
export { BrowseView } from "./screens/browse/view";
export { loadSearch, type SearchState } from "./screens/search/load";
export { SearchView } from "./screens/search/view";
export { loadItem, type ItemState } from "./screens/item/load";
export { ItemView } from "./screens/item/view";
export { loadDuplicates, type DuplicatesState } from "./screens/duplicates/load";
export { DuplicatesView } from "./screens/duplicates/view";
export { loadCoverage, CoverageFigure, type CoverageState } from "./components/coverage";
