import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vite-plus/test";
import { DocumentSidebar } from "./DocumentSidebar";
import type { NavigationDocument, NavigationNode } from "./DocumentSidebar";

const navigation: NavigationNode[] = [
  {
    kind: "folder",
    id: "projects",
    parent_id: null,
    name: "Projects",
    position: 0,
    created_at: "2026-09-20T15:00:00Z",
    updated_at: "2026-09-21T15:00:00Z",
    children: [
      {
        kind: "document",
        id: "roadmap",
        title: "Roadmap",
        status: "polished",
        tags: [],
        snippet: "",
        folder_id: "projects",
        position: 0,
        created_at: "2026-09-20T15:00:00Z",
        updated_at: "2026-09-21T15:00:00Z",
      },
      {
        kind: "folder",
        id: "archive",
        parent_id: "projects",
        name: "Archive",
        position: 1,
        created_at: "2026-09-20T15:00:00Z",
        updated_at: "2026-09-20T15:00:00Z",
        children: [
          {
            kind: "document",
            id: "old-plan",
            title: "Old plan",
            status: "rough",
            tags: [],
            snippet: "",
            folder_id: "archive",
            position: 0,
            created_at: "2026-09-01T15:00:00Z",
            updated_at: "2026-09-01T15:00:00Z",
          },
        ],
      },
    ],
  },
  {
    kind: "document",
    id: "welcome",
    title: "Welcome",
    status: "rough",
    tags: [],
    snippet: "",
    folder_id: null,
    position: 0,
    created_at: "2026-09-21T14:00:00Z",
    updated_at: "2026-09-21T14:00:00Z",
  },
  {
    kind: "folder",
    id: "empty",
    parent_id: null,
    name: "Empty",
    position: 1,
    created_at: "2026-09-21T14:00:00Z",
    updated_at: "2026-09-21T14:00:00Z",
    children: [],
  },
];

const recentDocuments: NavigationDocument[] = [
  {
    kind: "document",
    id: "welcome",
    title: "Welcome",
    status: "rough",
    tags: [],
    snippet: "",
    folder_id: null,
    position: 0,
    created_at: "2026-09-21T14:00:00Z",
    updated_at: "2026-09-21T14:00:00Z",
  },
];

