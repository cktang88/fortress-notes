import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vite-plus/test";
import { App } from "./App";

vi.mock("./features/notes/BlockNoteEditor", () => ({
  BlockNoteEditor: ({ focusBlockId }: { focusBlockId: string | null }) => (
    <div data-testid="block-editor" data-focus-block={focusBlockId ?? ""} />
  ),
}));

const date = "2026-09-28T12:00:00Z";
const notes = ["first", "second"].map((id) => ({
  id,
  title: `${id} note`,
  status: "rough",
  tags: [],
  snippet: `${id} body`,
  updated_at: date,
}));

function jsonResponse(value: unknown) {
  return new Response(JSON.stringify(value), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

describe("App navigation integration", () => {
  let root: Root | undefined;
  let container: HTMLDivElement | undefined;
  let queryClient: QueryClient | undefined;

  afterEach(() => {
    if (root) act(() => root?.unmount());
    container?.remove();
    queryClient?.clear();
    vi.unstubAllGlobals();
    window.history.replaceState(null, "", "/");
    root = undefined;
    container = undefined;
    queryClient = undefined;
  });

  it("opens a deep-linked block and follows sidebar navigation", async () => {
    window.history.replaceState(null, "", "/?note=first#block=block-1");
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const path = new URL(String(input), window.location.origin).pathname;
        if (path === "/api/notes") return jsonResponse(notes);
        if (path === "/api/navigation") {
          return jsonResponse({
            items: notes.map((note) => ({
              ...note,
              kind: "document",
              folder_id: null,
              position: 0,
              created_at: date,
            })),
            recent: [],
          });
        }
        if (path === "/api/notes/first" || path === "/api/notes/second") {
          const note = notes.find((item) => path.endsWith(item.id))!;
          return jsonResponse({ ...note, body: `${note.id} body`, created_at: date });
        }
        if (path === "/api/block-documents/first" || path === "/api/block-documents/second") {
          const id = path.split("/").at(-1)!;
          return jsonResponse({
            ...notes.find((item) => item.id === id),
            revision: 0,
            children: [],
            created_at: date,
          });
        }
        if (path.endsWith("/backlinks") || path.endsWith("/related")) return jsonResponse([]);
        throw new Error(`Unexpected API request: ${path}`);
      }),
    );

    container = document.createElement("div");
    document.body.append(container);
    queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    root = createRoot(container);
    await act(async () => {
      root?.render(
        <QueryClientProvider client={queryClient!}>
          <App />
        </QueryClientProvider>,
      );
    });

    await waitFor(() =>
      expect(
        container?.querySelector("[data-testid='block-editor']")?.getAttribute("data-focus-block"),
      ).toBe("block-1"),
    );

    const secondNote = container.querySelector<HTMLButtonElement>(
      '[data-document-row-id="second"] button',
    );
    expect(secondNote).not.toBeNull();
    await act(async () => secondNote?.click());
    await waitFor(() => expect(window.location.search).toContain("note=second"));
    await waitFor(() =>
      expect(container?.querySelector("[data-testid='block-editor']")).not.toBeNull(),
    );
  });
});

async function waitFor(assertion: () => void) {
  for (let attempt = 0; attempt < 40; attempt += 1) {
    try {
      assertion();
      return;
    } catch {
      await act(async () => new Promise((resolve) => setTimeout(resolve, 10)));
    }
  }
  assertion();
}
