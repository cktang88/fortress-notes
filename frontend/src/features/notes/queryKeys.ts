import type { QueryClient } from "@tanstack/react-query";

/** Every query key in one place, so invalidation can't drift between call sites. */
export const queryKeys = {
  notes: ["notes"] as const,
  navigation: ["navigation"] as const,
  document: (id: string | null) => ["block-document", id] as const,
  blockSearch: ["block-search"] as const,
  search: ["search"] as const,
  backlinks: ["backlinks"] as const,
  related: ["related"] as const,
  trash: ["trash"] as const,
  backups: ["backups"] as const,
  savedSearches: ["saved-searches"] as const,
  linkChecks: (id: string | null) => ["link-checks", id] as const,
  blockReference: (blockId: string | null) => ["block-reference", blockId] as const,
};

/** Refresh every list that shows documents (after create, rename, delete, restore…). */
export function invalidateDocumentLists(qc: QueryClient) {
  for (const queryKey of [
    queryKeys.notes,
    queryKeys.navigation,
    queryKeys.blockSearch,
    queryKeys.search,
    queryKeys.backlinks,
    queryKeys.related,
    queryKeys.trash,
  ]) {
    void qc.invalidateQueries({ queryKey });
  }
}
