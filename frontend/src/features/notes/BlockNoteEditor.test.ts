import { describe, expect, it } from "vite-plus/test";
import {
  buildBlockPermalink,
  convertSelectedBlock,
  deleteSelectedBlocks,
  duplicateSelectedBlocks,
  insertParagraphAbove,
  isBlockLevelSelection,
  moveSelectedBlocks,
  selectedBlockIds,
  transactionPayload,
  toPartialBlock,
  withExpectedUpdatedAt,
  mergeOperations,
} from "./BlockNoteEditor";
import type { BlockNode, BlockOperation } from "./types";

describe("buildBlockPermalink", () => {
  it("preserves the app URL and includes document and block IDs", () => {
    expect(buildBlockPermalink("http://localhost:5173/?mode=edit", "doc 1", "block/2")).toBe(
      "http://localhost:5173/?mode=edit&note=doc+1#block=block%2F2",
    );
  });
});

describe("isBlockLevelSelection", () => {
  it("recognizes node and drag-handle block selections, not text ranges", () => {
    expect(isBlockLevelSelection({ toJSON: () => ({ type: "node" }) })).toBe(true);
    expect(isBlockLevelSelection({ toJSON: () => ({ type: "multiple-node" }) })).toBe(true);
    expect(isBlockLevelSelection({ toJSON: () => ({ type: "text" }) })).toBe(false);
    expect(isBlockLevelSelection({ toJSON: () => null })).toBe(false);
  });
});

