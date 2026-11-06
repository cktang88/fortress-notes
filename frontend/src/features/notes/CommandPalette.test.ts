import { describe, expect, it, vi } from "vite-plus/test";
import { buildItems } from "./CommandPalette";

const note = (id: string, title: string) => ({
  id,
  title,
  status: "rough" as const,
  tags: [],
  updated_at: "2026-10-01T00:00:00Z",
  snippet: "",
});

function labels(query: string, titles = ["Garden plan", "Weekly review"]) {
  return buildItems(
    {
      open: true,
      onClose: vi.fn(),
      notes: titles.map((title, index) => note(String(index), title)),
      recentIds: ["1", "0"],
      onOpenNote: vi.fn(),
      onCreateNote: vi.fn(),
      onSearchEverywhere: vi.fn(),
      onWorkspaceAction: vi.fn(),
    },
    query,
  ).map((item) => item.label);
}

describe("command palette ranking", () => {
  it("shows recent notes, then actions, before you type", () => {
    expect(labels("").slice(0, 3)).toEqual(["Weekly review", "Garden plan", "New note"]);
  });

  it("puts matching notes first and offers to create a missing title", () => {
    expect(labels("garden")[0]).toBe("Garden plan");
    expect(labels("New ideas")[0]).toBe("Create note “New ideas”");
  });

  it("lets a plainly named command win over creating a note", () => {
    expect(labels("trash")[0]).toBe("Open Trash");
  });

  it("doesn't offer to create a note that already exists", () => {
    expect(labels("garden plan")).not.toContain("Create note “garden plan”");
  });
});
