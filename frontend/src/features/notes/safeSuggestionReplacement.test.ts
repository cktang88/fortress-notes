import { describe, expect, it } from "vite-plus/test";
import { safeSuggestionReplacement } from "./safeSuggestionReplacement";

describe("safeSuggestionReplacement", () => {
  it("replaces exact text split across runs that all have the same formatting", () => {
    const content = [
      { type: "text", text: "Current ", styles: { bold: true } },
      { type: "text", text: "fact", styles: { bold: true } },
    ];

    expect(safeSuggestionReplacement(content, "Current fact", "Updated fact"))
      .toEqual([{ type: "text", text: "Updated fact", styles: { bold: true } }]);
  });

  it("rejects stale canonical text", () => {
    expect(safeSuggestionReplacement([
      { type: "text", text: "Current fact", styles: {} },
    ], "Different fact", "Updated fact")).toBeNull();
  });

  it("rejects mixed formatting and linked inline content", () => {
    expect(safeSuggestionReplacement([
      { type: "text", text: "Current ", styles: { bold: true } },
      { type: "text", text: "fact", styles: {} },
    ], "Current fact", "Updated fact")).toBeNull();

    expect(safeSuggestionReplacement([
      { type: "text", text: "See ", styles: {} },
      { type: "link", href: "https://example.test", content: "source" },
    ], "See source", "See latest data")).toBeNull();
  });

  it("rejects non-text inline content and string-backed content", () => {
    expect(safeSuggestionReplacement([
      { type: "image", props: { url: "/image.png" } },
    ], "", "replacement")).toBeNull();
    expect(safeSuggestionReplacement("Current fact", "Current fact", "Updated fact"))
      .toBeNull();
  });
});
