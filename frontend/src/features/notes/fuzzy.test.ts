import { describe, expect, it } from "vite-plus/test";
import { fuzzyFilter, fuzzyScore } from "./fuzzy";

describe("fuzzy matching", () => {
  it("matches in-order letters and rejects the rest", () => {
    expect(fuzzyScore("grd", "Garden plan")).not.toBeNull();
    expect(fuzzyScore("dg", "Garden plan")).toBeNull();
    expect(fuzzyScore("", "anything")).toBe(0);
  });

  it("ranks prefixes and word starts above scattered letters", () => {
    const titles = ["Meeting notes", "Some random entry", "Weekly plan", "Plan B"];
    expect(fuzzyFilter(titles, "plan", (title) => title)).toEqual(["Plan B", "Weekly plan"]);
    expect(fuzzyFilter(titles, "mn", (title) => title)[0]).toBe("Meeting notes");
  });
});
