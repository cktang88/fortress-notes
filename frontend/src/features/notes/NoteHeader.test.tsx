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

function renderHeader(onTitle = vi.fn(), shown: typeof note & { title_auto?: boolean } = note) {
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  const render = (current: typeof shown) =>
    act(() => {
      root?.render(
        <NoteHeader note={current} onTitle={onTitle} onDelete={vi.fn()} onSetStatus={vi.fn()} />,
      );
    });
  render(shown);
  renderNote = render;
  return container.querySelector<HTMLInputElement>('input[aria-label="Note title"]')!;
}

let renderNote: (current: typeof note & { title_auto?: boolean }) => void = () => {};

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
    expect(container!.textContent).not.toMatch(/tags|fact-check|lint|clarify/i);
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

  it("saves a title still being typed when the header goes away (Back, note switch)", () => {
    vi.useFakeTimers();
    const onTitle = vi.fn();
    const input = renderHeader(onTitle);
    act(() => input.focus());
    type(input, "Half typed");
    act(() => root?.unmount());
    root = undefined;
    expect(onTitle.mock.calls).toEqual([["Half typed"]]);
    act(() => vi.advanceTimersByTime(1000));
    expect(onTitle).toHaveBeenCalledTimes(1);
  });

  it("doesn't save a title abandoned with Escape when the header goes away", () => {
    const onTitle = vi.fn();
    const input = renderHeader(onTitle);
    type(input, "Never mind");
    act(() => {
      input.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    });
    act(() => root?.unmount());
    root = undefined;
    expect(onTitle).not.toHaveBeenCalled();
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

  it("shows the first-line title again after clearing an automatic title", () => {
    const onTitle = vi.fn();
    const input = renderHeader(onTitle, { ...note, title: "First line", title_auto: true });
    act(() => input.focus());
    type(input, "");
    act(() => input.blur());
    expect(onTitle).not.toHaveBeenCalled();
    expect(input.value).toBe("First line");
  });

  it("shows the handed-back title once the cleared field loses focus", () => {
    vi.useFakeTimers();
    const onTitle = vi.fn();
    const input = renderHeader(onTitle);
    act(() => input.focus());
    type(input, "");
    act(() => vi.advanceTimersByTime(1000));
    expect(onTitle.mock.calls).toEqual([[""]]);
    // The server hands the title back to the first line while the field still has focus.
    renderNote({ ...note, title: "First line", title_auto: true });
    expect(input.value).toBe("");
    act(() => input.blur());
    expect(input.value).toBe("First line");
  });
});
