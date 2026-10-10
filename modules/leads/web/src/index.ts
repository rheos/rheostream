import type { Screen, ScreenProps } from "@rheo-stream/web-contract/screen";
import { overview, capture, connections, detail, receipt } from "./screens";
import { ActionForm } from "./action-form";
const list = (props: ScreenProps) => overview(props, false);
const board = (props: ScreenProps) => overview(props, true);
export const screens = {
  list,
  board,
  capture,
  connections,
  detail,
  receipt,
} satisfies Record<string, Screen>;
export const components = { ActionForm };
