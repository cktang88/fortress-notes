import { afterEach, describe, expect, it, vi } from "vite-plus/test";
import { act, type ReactNode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { blockApi } from "./api";
import {
  BlockReviewPanel,
  canApplyStaleSuggestion,
  readReviewDecisions,
  StaleFinding,
} from "./BlockReviewPanel";

let root: Root | undefined;
const mount = async (element: ReactNode) => {
  const container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  await act(async () => root?.render(element));
  return container;
};

describe("blockApi.review", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("posts the selected stable block ID to the block review endpoint", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({
        kind: "factcheck",
        summary: "Review complete",
        block_id: "block-1",
        items: [],
      }),
    });
    vi.stubGlobal("fetch", fetchMock);

    const result = await blockApi.review("document 1", "block-1");

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/block-documents/document%201/blocks/block-1/review?kind=factcheck",
      expect.objectContaining({ method: "POST" }),
    );
    expect(result.block_id).toBe("block-1");
  });
});

describe("blockApi.healthFindings", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("requests document consistency and stale/link checks together", async () => {
    const fetchMock = vi.fn().mockImplementation((url: string) => Promise.resolve({
      ok: true,
      status: 200,
      json: async () => url.includes("/consistency")
        ? { summary: "Compared", issues: [], checked_against: [] }
        : { summary: "Checked", dead_links: [], stale_facts: [] },
    }));
    vi.stubGlobal("fetch", fetchMock);

    const result = await blockApi.healthFindings("doc-1");

    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(fetchMock).toHaveBeenCalledWith("/api/notes/doc-1/consistency", expect.objectContaining({ method: "POST" }));
    expect(fetchMock).toHaveBeenCalledWith("/api/notes/doc-1/heal", expect.objectContaining({ method: "POST" }));
    expect(result.healing.dead_links).toEqual([]);
  });
});

describe("blockApi.reviewContext", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("posts the selected context mode and canonical block IDs", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({
        kind: "factcheck",
        context: "selected",
        summary: "Reviewed selected blocks",
        items: [],
      }),
    });
    vi.stubGlobal("fetch", fetchMock);

    const result = await blockApi.reviewContext("document 1", "selected", ["block-1", "block-2"]);

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
    expect(result.context).toBe("selected");
  });
});

describe("BlockReviewPanel context picker", () => {
  afterEach(async () => {
    if (root) await act(async () => root?.unmount());
    root = undefined;
    document.body.innerHTML = "";
    vi.unstubAllGlobals();
  });

  it("reviews the selected blocks and displays each exact block quote", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({
        kind: "factcheck",
        context: "selected",
        summary: "Two blocks reviewed.",
        items: [
          {
            block_id: "block-1",
            label: "Claim",
            detail: "Check this statement.",
            quote: "Canonical text one",
            severity: "medium",
          },
          {
            block_id: "block-2",
            label: "Claim",
            detail: "Check the second statement.",
            quote: "Canonical text two",
            severity: "low",
          },
        ],
      }),
    });
    vi.stubGlobal("fetch", fetchMock);
    const container = await mount(
      <BlockReviewPanel
        documentId="document-1"
        blockId="block-1"
        selectedBlockIds={["block-1", "block-2"]}
      />,
    );

    const select = container.querySelector<HTMLSelectElement>("#block-review-context");
    expect([...select!.options].map((option) => option.value)).toEqual([
      "current",
      "selected",
      "document",
      "linked",
    ]);
    await act(async () => {
      select!.value = "selected";
      select!.dispatchEvent(new Event("change", { bubbles: true }));
    });
    const button = [...container.querySelectorAll("button")].find(
      (candidate) => candidate.textContent === "Review context",
    );
    await act(async () => button?.click());

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/block-documents/document-1/review-context",
      expect.objectContaining({
        body: JSON.stringify({
          kind: "factcheck",
          context: "selected",
          block_ids: ["block-1", "block-2"],
        }),
      }),
    );
    expect(container.textContent).toContain("block block-1");
    expect(container.textContent).toContain("Canonical text one");
    expect(container.textContent).toContain("block block-2");
    expect(container.textContent).toContain("Canonical text two");
  });
});

describe("StaleFinding", () => {
  afterEach(async () => {
    if (root) await act(async () => root?.unmount());
    root = undefined;
    document.body.innerHTML = "";
    window.localStorage.clear();
  });

const props = {
    blockId: "b1", claim: "old value", suggestion: "new value", detail: "It changed.",
    onApply: vi.fn().mockReturnValue(false), onAccept: vi.fn(), onDismiss: vi.fn(),
  };

  it("hides Accept when the claim is only part of the block, but allows dismiss", async () => {
    const container = await mount(<StaleFinding {...props} blockText="prefix old value suffix" canApply />);
    expect(container.textContent).toContain("Before:");
    expect(container.textContent).toContain("After:");
    const accept = [...container.querySelectorAll("button")].find((button) => button.textContent === "Accept suggestion");
    expect(accept).toBeUndefined();
    const dismiss = [...container.querySelectorAll("button")].find((button) => button.textContent === "Reject finding");
    await act(async () => dismiss?.click());
    expect(props.onDismiss).toHaveBeenCalledOnce();
  });

  it("calls the apply callback only for an exact whole-block claim", async () => {
    const onApply = vi.fn().mockReturnValue(true);
    const onAccept = vi.fn();
    const container = await mount(<StaleFinding {...props} onApply={onApply} onAccept={onAccept} blockText="old value" canApply />);
    const accept = [...container.querySelectorAll("button")].find((button) => button.textContent === "Accept suggestion");
    expect(accept).toBeDefined();
    await act(async () => accept?.click());
    expect(onApply).toHaveBeenCalledOnce();
    expect(onAccept).toHaveBeenCalledOnce();
  });

  it("keeps the finding visible when the apply callback declines the change", async () => {
    const onDismiss = vi.fn();
    const container = await mount(
      <StaleFinding {...props} onDismiss={onDismiss} blockText="old value" canApply />,
    );
    const accept = [...container.querySelectorAll("button")].find((button) => button.textContent === "Accept suggestion");
    await act(async () => accept?.click());
    expect(container.textContent).toContain("Possibly outdated");
    expect(onDismiss).not.toHaveBeenCalled();
  });

  it("requires both exact full-block text and editor approval", () => {
    const approve = vi.fn().mockReturnValue(true);
    const change = { blockId: "b1", before: "old value", after: "new value" };
    expect(canApplyStaleSuggestion(change, "prefix old value", approve)).toBe(false);
    expect(approve).not.toHaveBeenCalled();
    expect(canApplyStaleSuggestion(change, "old value", approve)).toBe(true);
    expect(approve).toHaveBeenCalledWith(change);
    expect(canApplyStaleSuggestion(change, "old value", () => false)).toBe(false);
  });

  it("restores accepted and rejected decisions from browser storage", () => {
    window.localStorage.setItem(
      "fortress-notes:review-decisions:doc-1",
      JSON.stringify({ accepted: "accepted", rejected: "rejected", bad: "other" }),
    );
    expect(readReviewDecisions("doc-1")).toEqual({ accepted: "accepted", rejected: "rejected" });
  });
});
