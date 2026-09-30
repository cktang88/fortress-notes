import { act } from "react";
import { createRoot } from "react-dom/client";
import { describe, expect, it, vi } from "vite-plus/test";
import { NoteHeader } from "./NoteHeader";

describe("NoteHeader", () => {
  it("keeps note metadata editing out of the header", () => {
    const container = document.createElement("div");
    const root = createRoot(container);
    act(() => {
      root.render(
        <NoteHeader
          note={{
            id: "note-1",
            title: "Example",
            body: "Example body",
            status: "rough",
            tags: ["work"],
            snippet: "",
            created_at: "2026-09-01T00:00:00Z",
            updated_at: "2026-09-01T00:00:00Z",
          }}
          onTitle={vi.fn()}
          onDelete={vi.fn()}
          onSetStatus={vi.fn()}
          onClarify={vi.fn()}
          checking={false}
        />,
      );
    });

    expect(container.textContent).not.toMatch(/tags|fact-check|lint/i);
    expect(container.textContent).toContain("Clarify");

    act(() => root.unmount());
  });
});
