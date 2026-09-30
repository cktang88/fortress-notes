import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vite-plus/test";
import type { NavigationDocument, NavigationNode } from "./DocumentSidebar";

const { DocumentSidebar, canDropIntoFolder } = await import("./DocumentSidebar");

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
    expect(rendered.querySelector('[data-document-row-id="roadmap"]')).not.toBeNull();

    act(() => folderButton?.click());

    expect(folderButton?.getAttribute("aria-expanded")).toBe("false");
    expect(rendered.querySelector('[data-document-row-id="roadmap"]')).toBeNull();
    expect(rendered.querySelector('[data-document-row-id="welcome"]')).not.toBeNull();

    act(() => folderButton?.click());

    expect(folderButton?.getAttribute("aria-expanded")).toBe("true");
    expect(rendered.querySelector('[data-document-row-id="roadmap"]')).not.toBeNull();
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

  it("creates a root folder from the sidebar context menu", async () => {
    const onCreateFolder = vi.fn().mockResolvedValue(undefined);
    const rendered = renderSidebar({ onCreateFolder });

    await act(async () => {
      contextMenu(rendered.querySelector('[aria-label="Documents"]'));
    });
    await act(async () => menuItem(rendered, "New folder")?.click());
    const input = rendered.querySelector<HTMLInputElement>('[aria-label^="Folder name"]');
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

    expect(onCreateFolder).toHaveBeenCalledWith("Ideas", null);
  });

  it("creates a nested folder from a folder context menu", async () => {
    const onCreateFolder = vi.fn().mockResolvedValue(undefined);
    const rendered = renderSidebar({ onCreateFolder });

    await act(async () => contextMenu(rendered.querySelector('[data-folder-id="projects"]')));
    await act(async () => menuItem(rendered, "New folder")?.click());
    const input = rendered.querySelector<HTMLInputElement>('[aria-label^="Folder name"]');
    await act(async () => {
      if (!input) return;
      setInputValue(input, "Planning");
      input.dispatchEvent(new Event("input", { bubbles: true }));
      input
        .closest("form")
        ?.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    });

    expect(onCreateFolder).toHaveBeenCalledWith("Planning", "projects");
  });

  it("opens a note creation action for a folder and root", async () => {
    const onCreateNote = vi.fn().mockResolvedValue(undefined);
    const rendered = renderSidebar({ onCreateNote });

    await act(async () => contextMenu(rendered.querySelector('[data-folder-id="archive"]')));
    await act(async () => menuItem(rendered, "New note")?.click());
    expect(onCreateNote).toHaveBeenLastCalledWith("archive");

    await act(async () => contextMenu(rendered.querySelector('[aria-label="Documents"]')));
    await act(async () => menuItem(rendered, "New note")?.click());
    expect(onCreateNote).toHaveBeenLastCalledWith(null);
  });

  it("supports renaming a recent note and keyboard context menu controls", async () => {
    const onRenameDocument = vi.fn().mockResolvedValue(undefined);
    const rendered = renderSidebar({ onRenameDocument });
    const recentButton = rendered.querySelector<HTMLButtonElement>(
      '[data-recent-document-id="welcome"]',
    );

    await act(async () => {
      recentButton?.dispatchEvent(new MouseEvent("dblclick", { bubbles: true }));
    });
    const input = rendered.querySelector<HTMLInputElement>('[aria-label="Rename Welcome"]');
    await act(async () => {
      if (!input) return;
      setInputValue(input, "Welcome guide");
      input.dispatchEvent(new Event("input", { bubbles: true }));
      input
        .closest("form")
        ?.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    });
    expect(onRenameDocument).toHaveBeenCalledWith("welcome", "Welcome guide");
    expect(rendered.querySelector('[aria-label="Rename Welcome"]')).toBeNull();

    const folderButton = rendered.querySelector<HTMLButtonElement>(
      '[aria-label="Folder: Projects"]',
    );
    await act(async () => {
      folderButton?.dispatchEvent(
        new KeyboardEvent("keydown", { key: "ContextMenu", bubbles: true }),
      );
    });
    expect(rendered.querySelector('[role="menu"]')).not.toBeNull();
    await act(async () => {
      document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    });
    expect(rendered.querySelector('[role="menu"]')).toBeNull();
    expect(document.activeElement).toBe(folderButton);
  });

  it("renames folders and notes on double click and supports cancel", async () => {
    const onRenameFolder = vi.fn().mockResolvedValue(undefined);
    const onRenameDocument = vi.fn().mockResolvedValue(undefined);
    const rendered = renderSidebar({ onRenameFolder, onRenameDocument });

    await act(async () => {
      doubleClick(rendered.querySelector('[data-folder-id="projects"]'));
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

    await act(async () => {
      doubleClick(rendered.querySelector('[data-document-id="roadmap"]'));
    });
    const noteInput = rendered.querySelector<HTMLInputElement>('[aria-label="Rename Roadmap"]');
    await act(async () => {
      if (!noteInput) return;
      setInputValue(noteInput, "Product roadmap");
      noteInput.dispatchEvent(new Event("input", { bubbles: true }));
      noteInput
        .closest("form")
        ?.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    });
    expect(onRenameDocument).toHaveBeenCalledWith("roadmap", "Product roadmap");

    await act(async () => doubleClick(rendered.querySelector('[data-folder-id="empty"]')));
    const emptyInput = rendered.querySelector<HTMLInputElement>('[aria-label="Rename Empty"]');
    const cancel = [
      ...(emptyInput?.closest("form")?.querySelectorAll<HTMLButtonElement>("button") ?? []),
    ].find((button) => button.textContent?.trim() === "Cancel");
    await act(async () => cancel?.click());
    expect(rendered.querySelector('[aria-label="Rename Empty"]')).toBeNull();

    await act(async () => doubleClick(rendered.querySelector('[data-folder-id="empty"]')));
    const escapeInput = rendered.querySelector<HTMLInputElement>('[aria-label="Rename Empty"]');
    await act(async () => {
      escapeInput?.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    });
    expect(rendered.querySelector('[aria-label="Rename Empty"]')).toBeNull();
  });

  it("allows valid folder drops and rejects self, current-parent, and descendant drops", () => {
    expect(
      canDropIntoFolder(navigation, { kind: "document", id: "welcome", parentId: null }, "empty"),
    ).toBe(true);
    expect(
      canDropIntoFolder(
        navigation,
        { kind: "folder", id: "archive", parentId: "projects" },
        "empty",
      ),
    ).toBe(true);
    expect(
      canDropIntoFolder(navigation, { kind: "folder", id: "projects", parentId: null }, "projects"),
    ).toBe(false);
    expect(
      canDropIntoFolder(
        navigation,
        { kind: "document", id: "roadmap", parentId: "projects" },
        "projects",
      ),
    ).toBe(false);
    expect(
      canDropIntoFolder(navigation, { kind: "folder", id: "projects", parentId: null }, "archive"),
    ).toBe(false);
  });

  it("closes a context menu on an outside click", async () => {
    const rendered = renderSidebar();
    await act(async () => contextMenu(rendered.querySelector('[data-folder-id="empty"]')));
    expect(rendered.querySelector('[role="menu"]')).not.toBeNull();
    await act(async () =>
      rendered
        .querySelector('[data-folder-id="projects"]')
        ?.dispatchEvent(new PointerEvent("pointerdown", { bubbles: true })),
    );
    expect(rendered.querySelector('[role="menu"]')).toBeNull();
  });

  it("keeps the folder form open and shows an error when creation fails", async () => {
    const onCreateFolder = vi.fn().mockRejectedValue(new Error("Folder name is unavailable"));
    const rendered = renderSidebar({ onCreateFolder });

    await act(async () => {
      contextMenu(rendered.querySelector('[aria-label="Documents"]'));
    });
    await act(async () => menuItem(rendered, "New folder")?.click());
    const input = rendered.querySelector<HTMLInputElement>('[aria-label^="Folder name"]');
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
    expect(rendered.querySelector('[aria-label^="Folder name"]')).not.toBeNull();
  });

  function renderSidebar(
    overrides: Partial<{
      nodes: NavigationNode[];
      recent: NavigationDocument[];
      selectedId: string | null;
      loading: boolean;
      onSelect: (id: string) => void;
      onCreateFolder: (name: string, parentId: string | null) => Promise<unknown>;
      onRenameFolder: (id: string, name: string) => Promise<unknown>;
      onDeleteFolder: (id: string) => Promise<unknown>;
      onMoveDocument: (id: string, folderId: string | null) => Promise<unknown>;
      onCreateNote: (folderId: string | null) => Promise<unknown>;
      onDeleteDocument: (id: string) => Promise<unknown>;
      onRenameDocument: (id: string, title: string) => Promise<unknown>;
      onMoveFolder: (id: string, parentId: string | null) => Promise<unknown>;
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
          onCreateNote={overrides.onCreateNote ?? vi.fn().mockResolvedValue(undefined)}
          onRenameFolder={overrides.onRenameFolder ?? vi.fn().mockResolvedValue(undefined)}
          onRenameDocument={overrides.onRenameDocument ?? vi.fn().mockResolvedValue(undefined)}
          onDeleteDocument={overrides.onDeleteDocument ?? vi.fn().mockResolvedValue(undefined)}
          onDeleteFolder={overrides.onDeleteFolder ?? vi.fn().mockResolvedValue(undefined)}
          onMoveDocument={overrides.onMoveDocument ?? vi.fn().mockResolvedValue(undefined)}
          onMoveFolder={overrides.onMoveFolder ?? vi.fn().mockResolvedValue(undefined)}
        />,
      );
    });
    return container;
  }

  function setInputValue(input: HTMLInputElement, value: string) {
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
    setter?.call(input, value);
  }

  function contextMenu(element: Element | null) {
    const target = element?.querySelector("button") ?? element;
    target?.dispatchEvent(new MouseEvent("contextmenu", { bubbles: true, cancelable: true }));
  }

  function doubleClick(element: Element | null) {
    const target = element?.querySelector("button") ?? element;
    target?.dispatchEvent(new MouseEvent("dblclick", { bubbles: true }));
  }

  function menuItem(parent: ParentNode, name: string) {
    return [...parent.querySelectorAll<HTMLButtonElement>('[role="menuitem"]')].find(
      (item) => item.textContent?.trim() === name,
    );
  }
});
