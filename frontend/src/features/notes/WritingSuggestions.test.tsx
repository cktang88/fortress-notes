import { act } from "react";
import { createRoot } from "react-dom/client";
import { describe, expect, it, vi } from "vite-plus/test";
import { WritingSuggestions } from "./WritingSuggestions";

const suggestion = {
  note: {
    id: "n1",
    title: "Diet research",
    status: "rough" as const,
    tags: [],
    updated_at: "2026-10-01T00:00:00Z",
    snippet: "",
  },
  score: 0.4,
  matched_block_id: "b1",
  matched_block_text: "Olive oil and fish lower heart risk",
};

describe("WritingSuggestions", () => {
  it("renders nothing without suggestions", () => {
    const container = document.createElement("div");
    const root = createRoot(container);
    act(() =>
      root.render(
        <WritingSuggestions
          suggestions={[]}
          onLink={vi.fn()}
          onOpen={vi.fn()}
          onDismiss={vi.fn()}
          onTurnOff={vi.fn()}
        />,
      ),
    );
    expect(container.innerHTML).toBe("");
    act(() => root.unmount());
  });

  it("links, opens, and can be turned off", () => {
    const container = document.createElement("div");
    const root = createRoot(container);
    const onLink = vi.fn();
    const onOpen = vi.fn();
    const onTurnOff = vi.fn();
    act(() =>
      root.render(
        <WritingSuggestions
          suggestions={[suggestion]}
          onLink={onLink}
          onOpen={onOpen}
          onDismiss={vi.fn()}
          onTurnOff={onTurnOff}
        />,
      ),
    );
    const button = (label: string) =>
      [...container.querySelectorAll("button")].find((b) => b.textContent === label)!;
    act(() => button("Link").click());
    act(() => button("Open").click());
    act(() => button("Turn off").click());
    expect(onLink).toHaveBeenCalledWith(suggestion);
    expect(onOpen).toHaveBeenCalledWith("n1", "b1");
    expect(onTurnOff).toHaveBeenCalled();
    act(() => root.unmount());
  });
});