describe("DocumentSidebar", () => {
  let root: Root | undefined;
  let container: HTMLDivElement | undefined;

  afterEach(() => {
    if (root && container) {
      act(() => root?.unmount());
      container.remove();
    }
    root = undefined;
    container = undefined;
  });

  it("shows a loading state before navigation data arrives", () => {
    const rendered = renderSidebar({ nodes: [], loading: true });

    expect(rendered.textContent).toContain("Loading documents…");
  });

  it("renders documents with status and recent metadata", () => {
    const rendered = renderSidebar({ selectedId: "roadmap" });
    const documentButton = rendered.querySelector<HTMLButtonElement>(
      '[data-document-id="roadmap"]',
    );

    expect(documentButton?.getAttribute("aria-current")).toBe("page");
    expect(documentButton?.textContent).toContain("Roadmap");
    expect(documentButton?.textContent).toContain("polished");
    expect(documentButton?.textContent).toContain("Edited");
  });

  it("shows recent documents and opens them from the recent section", () => {
    const onSelect = vi.fn();
    const rendered = renderSidebar({ onSelect });
    const recentButton = rendered.querySelector<HTMLButtonElement>(
      '[data-recent-document-id="welcome"]',
    );

    expect(rendered.querySelector('[aria-label="Recent documents"]')?.textContent).toContain(
      "Welcome",
    );
    act(() => recentButton?.click());
    expect(onSelect).toHaveBeenCalledWith("welcome");
  });

  it("collapses and expands folders without losing selection behavior", () => {
    const rendered = renderSidebar();
    const folderButton = rendered.querySelector<HTMLButtonElement>(
      '[aria-controls="folder-panel-projects"]',
    );

    expect(folderButton?.getAttribute("aria-expanded")).toBe("true");
    expect(rendered.querySelector('[data-document-id="roadmap"]')).not.toBeNull();

    act(() => folderButton?.click());

    expect(folderButton?.getAttribute("aria-expanded")).toBe("false");
    expect(rendered.querySelector('[data-document-id="roadmap"]')).toBeNull();
    expect(rendered.querySelector('[data-document-id="welcome"]')).not.toBeNull();

    act(() => folderButton?.click());

    expect(folderButton?.getAttribute("aria-expanded")).toBe("true");
    expect(rendered.querySelector('[data-document-id="roadmap"]')).not.toBeNull();
  });

  it("reports the selected document ID", () => {
    const onSelect = vi.fn();
    const rendered = renderSidebar({ onSelect });
    const documentButton = rendered.querySelector<HTMLButtonElement>(
      '[data-document-id="welcome"]',
    );

    act(() => documentButton?.click());

    expect(onSelect).toHaveBeenCalledWith("welcome");
  });

  it("creates a root folder from the sidebar action", async () => {
    const onCreateFolder = vi.fn().mockResolvedValue(undefined);
    const rendered = renderSidebar({ onCreateFolder });

    await act(async () => {
      rendered.querySelector<HTMLButtonElement>("button")?.click();
    });
    const input = rendered.querySelector<HTMLInputElement>('[aria-label="Folder name"]');
    await act(async () => {
      if (!input) return;
      setInputValue(input, "Ideas");
      input.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await act(async () => {
      input
        ?.closest("form")
        ?.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    });

    expect(onCreateFolder).toHaveBeenCalledWith("Ideas");
  });

  it("renames and deletes empty folders with inline controls", async () => {
    const onRenameFolder = vi.fn().mockResolvedValue(undefined);
    const onDeleteFolder = vi.fn().mockResolvedValue(undefined);
    const rendered = renderSidebar({ onRenameFolder, onDeleteFolder });

    await act(async () => {
      rendered.querySelector<HTMLButtonElement>('[aria-label="Rename Projects"]')?.click();
    });
    const input = rendered.querySelector<HTMLInputElement>('[aria-label="Rename Projects"]');
    await act(async () => {
      if (!input) return;
      setInputValue(input, "Plans");
      input.dispatchEvent(new Event("input", { bubbles: true }));
      input
        .closest("form")
        ?.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    });

    expect(onRenameFolder).toHaveBeenCalledWith("projects", "Plans");

    const archiveDelete = rendered.querySelector<HTMLButtonElement>(
      '[aria-label="Delete Archive"]',
    );
    const emptyDelete = rendered.querySelector<HTMLButtonElement>('[aria-label="Delete Empty"]');
    await act(async () => {
      emptyDelete?.click();
    });

    expect(archiveDelete?.disabled).toBe(true);
    expect(emptyDelete?.disabled).toBe(false);
    expect(onDeleteFolder).toHaveBeenCalledWith("empty");
  });

  it("moves the selected document with an accessible folder picker", async () => {
    const onMoveDocument = vi.fn().mockResolvedValue(undefined);
    const rendered = renderSidebar({ selectedId: "welcome", onMoveDocument });
    const picker = rendered.querySelector<HTMLSelectElement>('[aria-label="Move Welcome"]');

    await act(async () => {
      if (!picker) return;
      picker.value = "projects";
      picker.dispatchEvent(new Event("change", { bubbles: true }));
    });

    expect(picker?.textContent).toContain("Projects");
    expect(onMoveDocument).toHaveBeenCalledWith("welcome", "projects");
  });

  it("keeps the folder form open and shows an error when creation fails", async () => {
    const onCreateFolder = vi.fn().mockRejectedValue(new Error("Folder name is unavailable"));
    const rendered = renderSidebar({ onCreateFolder });

    await act(async () => {
      rendered.querySelector<HTMLButtonElement>("button")?.click();
    });
    const input = rendered.querySelector<HTMLInputElement>('[aria-label="Folder name"]');
    await act(async () => {
      if (!input) return;
      setInputValue(input, "Ideas");
      input.dispatchEvent(new Event("input", { bubbles: true }));
      input
        .closest("form")
        ?.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
      await Promise.resolve();
    });

    expect(rendered.querySelector('[role="alert"]')?.textContent).toContain(
      "Folder name is unavailable",
    );
    expect(rendered.querySelector('[aria-label="Folder name"]')).not.toBeNull();
  });

  function renderSidebar(
    overrides: Partial<{
      nodes: NavigationNode[];
      recent: NavigationDocument[];
      selectedId: string | null;
      loading: boolean;
      onSelect: (id: string) => void;
      onCreateFolder: (name: string) => Promise<unknown>;
      onRenameFolder: (id: string, name: string) => Promise<unknown>;
      onDeleteFolder: (id: string) => Promise<unknown>;
      onMoveDocument: (id: string, folderId: string | null) => Promise<unknown>;
    }> = {},
  ) {
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
    act(() => {
      root?.render(
        <DocumentSidebar
          nodes={overrides.nodes ?? navigation}
          recent={overrides.recent ?? recentDocuments}
          selectedId={overrides.selectedId ?? null}
          loading={overrides.loading ?? false}
          onSelect={overrides.onSelect ?? vi.fn()}
          onCreateFolder={overrides.onCreateFolder ?? vi.fn().mockResolvedValue(undefined)}
          onRenameFolder={overrides.onRenameFolder ?? vi.fn().mockResolvedValue(undefined)}
          onDeleteFolder={overrides.onDeleteFolder ?? vi.fn().mockResolvedValue(undefined)}
          onMoveDocument={overrides.onMoveDocument ?? vi.fn().mockResolvedValue(undefined)}
        />,
      );
    });
    return container;
  }

  function setInputValue(input: HTMLInputElement, value: string) {
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
    setter?.call(input, value);
  }
});
