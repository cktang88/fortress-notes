import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vite-plus/test";
import { DocumentSidebar } from "./DocumentSidebar";
import type { NavigationNode } from "./DocumentSidebar";

const navigation: NavigationNode[] = [
  {
    kind: "folder",
    id: "projects",
    name: "Projects",
    children: [
      {
        kind: "document",
        id: "roadmap",
        title: "Roadmap",
        status: "polished",
        updated_at: "2026-09-21T15:00:00Z",
      },
      {
        kind: "folder",
        id: "archive",
        name: "Archive",
        children: [
          {
            kind: "document",
            id: "old-plan",
            title: "Old plan",
            status: "rough",
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

  function renderSidebar(
    overrides: Partial<{
      nodes: NavigationNode[];
      selectedId: string | null;
      loading: boolean;
      onSelect: (id: string) => void;
    }> = {},
  ) {
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
    act(() => {
      root?.render(
        <DocumentSidebar
          nodes={overrides.nodes ?? navigation}
          selectedId={overrides.selectedId ?? null}
          loading={overrides.loading ?? false}
          onSelect={overrides.onSelect ?? vi.fn()}
        />,
      );
    });
    return container;
  }
});
