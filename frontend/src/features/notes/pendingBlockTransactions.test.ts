import { afterEach, describe, expect, it } from "vite-plus/test";
import {
  asBlockTransaction,
  makePendingTransaction,
  readPendingTransactions,
  withPendingOperations,
  writePendingTransactions,
} from "./pendingBlockTransactions";
import type { BlockNode } from "./types";

describe("pending block transactions", () => {
  afterEach(() => window.localStorage.clear());

  it("keeps the same request ID and body for safe replay", () => {
    const pending = makePendingTransaction([
      { operation: "update", block_id: "block-1", text: "Saved" },
    ]);
    pending.base_revision = 3;
    writePendingTransactions("doc-1", [pending]);

    const recovered = readPendingTransactions("doc-1");
    expect(asBlockTransaction(recovered[0], 99)).toEqual({
      transaction_id: pending.transaction_id,
      base_revision: 3,
      operations: pending.operations,
    });
  });
});

function node(id: string, text: string, children: BlockNode[] = []): BlockNode {
  return {
    id,
    document_id: "doc-1",
    parent_id: null,
    position: 0,
    type: "paragraph",
    attrs: {},
    user_attrs: {},
    content: { markdown: text },
    text,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    children,
  };
}

const shape = (nodes: BlockNode[]): unknown =>
  nodes.map((item) => [
    item.id,
    item.text,
    ...(item.children.length ? [shape(item.children)] : []),
  ]);

describe("withPendingOperations", () => {
  it("shows edits still waiting to sync on top of the saved document", () => {
    const saved = [node("a", "first"), node("b", "second", [node("c", "child")])];
    const pending = [
      makePendingTransaction([
        { operation: "update", block_id: "a", text: "first edited", content: { markdown: "x" } },
        { operation: "insert", block_id: "n", parent_id: null, position: 1, text: "new" },
      ]),
      makePendingTransaction([
        { operation: "move", block_id: "c", parent_id: null, position: 0 },
        { operation: "delete", block_id: "b" },
        { operation: "set_user_attrs", block_id: "n", user_attrs: { due: "today" } },
      ]),
    ];

    const result = withPendingOperations(saved, pending);
    expect(shape(result)).toEqual([
      ["c", "child"],
      ["a", "first edited"],
      ["n", "new"],
    ]);
    expect(result[1].content).toEqual({ markdown: "x" });
    expect(result[2].user_attrs).toEqual({ due: "today" });
    // The saved document itself is left alone.
    expect(shape(saved)).toEqual([
      ["a", "first"],
      ["b", "second", [["c", "child"]]],
    ]);
  });

  it("doesn't repeat edits the server already applied", () => {
    const saved = [node("a", "first"), node("n", "new")];
    const pending = [
      makePendingTransaction([
        { operation: "insert", block_id: "n", parent_id: null, position: 1, text: "new" },
        { operation: "delete", block_id: "gone" },
      ]),
    ];
    expect(shape(withPendingOperations(saved, pending))).toEqual([
      ["a", "first"],
      ["n", "new"],
    ]);
  });
});
