import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vite-plus/test";
import { NoteHeader } from "./NoteHeader";

const note = {
  id: "note-1",
  title: "Example",
  status: "rough" as const,
  created_at: "2026-09-01T00:00:00Z",
  updated_at: "2026-09-01T00:00:00Z",
};

let root: Root | undefined;
let container: HTMLDivElement | undefined;

afterEach(() => {
  if (root) act(() => root?.unmount());
  container?.remove();
  vi.useRealTimers();
});

function renderHeader(onTitle = vi.fn()) {
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  act(() => {
    root?.render(
      <NoteHeader
        note={note}
        onTitle={onTitle}
        onDelete={vi.fn()}
        onSetStatus={vi.fn()}
        onClarify={vi.fn()}
        checking={false}
      />,
    );
  });
  return container.querySelector<HTMLInputElement>('input[aria-label="Note title"]')!;
}

function type(input: HTMLInputElement, value: string) {
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!;
  act(() => {
    setter.call(input, value);
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
}

describe("NoteHeader", () => {
  it("keeps note metadata editing out of the header", () => {
    renderHeader();
    expect(container!.textContent).not.toMatch(/tags|fact-check|lint/i);
    expect(container!.textContent).toContain("Clarify");
  });

  it("shows every keystroke immediately and saves the title once typing pauses", () => {
    vi.useFakeTimers();
    const onTitle = vi.fn();
    const input = renderHeader(onTitle);
    type(input, "Ex");
    type(input, "Exa");
    type(input, "Exam ideas");
    expect(input.value).toBe("Exam ideas");
    expect(onTitle).not.toHaveBeenCalled();
    act(() => vi.advanceTimersByTime(1000));
    expect(onTitle.mock.calls).toEqual([["Exam ideas"]]);
  });

  it("saves on Enter and reverts on Escape", () => {
    const onTitle = vi.fn();
    const input = renderHeader(onTitle);
    type(input, "Draft");
    act(() => {
      input.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    });
    expect(input.value).toBe("Example");
    type(input, "Final");
    act(() => {
      input.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
    });
    expect(onTitle.mock.calls).toEqual([["Final"]]);
  });
});
