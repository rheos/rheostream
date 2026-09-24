import type { ReactNode } from "react";

import type { ScreenProps } from "@rheo-stream/web-contract/screen";

import { loadDuplicates } from "./load";
import { DuplicatesView } from "./view";

export async function duplicates(props: ScreenProps): Promise<ReactNode> {
  const state = await loadDuplicates(props.shell);
  return <DuplicatesView state={state} />;
}
