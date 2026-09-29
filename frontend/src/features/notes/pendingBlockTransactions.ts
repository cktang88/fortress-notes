import type { BlockOperation, BlockTransaction } from "./types";

export interface PendingBlockTransaction {
  transaction_id: string;
  base_revision?: number;
  operations: BlockOperation[];
}

function storageKey(documentId: string) {
  return `fortress-notes:pending-block-transactions:${documentId}`;
}

export function readPendingTransactions(documentId: string): PendingBlockTransaction[] {
  const raw = window.localStorage.getItem(storageKey(documentId));
  if (!raw) return [];
  const parsed: unknown = JSON.parse(raw);
  if (!Array.isArray(parsed)) throw new Error("Saved edits could not be read safely");
  return parsed as PendingBlockTransaction[];
}

export function writePendingTransactions(
  documentId: string,
  pending: PendingBlockTransaction[],
) {
  const key = storageKey(documentId);
  if (pending.length === 0) window.localStorage.removeItem(key);
  else window.localStorage.setItem(key, JSON.stringify(pending));
}

export function makePendingTransaction(operations: BlockOperation[]): PendingBlockTransaction {
  return { transaction_id: crypto.randomUUID(), operations };
}

export function asBlockTransaction(
  pending: PendingBlockTransaction,
  base_revision: number,
): BlockTransaction {
  return {
    transaction_id: pending.transaction_id,
    base_revision: pending.base_revision ?? base_revision,
    operations: pending.operations,
  };
}
