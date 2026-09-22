import { describe, expect, it } from "vite-plus/test";
import { documentLink, matchingLinkTargets } from "./linkSuggestions";
import type { NoteSummary } from "./types";

const notes: NoteSummary[] = [
  { id: "zeta", title: "Zeta", status: "rough", tags: [], updated_at: "", snippet: "" },
  { id: "alpha", title: "Alpha", status: "rough", tags: [], updated_at: "", snippet: "" },
  {
    id: "project-plan",
    title: "Project Plan",
    status: "polished",
    tags: [],
    updated_at: "",
    snippet: "",
  },
];

describe("document link suggestions", () => {
  it("prioritizes title prefixes and limits the menu", () => {
    expect(matchingLinkTargets(notes, "p", 2).map((note) => note.title)).toEqual([
      "Project Plan",
      "Alpha",
    ]);
  });

  it("matches document IDs as well as titles", () => {
    expect(matchingLinkTargets(notes, "project-").map((note) => note.id)).toEqual(["project-plan"]);
  });

  it("serializes a stable ID with a readable label", () => {
    expect(documentLink({ id: "alpha", title: "Alpha | Draft]" })).toBe("[[alpha|Alpha Draft]]");
  });
});
