import { act } from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vite-plus/test";
import { RelatedNotes } from "./RelatedNotes";

function related(id: string, title: string) {
  return {
    note: { id, title, status: "rough", tags: [], updated_at: "", snippet: "" },
    score: 0.5,
    matched_block_id: null,
    matched_block_text: null,
  };
}

describe("RelatedNotes", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("doesn't show the previous note's related notes while a new note's load", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL) => {
        const path = new URL(String(input), window.location.origin).pathname;
        if (path === "/api/notes/a/related") {
          return Promise.resolve(
            new Response(JSON.stringify([related("c", "Related to A")]), {
              headers: { "Content-Type": "application/json" },
            }),
          );
        }
        return new Promise<Response>(() => {}); // note b: still loading
      }),
    );
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const container = document.createElement("div");
    const root = createRoot(container);
    const render = (noteId: string, blockId: string | null = null) =>
      act(async () =>
        root.render(
          <QueryClientProvider client={client}>
            <RelatedNotes noteId={noteId} blockId={blockId} onSelect={() => {}} />
          </QueryClientProvider>,
        ),
      );

    await render("a");
    await vi.waitFor(() => expect(container.textContent).toContain("Related to A"));

    // Moving between blocks of the same note keeps the list while it refreshes…
    await render("a", "block-1");
    expect(container.textContent).toContain("Related to A");

    // …but another note must not show A's list as its own.
    await render("b");
    expect(container.textContent).not.toContain("Related to A");
    expect(container.textContent).toContain("Finding related…");

    act(() => root.unmount());
    client.clear();
  });
});
