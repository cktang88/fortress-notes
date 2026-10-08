import { act } from "react";
import { createRoot } from "react-dom/client";
import { describe, expect, it, vi } from "vite-plus/test";
import { BlockSearchResults } from "./BlockSearchResults";

describe("BlockSearchResults", () => {
  it("highlights matched words and explains why each result matched", () => {
    const container = document.createElement("div");
    const root = createRoot(container);
    const onSelect = vi.fn();
    act(() =>
      root.render(
        <BlockSearchResults
          loading={false}
          onSelect={onSelect}
          results={[
            {
              block_id: "b1",
              document_id: "d1",
              document_title: "Lisbon trip",
              block_type: "paragraph",
              text: "…book the hotel near Alfama",
              highlights: [[10, 15]],
              matched: ["words", "title"],
              score: 1,
            },
          ]}
        />,
      ),
    );
    expect(container.querySelector("mark")?.textContent).toBe("hotel");
    expect(container.textContent).toContain("exact words · in title");
    act(() => container.querySelector("button")?.click());
    expect(onSelect).toHaveBeenCalledWith("d1", "b1");
    act(() => root.unmount());
  });
});
