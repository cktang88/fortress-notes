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
        if (path === "/api/tags" || path === "/api/saved-searches") return jsonResponse([]);
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

describe("App trash and shortcuts", () => {
  let root: Root | undefined;
  let container: HTMLDivElement | undefined;
  let queryClient: QueryClient | undefined;

  afterEach(() => {
    if (root) act(() => root?.unmount());
    container?.remove();
    queryClient?.clear();
    vi.unstubAllGlobals();
    window.history.replaceState(null, "", "/");
  });

  it("moves a note to the trash with an Undo that restores and reopens it", async () => {
    window.history.replaceState(null, "", "/?note=first");
    let deleted = false;
    const calls: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const path = new URL(String(input), window.location.origin).pathname;
        const method = init?.method ?? "GET";
        if (method !== "GET") calls.push(`${method} ${path}`);
        const visible = deleted ? notes.filter((note) => note.id !== "first") : notes;
        if (path === "/api/notes") return jsonResponse(visible);
        if (path === "/api/navigation") {
          return jsonResponse({
            items: visible.map((note) => ({
              ...note,
              kind: "document",
              folder_id: null,
              position: 0,
              created_at: date,
            })),
            recent: [],
          });
        }
        if (path === "/api/block-documents/first" && method === "DELETE") {
          deleted = true;
          return new Response(null, { status: 204 });
        }
        if (path === "/api/trash/first/restore") {
          deleted = false;
          return jsonResponse({ id: "first", folder_id: null, position: 0 });
        }
        if (path.endsWith("/related")) return jsonResponse([]);
        if (path.startsWith("/api/notes/")) {
          const note = notes.find((item) => path.endsWith(item.id))!;
          return jsonResponse({ ...note, body: "", created_at: date });
        }
        if (path.startsWith("/api/block-documents/") && method === "GET") {
          const id = path.split("/").at(-1)!;
          if (path.endsWith("/backlinks")) return jsonResponse([]);
          return jsonResponse({
            ...notes.find((item) => item.id === id),
            revision: 0,
            children: [],
            created_at: date,
          });
        }
        return jsonResponse([]);
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
    await waitFor(() => expect(findButton(container!, "Delete")).not.toBeUndefined());

    await act(async () => findButton(container!, "Delete")?.click());
    await waitFor(() => expect(container?.textContent).toContain("Moved “first note” to Trash"));
    expect(window.location.search).not.toContain("note=first");

    await act(async () => findButton(container!, "Undo")?.click());
    await waitFor(() => expect(window.location.search).toContain("note=first"));
    expect(calls.filter((call) => !call.endsWith("/link-checks"))).toEqual([
      "DELETE /api/block-documents/first",
      "POST /api/trash/first/restore",
    ]);
  });

  it("focuses search with Ctrl+K", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const path = new URL(String(input), window.location.origin).pathname;
        if (path === "/api/navigation") return jsonResponse({ items: [], recent: [] });
        return jsonResponse([]);
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
    act(() => {
      document.dispatchEvent(new KeyboardEvent("keydown", { key: "k", ctrlKey: true }));
    });
    expect(document.activeElement?.id).toBe("workspace-search");
  });
});

function findButton(root: HTMLElement, label: string) {
  return [...root.querySelectorAll<HTMLButtonElement>("button")].find(
    (button) => button.textContent?.trim() === label,
  );
}

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
