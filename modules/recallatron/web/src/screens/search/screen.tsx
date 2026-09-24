import type { ReactNode } from "react";

import type { ScreenProps } from "@rheo-stream/web-contract/screen";

import { loadSearch } from "./load";
import { SearchView } from "./view";

export async function search(props: ScreenProps): Promise<ReactNode> {
  const state = await loadSearch(props.shell, props.query);
  return <SearchView state={state} />;
}
