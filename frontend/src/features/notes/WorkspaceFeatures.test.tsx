import { act, type ReactNode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vite-plus/test";
import { SearchBar } from "./SearchBar";
import { TagEditorDialog } from "./TagEditorDialog";
import { WorkspaceMenu } from "./WorkspaceMenu";
import { SEARCH_INPUT_ID } from "./listKeyboard";
import type { SavedSearch } from "./types";

let root: Root | undefined;
let container: HTMLDivElement | undefined;

afterEach(() => {
  if (root) act(() => root?.unmount());
  container?.remove();
  root = undefined;
  container = undefined;
  vi.unstubAllGlobals();
});

function render(node: ReactNode) {
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  act(() => root?.render(node));
  return container;
}

function button(label: string | RegExp) {
  const match = [...document.querySelectorAll<HTMLButtonElement>("button")].find((item) =>
    typeof label === "string"
      ? item.textContent?.trim() === label || item.getAttribute("aria-label") === label
      : label.test(item.textContent ?? ""),
  );
  if (!match) throw new Error(`No button ${label}`);
  return match;
}

const saved: SavedSearch = {
  id: "s1",
  name: "budget",
  query: "budget",
  mode: "text",
  filters: { status: "rough" },
  created_at: "2026-10-01T00:00:00Z",
};

describe("saved searches", () => {
  const baseProps = {
    mode: "text" as const,
    onMode: vi.fn(),
    filters: {},
    onFilters: vi.fn(),
    onNew: vi.fn(),
  };

  it("shows saved searches when idle and applies or forgets them", () => {
    const onApply = vi.fn();
    const onDelete = vi.fn();
    render(
      <SearchBar
        {...baseProps}
        query=""
        onQuery={vi.fn()}
        savedSearches={[saved]}
        onSaveSearch={vi.fn()}
        onApplySavedSearch={onApply}
        onDeleteSavedSearch={onDelete}
      />,
    );
    act(() => button(/★ budget/).click());
    act(() => button("Forget saved search budget").click());
    expect(onApply).toHaveBeenCalledWith(saved);
    expect(onDelete).toHaveBeenCalledWith("s1");
    expect(document.body.textContent).not.toContain("Save search");
  });

  it("offers to save the current search and marks an existing one as saved", () => {
    const onSave = vi.fn();
    render(
      <SearchBar
        {...baseProps}
        query="ideas"
        onQuery={vi.fn()}
        savedSearches={[saved]}
        onSaveSearch={onSave}
      />,
    );
    act(() => button("☆ Save search").click());
    expect(onSave).toHaveBeenCalledOnce();
    act(() => root?.unmount());
    root = undefined;
    container?.remove();

    render(
      <SearchBar
        {...baseProps}
        query="budget"
        filters={{ status: "rough" }}
        onQuery={vi.fn()}
        savedSearches={[saved]}
        onSaveSearch={onSave}
      />,
    );
    expect(button("★ Saved").disabled).toBe(true);
  });

  it("clears the search with Escape and filters by tag", () => {
    const onQuery = vi.fn();
    const onFilters = vi.fn();
    render(
      <SearchBar
        {...baseProps}
        query="something"
        onQuery={onQuery}
        onFilters={onFilters}
        tags={["work", "home"]}
      />,
    );
    const input = document.getElementById(SEARCH_INPUT_ID)!;
    act(() => {
      input.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    });
    expect(onQuery).toHaveBeenCalledWith("");

    const tag = container!.querySelector<HTMLSelectElement>('[aria-label="Tag"]')!;
    act(() => {
      tag.value = "work";
      tag.dispatchEvent(new Event("change", { bubbles: true }));
    });
    expect(onFilters).toHaveBeenCalledWith({ tag: "work" });
  });
});

describe("TagEditorDialog", () => {
  it("adds comma-separated tags without duplicates and removes tags", () => {
    const onChange = vi.fn();
    render(
      <TagEditorDialog
        open
        title="Trip"
        tags={["travel"]}
        suggestions={["travel", "family"]}
        onChange={onChange}
        onClose={vi.fn()}
      />,
    );
    const input = container!.querySelector<HTMLInputElement>("input")!;
    act(() => {
      const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!;
      setter.call(input, "#family, travel, food");
      input.dispatchEvent(new Event("input", { bubbles: true }));
    });
    act(() => input.form!.requestSubmit());
    expect(onChange).toHaveBeenLastCalledWith(["travel", "family", "food"]);

    act(() => button("Remove tag travel").click());
    expect(onChange).toHaveBeenLastCalledWith([]);
  });
});

describe("WorkspaceMenu", () => {
  function jsonResponse(value: unknown, status = 200) {
    return new Response(status === 204 ? null : JSON.stringify(value), {
      status,
      headers: { "Content-Type": "application/json" },
    });
  }

  it("restores a note from the trash and offers to open it", async () => {
    let trash = [
      {
        id: "n1",
        title: "Old idea",
        status: "rough",
        tags: [],
        deleted_at: "2026-10-01T00:00:00Z",
        snippet: "something",
      },
    ];
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = new URL(String(input), window.location.origin).pathname;
      if (path === "/api/trash" && !init?.method) return jsonResponse(trash);
      if (path === "/api/trash/n1/restore") {
        trash = [];
        return jsonResponse({ id: "n1", folder_id: null, position: 0 });
      }
      return jsonResponse([]);
    });
    vi.stubGlobal("fetch", fetchMock);
    const onNotice = vi.fn();
    const onOpenNote = vi.fn();
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <WorkspaceMenu onNotice={onNotice} onOpenNote={onOpenNote} />
      </QueryClientProvider>,
    );

    act(() => button("Workspace menu").click());
    expect(
      [...document.querySelectorAll("[role='menuitem']")].map((item) => item.textContent),
    ).toEqual(["Import Markdown files…", "Export all notes (.zip)", "Trash", "Backups…"]);
    act(() => button("Trash").click());
    await waitFor(() => expect(document.body.textContent).toContain("Old idea"));

    await act(async () => button("Restore").click());
    await waitFor(() => expect(onNotice).toHaveBeenCalled());
    const notice = onNotice.mock.calls[0][0];
    expect(notice.message).toBe("Restored “Old idea”");
    notice.action.run();
    expect(onOpenNote).toHaveBeenCalledWith("n1");
    await waitFor(() => expect(document.body.textContent).toContain("Trash is empty."));
    client.clear();
  });

  it("imports chosen Markdown files and opens the first one", async () => {
    const fetchMock = vi.fn(async () =>
      jsonResponse({
        imported: [{ name: "a.md", id: "new-1", title: "a" }],
        errors: [{ name: "b.md", detail: "file is not UTF-8 text" }],
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const onNotice = vi.fn();
    const onOpenNote = vi.fn();
    const client = new QueryClient();
    render(
      <QueryClientProvider client={client}>
        <WorkspaceMenu onNotice={onNotice} onOpenNote={onOpenNote} />
      </QueryClientProvider>,
    );
    const input = container!.querySelector<HTMLInputElement>(
      "[data-testid='markdown-import-input']",
    )!;
    const files = [new File(["# A"], "a.md"), new File(["x"], "b.md")];
    Object.defineProperty(input, "files", { value: files, configurable: true });
    await act(async () => input.dispatchEvent(new Event("change", { bubbles: true })));

    await waitFor(() => expect(onOpenNote).toHaveBeenCalledWith("new-1"));
    expect(onNotice).toHaveBeenCalledWith({
      tone: "info",
      message: "Imported 1 note · couldn't read b.md",
    });
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/markdown-import/files");
    expect((init.body as FormData).getAll("files")).toHaveLength(2);
    client.clear();
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
