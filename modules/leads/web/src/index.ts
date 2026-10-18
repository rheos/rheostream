import type { Screen, ScreenProps } from "@rheo-stream/web-contract/screen";
import {
  overview,
  capture,
  connections,
  detail,
  queue,
  receipt,
  review,
  scoring,
} from "./screens";
import { ActionForm } from "./action-form";
const list = (props: ScreenProps) => overview(props, false);
const board = (props: ScreenProps) => overview(props, true);
export const screens = {
  queue,
  review,
  scoring,
  list,
  board,
  capture,
  connections,
  detail,
  receipt,
} satisfies Record<string, Screen>;
export const components = { ActionForm };
