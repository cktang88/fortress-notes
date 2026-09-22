import { describe, expect, it } from "vite-plus/test";
import { editTags } from "./NoteHeader";

describe("editTags", () => {
  it("adds trimmed tags without duplicates or empty values", () => {
    expect(editTags(["work"], "  ideas  ", "add")).toEqual(["work", "ideas"]);
    expect(editTags(["work"], "work", "add")).toEqual(["work"]);
    expect(editTags(["work"], "   ", "add")).toEqual(["work"]);
  });

  it("removes only the requested tag", () => {
    expect(editTags(["work", "ideas"], " work ", "remove")).toEqual(["ideas"]);
  });
});
