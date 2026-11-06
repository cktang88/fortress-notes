import { afterEach, describe, expect, it, vi } from "vite-plus/test";
import { blockApi } from "./api";

const sentence = (text: string) => ({
  text,
  citations: [{ block_id: "b", document_id: "d", document_title: "Trip", quote: text }],
});

function streamOf(chunks: string[]): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder();
  return new ReadableStream({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(encoder.encode(chunk));
      controller.close();
    },
  });
}

describe("blockApi.ask", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("shows each streamed sentence as it arrives, then the final answer", async () => {
    const lines = [
      JSON.stringify({ type: "sentence", sentence: sentence("One.") }),
      JSON.stringify({ type: "step", step: { action: "grep", detail: "x" } }),
      JSON.stringify({ type: "sentence", sentence: sentence("Two.") }),
      JSON.stringify({
        type: "done",
        result: { status: "answered", answer: [sentence("One."), sentence("Two.")], sources: [] },
      }),
    ].join("\n");
    // Split mid-line so the reader has to stitch lines back together.
    const chunks = [lines.slice(0, 25), lines.slice(25, 140), lines.slice(140)];
    const fetchMock = vi.fn(
      async () =>
        new Response(streamOf(chunks), {
          headers: { "content-type": "application/x-ndjson" },
        }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const partials: string[][] = [];
    const result = await blockApi.ask("q?", (partial) =>
      partials.push(partial.answer.map((s) => s.text)),
    );
    expect(partials[0]).toEqual(["One."]);
    expect(partials.at(-1)).toEqual(["One.", "Two."]);
    expect(result.status).toBe("answered");
    expect(result.answer).toHaveLength(2);
    const init = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect((init[1].headers as Record<string, string>).Accept).toContain("application/x-ndjson");
  });

  it("still accepts a plain JSON reply", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => Response.json({ status: "not_found", answer: [], sources: [] })),
    );
    const onPartial = vi.fn();
    const result = await blockApi.ask("q?", onPartial);
    expect(result.status).toBe("not_found");
    expect(onPartial).not.toHaveBeenCalled();
  });

  it("reports an answer stream that ends without a result", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async () =>
          new Response(streamOf([JSON.stringify({ type: "sentence", sentence: sentence("A.") })]), {
            headers: { "content-type": "application/x-ndjson" },
          }),
      ),
    );
    await expect(blockApi.ask("q?")).rejects.toThrow("cut off");
  });
});
