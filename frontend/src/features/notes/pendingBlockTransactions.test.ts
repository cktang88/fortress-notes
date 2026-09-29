import { afterEach, describe, expect, it } from "vite-plus/test";
import {
  asBlockTransaction,
  makePendingTransaction,
  readPendingTransactions,
  writePendingTransactions,
} from "./pendingBlockTransactions";

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
