import type { Screen } from "@rheo-stream/web-contract/screen";
import { areas, item, newItem, project, projects, queue } from "./screens";
import { ActionForm } from "./forms";
export const screens = {
  queue,
  item,
  newItem,
  projects,
  project,
  areas,
} satisfies Record<string, Screen>;
export const components = { ActionForm };
