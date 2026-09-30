import { act, type ReactNode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vite-plus/test";
import { blockApi } from "./api";
import { BlockReviewPanel } from "./BlockReviewPanel";

let root: Root | undefined;
let container: HTMLDivElement | undefined;

async function mount(element: ReactNode) {
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  await act(async () => root?.render(element));
  return container;
}

afterEach(async () => {
  if (root) await act(async () => root?.unmount());
  root = undefined;
  container?.remove();
  container = undefined;
  vi.unstubAllGlobals();
});

describe("blockApi.reviewContext", () => {
  it("sends the selected block IDs as the review scope", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ kind: "factcheck", context: "selected", summary: "Checked", items: [] }),
    });
    vi.stubGlobal("fetch", fetchMock);

    await blockApi.reviewContext("document 1", "selected", ["block-1", "block-2"]);

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/block-documents/document%201/review-context",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          kind: "factcheck",
          context: "selected",
          block_ids: ["block-1", "block-2"],
        }),
      }),
    );
  });
});

describe("BlockReviewPanel", () => {
  it("only starts review when blocks are selected and shows the returned summary", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        status: 200,
        json: async () => ({
          kind: "factcheck",
          context: "selected",
          summary: "Two highlighted blocks reviewed.",
          items: [],
        }),
      }),
    );
    const panel = await mount(<BlockReviewPanel documentId="doc-1" selectedBlockIds={[]} />);
    const button = panel.querySelector<HTMLButtonElement>("button")!;
    expect(button.disabled).toBe(true);
    expect(panel.querySelector("select")).toBeNull();

    await act(async () =>
      root?.render(
        <BlockReviewPanel documentId="doc-1" selectedBlockIds={["block-1", "block-2"]} />,
      ),
    );
    expect(button.disabled).toBe(false);
    await act(async () => button.click());

    expect(panel.textContent).toContain("2 blocks selected");
    expect(panel.textContent).toContain("Two highlighted blocks reviewed.");
  });
});
