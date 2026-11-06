import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, describe, expect, it } from "vite-plus/test";
import { useNoteSelection } from "./useNoteSelection";

type Selection = ReturnType<typeof useNoteSelection>;

describe("useNoteSelection", () => {
  afterEach(() => window.history.replaceState(null, "", "/"));

  it("adds a history entry when a callback kept from an earlier render opens another note", () => {
    // e.g. a toast's Undo, created while note "a" was open, run after moving on to "b".
    window.history.replaceState(null, "", "/?note=a");
    let latest: Selection | undefined;
    function Probe() {
      latest = useNoteSelection();
      return null;
    }
    const root = createRoot(document.createElement("div"));
    act(() => root.render(<Probe />));
    const selectFromWhenAWasOpen = latest!.select;

    act(() => latest!.clear());
    act(() => latest!.select("b", "block-in-b"));
    act(() => latest!.setActiveBlockId("block-in-b"));
    const entries = window.history.length;

    act(() => selectFromWhenAWasOpen("a"));
    expect(latest!.selectedId).toBe("a");
    expect(window.location.search).toBe("?note=a");
    // "b" stays in history (Back returns to it) and its block isn't carried over.
    expect(window.history.length).toBe(entries + 1);
    expect(latest!.activeBlockId).toBeNull();
    act(() => root.unmount());
  });
});
