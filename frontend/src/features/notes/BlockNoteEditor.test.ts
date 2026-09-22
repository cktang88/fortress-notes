import { describe, expect, it } from "vite-plus/test";
import { withExpectedUpdatedAt } from "./BlockNoteEditor";
import type { BlockOperation } from "./types";

describe("withExpectedUpdatedAt", () => {
  it("checks only the first non-insert operation for each known block", () => {
    const operations: BlockOperation[] = [
      { operation: "insert", block_id: "new-block" },
      { operation: "move", block_id: "known-block", position: 1 },
      { operation: "update", block_id: "known-block", text: "Changed" },
      { operation: "delete", block_id: "other-block" },
    ];

    expect(
      withExpectedUpdatedAt(
        operations,
        new Map([
          ["known-block", "2026-09-22T12:00:00Z"],
          ["other-block", "2026-09-22T12:01:00Z"],
        ]),
      ),
    ).toEqual([
      { operation: "insert", block_id: "new-block" },
      {
        operation: "move",
        block_id: "known-block",
        position: 1,
        expected_updated_at: "2026-09-22T12:00:00Z",
      },
      { operation: "update", block_id: "known-block", text: "Changed" },
      {
        operation: "delete",
        block_id: "other-block",
        expected_updated_at: "2026-09-22T12:01:00Z",
      },
    ]);
  });
});
