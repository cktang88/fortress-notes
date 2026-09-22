import { describe, expect, it } from "vite-plus/test";
import {
  deleteSelectedBlocks,
  duplicateSelectedBlocks,
  selectedBlockIds,
  transactionPayload,
  withExpectedUpdatedAt,
} from "./BlockNoteEditor";
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

describe("transactionPayload", () => {
  it("sends the document revision with the existing operations", () => {
    const operations: BlockOperation[] = [{ operation: "update", block_id: "block-1" }];

    expect(transactionPayload(7, operations)).toEqual({
      base_revision: 7,
      operations,
    });
  });
});

describe("block selection actions", () => {
  const block = (id: string, text: string) => ({
    id,
    type: "paragraph" as const,
    props: {},
    content: [{ type: "text" as const, text, styles: {} }],
    children: [],
  });

  it("duplicates the selected blocks with fresh IDs", () => {
    const inserted = [{ id: "copy-1" }];
    const editor = {
      getSelection: () => ({ blocks: [block("one", "A")] }),
      insertBlocks: (blocks: unknown[], reference: string, placement: string) => {
        expect(blocks).toEqual([
          { type: "paragraph", props: {}, content: block("one", "A").content, children: [] },
        ]);
        expect(reference).toBe("one");
        expect(placement).toBe("after");
        return inserted;
      },
      removeBlocks: () => [],
    } as never;

    expect(duplicateSelectedBlocks(editor)).toEqual(["copy-1"]);
  });

  it("deletes the current selection and reports the removed IDs", () => {
    const removed: string[][] = [];
    const editor = {
      getSelection: () => ({ blocks: [block("one", "A"), block("two", "B")] }),
      insertBlocks: () => [],
      removeBlocks: (ids: string[]) => {
        removed.push(ids);
        return [];
      },
    } as never;

    expect(selectedBlockIds(editor)).toEqual(["one", "two"]);
    expect(deleteSelectedBlocks(editor)).toEqual(["one", "two"]);
    expect(removed).toEqual([["one", "two"]]);
  });
});
