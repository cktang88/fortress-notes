import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vite-plus/test";
import { SeenBefore } from "./SeenBefore";

const related = {
  note: {
    id: "n1",
    title: "Sourdough notes",
    status: "rough" as const,
    tags: [],
    updated_at: "2026-10-01T00:00:00Z",
    snippet: "",
  },
  score: 0.4,
  matched_block_id: "b1",
  matched_block_text: "Feed the starter twice a day",
};
const duplicate = {
  document_id: "d1",
  title: "Kitchen reno",
  updated_at: "2026-10-01T00:00:00Z",
  overlap: 0.72,
};

let root: Root | undefined;
let container: HTMLDivElement | undefined;
afterEach(() => {
  if (root) act(() => root?.unmount());
  container?.remove();
});

function render(overrides: Partial<React.ComponentProps<typeof SeenBefore>> = {}) {
  const props = {
    related: [],
    duplicate: null,
    suggestionsOn: true,
    onToggleSuggestions: vi.fn(),
    onLink: vi.fn(),
    onOpen: vi.fn(),
    onMoveInto: vi.fn(),
    onNotDuplicate: vi.fn(),
    moving: false,
    ...overrides,
  };
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  act(() => root?.render(<SeenBefore {...props} />));
  return props;
}

const button = (label: string | RegExp) =>
  [...container!.querySelectorAll("button")].find((b) =>
    typeof label === "string" ? b.textContent === label : label.test(b.textContent ?? ""),
  )!;

describe("SeenBefore", () => {
  it("shows nothing when there is nothing to say", () => {
    render();
    expect(container!.textContent).toBe("");
  });

  it("is a quiet count until clicked, then offers Link and Open", () => {
    const props = render({ related: [related] });
    expect(button("↗ 1 related")).toBeTruthy();
    expect(container!.querySelector('[role="dialog"]')).toBeNull();
    act(() => button("↗ 1 related").click());
    expect(container!.textContent).toContain("Feed the starter twice a day");
    act(() => button("Link").click());
    expect(props.onLink).toHaveBeenCalledWith(related);
    expect(container!.querySelector('[role="dialog"]')).toBeNull();
  });

  it("puts a near-copy first and confirms before moving", () => {
    const props = render({ related: [related], duplicate });
    act(() => button(/Looks like “Kitchen reno”/).click());
    expect(container!.textContent).toContain("72% of its words are already there");
    act(() => button("Move this into it…").click());
    act(() => button("Move").click());
    expect(props.onMoveInto).toHaveBeenCalledWith(duplicate);
  });

  it("stays reachable when suggestions are off so they can be turned back on", () => {
    const props = render({ suggestionsOn: false });
    act(() => button("↗").click());
    const toggle = container!.querySelector<HTMLInputElement>('input[type="checkbox"]')!;
    expect(toggle.checked).toBe(false);
    act(() => toggle.click());
    expect(props.onToggleSuggestions).toHaveBeenCalledWith(true);
  });
});
