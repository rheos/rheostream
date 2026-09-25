import type { ReactNode } from "react";

import type { ScreenProps } from "@rheo-stream/web-contract/screen";

import { loadBrowse } from "./load";
import { BrowseView } from "./view";

export async function browse(props: ScreenProps): Promise<ReactNode> {
  const state = await loadBrowse(props.shell, props.query);
  return <BrowseView state={state} />;
}
