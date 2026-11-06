import type { BlockNode, BlockOperation, BlockTransaction } from "./types";

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

export function writePendingTransactions(documentId: string, pending: PendingBlockTransaction[]) {
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

/**
 * The document as it will be once the edits still waiting on this device are saved,
 * so a note reopened before they sync shows them (and new edits build on them).
 * Edits the server may already have applied are skipped rather than repeated.
 */
export function withPendingOperations(
  nodes: readonly BlockNode[],
  pending: readonly PendingBlockTransaction[],
): BlockNode[] {
  const root = cloneTree(nodes);
  const siblingsOf = (parentId: string | null | undefined) =>
    parentId ? findNode(root, parentId)?.node.children : root;
  for (const operation of pending.flatMap((transaction) => transaction.operations)) {
    const found = operation.block_id ? findNode(root, operation.block_id) : undefined;
    if (operation.operation === "insert") {
      const siblings = siblingsOf(operation.parent_id);
      if (!operation.block_id || found || !siblings) continue;
      siblings.splice(operation.position ?? siblings.length, 0, {
        id: operation.block_id,
        document_id: "",
        parent_id: operation.parent_id ?? null,
        position: 0,
        type: operation.type ?? "paragraph",
        attrs: operation.attrs ?? {},
        user_attrs: operation.user_attrs ?? {},
        content: operation.content ?? {},
        text: operation.text ?? "",
        created_at: "",
        updated_at: "",
        children: [],
      });
      continue;
    }
    if (!found) continue;
    const { node, siblings } = found;
    if (operation.operation === "update") {
      if (operation.type !== undefined) node.type = operation.type;
      if (operation.attrs !== undefined) node.attrs = operation.attrs;
      if (operation.content !== undefined) node.content = operation.content;
      if (operation.text !== undefined) node.text = operation.text;
    } else if (operation.operation === "set_attrs") {
      node.attrs = operation.attrs ?? {};
    } else if (operation.operation === "set_user_attrs") {
      node.user_attrs = operation.user_attrs ?? {};
    } else if (operation.operation === "delete") {
      siblings.splice(siblings.indexOf(node), 1);
    } else if (operation.operation === "move") {
      const parentId = operation.parent_id ?? null;
      if (parentId === node.id || (parentId && findNode(node.children, parentId))) continue;
      const target = siblingsOf(parentId);
      if (!target) continue;
      siblings.splice(siblings.indexOf(node), 1);
      node.parent_id = parentId;
      target.splice(operation.position ?? target.length, 0, node);
    }
  }
  return root;
}

function cloneTree(nodes: readonly BlockNode[]): BlockNode[] {
  return nodes.map((node) => ({ ...node, children: cloneTree(node.children) }));
}

function findNode(
  nodes: BlockNode[],
  id: string,
): { node: BlockNode; siblings: BlockNode[] } | undefined {
  for (const node of nodes) {
    if (node.id === id) return { node, siblings: nodes };
    const nested = findNode(node.children, id);
    if (nested) return nested;
  }
  return undefined;
}