describe("toPartialBlock", () => {
  it("restores callout blocks with their rich inline content", () => {
    const inlineContent = [{ type: "text", text: "Remember", styles: { bold: true } }];
    const callout: BlockNode = {
      id: "callout-id",
      document_id: "document-id",
      type: "callout",
      parent_id: null,
      position: 0,
      attrs: {},
      user_attrs: {},
      content: { blocknote: inlineContent, markdown: "Remember" },
      text: "Remember",
      created_at: "2026-01-01T00:00:00Z",
      updated_at: "2026-01-01T00:00:00Z",
      children: [],
    };

    expect(toPartialBlock(callout)).toEqual({
      id: "callout-id",
      type: "callout",
      content: inlineContent,
      children: [],
    });
  });

  it("restores math placeholder blocks with their custom type", () => {
    const math: BlockNode = {
      id: "math-id",
      document_id: "document-id",
      type: "mathPlaceholder",
      parent_id: null,
      position: 0,
      attrs: {},
      user_attrs: {},
      content: { markdown: "Math placeholder", blocknote: [] },
      text: "Math placeholder",
      created_at: "2026-01-01T00:00:00Z",
      updated_at: "2026-01-01T00:00:00Z",
      children: [],
    };

    expect(toPartialBlock(math)).toEqual({
      id: "math-id",
      type: "mathPlaceholder",
      children: [],
    });
  });

  it("restores lists, tasks and code saved by the editor with their own type", () => {
    const content = [{ type: "text", text: "Renew passport", styles: {} }];
    const task: BlockNode = {
      id: "task-id",
      document_id: "document-id",
      type: "checkListItem",
      parent_id: null,
      position: 0,
      attrs: { checked: true, textColor: "default", id: "stray" },
      user_attrs: {},
      content: { blocknote: content, markdown: "- [x] Renew passport\n" },
      text: "Renew passport",
      created_at: "2026-01-01T00:00:00Z",
      updated_at: "2026-01-01T00:00:00Z",
      children: [],
    };

    expect(toPartialBlock(task)).toEqual({
      id: "task-id",
      type: "checkListItem",
      props: { checked: true, textColor: "default" },
      content,
      children: [],
    });
    expect(toPartialBlock({ ...task, type: "bulletListItem", attrs: {} }).type).toBe(
      "bulletListItem",
    );
    expect(toPartialBlock({ ...task, type: "codeBlock", attrs: { language: "ts" } })).toMatchObject(
      { type: "codeBlock", props: { language: "ts" } },
    );
  });

  it("restores saved image blocks and their props", () => {
    const image: BlockNode = {
      id: "image-id",
      document_id: "document-id",
      type: "image",
      parent_id: null,
      position: 0,
      attrs: { id: "incorrect-id", url: "/api/images/image-id", caption: "Map" },
      user_attrs: {},
      content: { blocknote: [] },
      text: "",
      created_at: "2026-01-01T00:00:00Z",
      updated_at: "2026-01-01T00:00:00Z",
      children: [],
    };

    expect(toPartialBlock(image)).toEqual({
      id: "image-id",
      type: "image",
      props: { url: "/api/images/image-id", caption: "Map" },
      children: [],
    });
  });

  it("restores blocks saved by the editor with their own type, props and rich content", () => {
    const saved = (
      type: string,
      attrs: Record<string, unknown>,
      blocknote: unknown,
      markdown: string,
    ): BlockNode => ({
      id: `${type}-id`,
      document_id: "document-id",
      type,
      parent_id: null,
      position: 0,
      attrs,
      user_attrs: {},
      content: { blocknote, markdown },
      text: "",
      created_at: "2026-01-01T00:00:00Z",
      updated_at: "2026-01-01T00:00:00Z",
      children: [],
    });
    const rich = [{ type: "text", text: "Item", styles: { bold: true } }];

    expect(
      toPartialBlock(saved("bulletListItem", { textAlignment: "left" }, rich, "* **Item**")),
    ).toEqual({
      id: "bulletListItem-id",
      type: "bulletListItem",
      props: { textAlignment: "left" },
      content: rich,
      children: [],
    });
    expect(
      toPartialBlock(saved("checkListItem", { checked: true }, rich, "[x] **Item**")),
    ).toMatchObject({
      type: "checkListItem",
      props: { checked: true },
      content: rich,
    });
    expect(toPartialBlock(saved("numberedListItem", {}, rich, "1. **Item**"))).toMatchObject({
      type: "numberedListItem",
      content: rich,
    });
    expect(
      toPartialBlock(saved("codeBlock", { language: "ts" }, rich, "```ts\nItem\n```")),
    ).toMatchObject({
      type: "codeBlock",
      props: { language: "ts" },
      content: rich,
    });
    expect(toPartialBlock(saved("heading", { level: 2 }, rich, "## **Item**"))).toMatchObject({
      type: "heading",
      props: { level: 2 },
      content: rich,
    });
    expect(toPartialBlock(saved("divider", {}, undefined, "***"))).toEqual({
      id: "divider-id",
      type: "divider",
      children: [],
    });
  });

  it("restores saved file blocks without changing their type", () => {
    const file: BlockNode = {
      id: "file-id",
      document_id: "document-id",
      type: "file",
      parent_id: null,
      position: 0,
      attrs: { url: "/media/file.bin", name: "report.html" },
      user_attrs: {},
      content: {},
      text: "",
      created_at: "2026-01-01T00:00:00Z",
      updated_at: "2026-01-01T00:00:00Z",
      children: [],
    };

    expect(toPartialBlock(file)).toEqual({
      id: "file-id",
      type: "file",
      props: { url: "/media/file.bin", name: "report.html" },
      children: [],
    });
  });
});

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

  it("inserts a paragraph before the selected block", () => {
    const editor = {
      getSelection: () => ({ blocks: [block("one", "A")] }),
      insertBlocks: (blocks: unknown[], reference: string, placement: string) => {
        expect(blocks).toEqual([{ type: "paragraph" }]);
        expect(reference).toBe("one");
        expect(placement).toBe("before");
        return [{ id: "new" }];
      },
      removeBlocks: () => [],
      updateBlock: () => block("one", "A"),
    } as never;

    expect(insertParagraphAbove(editor)).toBe("new");
  });

  it("converts the selected block to a supported BlockNote type", () => {
    const updates: unknown[] = [];
    const editor = {
      getSelection: () => ({ blocks: [block("one", "A")] }),
      insertBlocks: () => [],
      removeBlocks: () => [],
      updateBlock: (id: string, update: unknown) => {
        updates.push([id, update]);
        return block("one", "A");
      },
    } as never;

    convertSelectedBlock(editor, "quote");
    expect(updates).toEqual([["one", { type: "quote" }]]);
  });

  it("moves a selected block in the requested direction", () => {
    const moved: string[] = [];
    const editor = {
      getSelection: () => ({ blocks: [block("one", "A")] }),
      insertBlocks: () => [],
      removeBlocks: () => [],
      updateBlock: () => block("one", "A"),
      moveBlocksUp: () => moved.push("up"),
      moveBlocksDown: () => moved.push("down"),
    } as never;

    moveSelectedBlocks(editor, "down");
    expect(moved).toEqual(["down"]);
  });
});

describe("mergeOperations", () => {
  it("collapses consecutive updates to one block and keeps everything else in order", () => {
    const merged = mergeOperations(
      [
        { operation: "insert", block_id: "a", text: "" },
        { operation: "update", block_id: "a", text: "h", type: "paragraph" },
      ],
      [
        { operation: "update", block_id: "a", text: "hi" },
        { operation: "update", block_id: "b", text: "x" },
        { operation: "update", block_id: "a", text: "hi!" },
      ],
    );
    expect(merged).toEqual([
      { operation: "insert", block_id: "a", text: "" },
      { operation: "update", block_id: "a", text: "hi", type: "paragraph" },
      { operation: "update", block_id: "b", text: "x" },
      { operation: "update", block_id: "a", text: "hi!" },
    ]);
  });
});
