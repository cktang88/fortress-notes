import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vite-plus/test";
import { blockSearchParams } from "./api";
import { SearchBar } from "./SearchBar";

describe("SearchBar", () => {
  let rendered: ReturnType<typeof renderSearchBar> | undefined;

  afterEach(() => {
    rendered?.root.unmount();
    rendered?.container.remove();
    rendered = undefined;
  });

  it("updates compact block filters in full-text mode", () => {
    const onFilters = vi.fn();
    rendered = renderSearchBar({ onFilters });
    const type = rendered.container.querySelector<HTMLSelectElement>('[aria-label="Block type"]');
    const status = rendered.container.querySelector<HTMLSelectElement>(
      '[aria-label="Document status"]',
    );

    act(() => {
      setValue(type!, "heading");
      type?.dispatchEvent(new Event("change", { bubbles: true }));
    });
    act(() => {
      setValue(status!, "polished");
      status?.dispatchEvent(new Event("change", { bubbles: true }));
    });
    expect(onFilters).toHaveBeenNthCalledWith(1, { blockType: "heading" });
    expect(onFilters).toHaveBeenNthCalledWith(2, { status: "polished" });
    expect(rendered.container.querySelector('[aria-label="Tag"]')).toBeNull();
  });

  it("keeps block filters out of embedding search", () => {
    rendered = renderSearchBar({ mode: "embedding" });

    expect(rendered.container.querySelector('[aria-label="Block search filters"]')).toBeNull();
  });

  it("starts a new note without passing the click event to its callback", () => {
    const onNew = vi.fn();
    rendered = renderSearchBar({ onNew });
    act(() => {
      [...rendered!.container.querySelectorAll("button")]
        .find((button) => button.textContent === "+ New")
        ?.click();
    });

    expect(onNew.mock.calls).toEqual([[]]);
  });

  it("opens help with folder, selection, and automatic link tips", () => {
    const dialogPrototype = HTMLDialogElement.prototype;
    const originalShowModal = Object.getOwnPropertyDescriptor(dialogPrototype, "showModal");
    const showModal = vi.fn(function (this: HTMLDialogElement) {
      this.setAttribute("open", "");
    });
    Object.defineProperty(dialogPrototype, "showModal", { configurable: true, value: showModal });
    rendered = renderSearchBar();
    act(() => {
      rendered?.container.querySelector<HTMLButtonElement>('[aria-label="Help and tips"]')?.click();
    });

    expect(showModal).toHaveBeenCalledOnce();
    expect(rendered.container.querySelector("dialog")?.textContent).toMatch(/highlight text/i);
    expect(rendered.container.textContent).toMatch(/checked automatically/i);
    expect(rendered.container.textContent).toMatch(/right-click/i);
    if (originalShowModal) Object.defineProperty(dialogPrototype, "showModal", originalShowModal);
    else delete (dialogPrototype as { showModal?: unknown }).showModal;
  });
});

describe("blockSearchParams", () => {
  it("serializes active filters and omits empty values", () => {
    expect(
      blockSearchParams("road map", {
        blockType: "heading",
        status: "polished",
        tag: "  work  ",
        updatedAfter: "2026-01-01T00:00:00Z",
      }),
    ).toBe(
      "q=road+map&limit=50&block_type=heading&status=polished&tag=work&updated_after=2026-01-01T00%3A00%3A00Z",
    );
  });
});

function renderSearchBar(overrides: Partial<React.ComponentProps<typeof SearchBar>> = {}): {
  container: HTMLDivElement;
  root: Root;
} {
  const rendered = document.createElement("div");
  document.body.appendChild(rendered);
  const renderedRoot = createRoot(rendered);
  act(() => {
    renderedRoot.render(
      <SearchBar
        query=""
        onQuery={vi.fn()}
        mode="text"
        onMode={vi.fn()}
        filters={{}}
        onFilters={vi.fn()}
        onNew={vi.fn()}
        {...overrides}
      />,
    );
  });
  return { container: rendered, root: renderedRoot };
}

function setValue(element: HTMLInputElement | HTMLSelectElement, value: string) {
  const prototype =
    element instanceof HTMLSelectElement ? HTMLSelectElement.prototype : HTMLInputElement.prototype;
  Object.getOwnPropertyDescriptor(prototype, "value")?.set?.call(element, value);
}
