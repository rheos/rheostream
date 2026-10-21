import { beforeEach, describe, expect, it, vi } from "vitest";

const seams = vi.hoisted(() => ({ refresh: vi.fn() }));
vi.mock("next/cache", () => ({ refresh: seams.refresh }));

import { refreshScreen } from "./refresh-screen";

describe("refreshScreen (#443)", () => {
  beforeEach(() => seams.refresh.mockClear());

  it("renders the screen again in place and nothing more", async () => {
    await expect(refreshScreen()).resolves.toBeUndefined();
    expect(seams.refresh).toHaveBeenCalledOnce();
  });
});
