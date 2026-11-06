import { afterEach, describe, expect, it, vi } from "vite-plus/test";
import { blockApi, fitsKeepalive, KEEPALIVE_BODY_LIMIT } from "./api";

afterEach(() => vi.unstubAllGlobals());

describe("saving edits as the page goes away", () => {
  it("sends edits with keepalive so they outlive a closed tab", async () => {
    const fetch = vi.fn(async () => new Response("{}", { status: 200 }));
    vi.stubGlobal("fetch", fetch);
    await blockApi.transaction("doc", {
      transaction_id: "t1",
      base_revision: 1,
      operations: [{ operation: "update", block_id: "b", text: "hi" }],
    });
    expect(fetch).toHaveBeenCalledWith(
      "/api/block-documents/doc/transactions",
      expect.objectContaining({ method: "POST", keepalive: true }),
    );
  });

  it("sends edits too big for keepalive as a normal request", async () => {
    const fetch = vi.fn(async () => new Response("{}", { status: 200 }));
    vi.stubGlobal("fetch", fetch);
    await blockApi.transaction("doc", {
      transaction_id: "t2",
      base_revision: 1,
      operations: [{ operation: "update", block_id: "b", text: "x".repeat(KEEPALIVE_BODY_LIMIT) }],
    });
    expect(fetch).toHaveBeenCalledWith(
      "/api/block-documents/doc/transactions",
      expect.objectContaining({ keepalive: false }),
    );
  });

  it("measures the limit in bytes, not characters", () => {
    expect(fitsKeepalive("a".repeat(KEEPALIVE_BODY_LIMIT))).toBe(true);
    expect(fitsKeepalive("é".repeat(KEEPALIVE_BODY_LIMIT / 2 + 1))).toBe(false);
  });
});
