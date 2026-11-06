import { act } from "react";
import { createRoot } from "react-dom/client";
import { describe, expect, it, vi } from "vite-plus/test";
import { AnswerCard } from "./AnswerCard";

const citation = (block_id: string, document_title: string) => ({
  block_id,
  document_id: `doc-${block_id}`,
  document_title,
  quote: "q",
});

describe("AnswerCard", () => {
  it("numbers each cited paragraph once and opens it on click", () => {
    const container = document.createElement("div");
    const root = createRoot(container);
    const onOpen = vi.fn();
    act(() =>
      root.render(
        <AnswerCard
          question="hotel?"
          loading={false}
          error={null}
          onOpen={onOpen}
          onDismiss={vi.fn()}
          response={{
            status: "answered",
            sources: [],
            answer: [
              { text: "You chose Alfama.", citations: [citation("a", "Lisbon trip")] },
              {
                text: "It was 3 nights.",
                citations: [citation("a", "Lisbon trip"), citation("b", "Budget")],
              },
            ],
          }}
        />,
      ),
    );
    const chips = [...container.querySelectorAll("p button")].map((b) => b.textContent);
    expect(chips).toEqual(["1", "1", "2"]);
    expect(container.querySelector("ol")?.textContent).toBe("1. Lisbon trip2. Budget");
    act(() => (container.querySelectorAll("p button")[2] as HTMLButtonElement).click());
    expect(onOpen).toHaveBeenCalledWith("doc-b", "b");
    act(() => root.unmount());
  });

  it("says plainly when the notes don't answer the question", () => {
    const container = document.createElement("div");
    const root = createRoot(container);
    act(() =>
      root.render(
        <AnswerCard
          question="x"
          loading={false}
          error={null}
          onOpen={vi.fn()}
          onDismiss={vi.fn()}
          response={{ status: "not_found", answer: [], sources: [] }}
        />,
      ),
    );
    expect(container.textContent).toContain("Your notes don't seem to answer that");
    act(() => root.unmount());
  });
});

describe("AnswerCard trace", () => {
  it("shows what extra looking it did and how long it took", () => {
    const container = document.createElement("div");
    const root = createRoot(container);
    act(() =>
      root.render(
        <AnswerCard
          question="x"
          loading={false}
          error={null}
          onOpen={vi.fn()}
          onDismiss={vi.fn()}
          response={{
            status: "timeout",
            answer: [],
            sources: [],
            steps: [
              { action: "grep", detail: "Miguel" },
              { action: "read", detail: "Kitchen reno" },
            ],
            elapsed_ms: 2950,
          }}
        />,
      ),
    );
    expect(container.textContent).toContain("That took too long to answer.");
    expect(container.textContent).toContain("Searched for “Miguel” · Read “Kitchen reno” · 3.0 s");
    act(() => root.unmount());
  });
});
