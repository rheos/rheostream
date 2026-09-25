import type { ReactNode } from "react";

import type { ScreenProps } from "@rheo-stream/web-contract/screen";

import { loadItem } from "./load";
import { ItemView } from "./view";

export async function item(props: ScreenProps): Promise<ReactNode> {
  const state = await loadItem(props.shell, props.query);
  return <ItemView state={state} />;
}
