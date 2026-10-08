import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, blockApi, notesApi, workspaceApi } from "./api";
import { invalidateDocumentLists, queryKeys } from "./queryKeys";
import type {
  BlockDocument,
  BlockSearchFilters,
  NoteStatus,
  SavedSearch,
  SearchMode,
} from "./types";

// --- Reads ---------------------------------------------------------------------

export function useNotes() {
  return useQuery({ queryKey: queryKeys.notes, queryFn: () => notesApi.list() });
}

export function useNavigation() {
  return useQuery({
    queryKey: queryKeys.navigation,
    queryFn: () => blockApi.navigation(),
    retry: false,
  });
}

export function useBlockDocument(id: string | null) {
  return useQuery({
    queryKey: queryKeys.document(id),
    queryFn: () => blockApi.get(id!),
    enabled: !!id,
    // A missing note is a real answer, not something to retry.
    retry: (count, error) => !(error instanceof ApiError && error.status === 404) && count < 2,
  });
}

export function useBlockSearch(q: string, filters: BlockSearchFilters = {}, enabled = true) {
  return useQuery({
    queryKey: [...queryKeys.blockSearch, q, filters],
    queryFn: () => blockApi.search(q, filters),
    enabled: enabled && q.trim().length > 0,
    retry: false,
    placeholderData: (previous) => previous,
  });
}

export function useSearch(q: string, mode: SearchMode, enabled = true) {
  return useQuery({
    queryKey: [...queryKeys.search, mode, q],
    queryFn: () => notesApi.search(q, mode),
    enabled: enabled && q.trim().length > 0,
    placeholderData: (previous) => previous,
  });
}

export function useBacklinks(id: string | null) {
  return useQuery({
    queryKey: [...queryKeys.backlinks, id],
    queryFn: () => blockApi.backlinks(id!),
    enabled: !!id,
  });
}

export function useRelated(id: string | null, blockId: string | null = null) {
  return useQuery({
    queryKey: [...queryKeys.related, id, blockId],
    queryFn: () => notesApi.related(id!, 5, blockId),
    enabled: !!id,
    // Keep showing the last results while the focused block changes.
    placeholderData: (previous) => previous,
  });
}

export function useBlockReference(blockId: string | null) {
  return useQuery({
    queryKey: queryKeys.blockReference(blockId),
    queryFn: async () =>
      (await blockApi.linkTargets(blockId!, 1)).find((target) => target.block_id === blockId) ??
      null,
    enabled: !!blockId,
    staleTime: 30_000,
    retry: false,
  });
}

export function useLinkChecks(documentId: string | null) {
  return useQuery({
    queryKey: queryKeys.linkChecks(documentId),
    queryFn: () => blockApi.checkLinks(documentId!),
    enabled: !!documentId,
    retry: false,
    refetchOnWindowFocus: false,
  });
}

/** The value after it has stopped changing for `delay` ms (for search-as-you-type). */
export function useDebouncedValue<T>(value: T, delay = 250): T {
  const [settled, setSettled] = useState(value);
  useEffect(() => {
    const timer = window.setTimeout(() => setSettled(value), delay);
    return () => window.clearTimeout(timer);
  }, [value, delay]);
  return settled;
}

// --- Documents -----------------------------------------------------------------

export function useCreateDocument() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ folderId = null, title = "" }: { folderId?: string | null; title?: string }) =>
      blockApi.create(folderId, title),
    onSuccess: (document) => {
      qc.setQueryData(queryKeys.document(document.id), document);
      invalidateDocumentLists(qc);
    },
  });
}

/**
 * Change a document's title or status. Updates are applied optimistically and
 * run one at a time per document, so fast edits can't land out of order.
 */
export function useUpdateDocument() {
  const qc = useQueryClient();
  return useMutation({
    // One queue for all metadata edits: a later title can never be overwritten
    // by an earlier request that happened to finish last.
    scope: { id: "document-metadata" },
    mutationFn: ({ id, ...patch }: { id: string; title?: string; status?: NoteStatus }) =>
      blockApi.update(id, patch),
    onMutate: ({ id, ...patch }) => {
      const previous = qc.getQueryData<BlockDocument>(queryKeys.document(id));
      if (previous) qc.setQueryData(queryKeys.document(id), { ...previous, ...patch });
      return { previous };
    },
    onError: (_error, { id }, context) => {
      if (context?.previous) qc.setQueryData(queryKeys.document(id), context.previous);
    },
    onSuccess: (saved) => {
      // Merge metadata only: the editor owns the blocks, and its saves may be newer.
      qc.setQueryData<BlockDocument>(queryKeys.document(saved.id), (current) =>
        current
          ? {
              ...current,
              title: saved.title,
              status: saved.status,
              updated_at: saved.updated_at,
            }
          : saved,
      );
      invalidateDocumentLists(qc);
    },
  });
}

export function useTrashDocument() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => blockApi.remove(id),
    onSuccess: (_result, id) => {
      qc.removeQueries({ queryKey: queryKeys.document(id) });
      invalidateDocumentLists(qc);
    },
  });
}

export function useRestoreDocument() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => workspaceApi.restore(id),
    onSuccess: (_result, id) => {
      void qc.invalidateQueries({ queryKey: queryKeys.document(id) });
      invalidateDocumentLists(qc);
    },
  });
}

// --- Folders -------------------------------------------------------------------

function useNavigationMutation<TVariables, TResult>(
  mutationFn: (variables: TVariables) => Promise<TResult>,
) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn,
    onSuccess: () => void qc.invalidateQueries({ queryKey: queryKeys.navigation }),
  });
}

export function useCreateFolder() {
  return useNavigationMutation(({ name, parentId }: { name: string; parentId?: string | null }) =>
    blockApi.createFolder(name, parentId),
  );
}

export function useRenameFolder() {
  return useNavigationMutation(({ id, name }: { id: string; name: string }) =>
    blockApi.renameFolder(id, name),
  );
}

export function useDeleteFolder() {
  return useNavigationMutation((id: string) => blockApi.removeFolder(id));
}

export function useMoveFolder() {
  return useNavigationMutation(({ id, parentId }: { id: string; parentId: string | null }) =>
    blockApi.moveFolder(id, parentId),
  );
}

export function useMoveDocument() {
  return useNavigationMutation(({ id, folderId }: { id: string; folderId: string | null }) =>
    blockApi.moveDocument(id, folderId),
  );
}

// --- Workspace -----------------------------------------------------------------

export function useTrash(enabled = true) {
  return useQuery({
    queryKey: queryKeys.trash,
    queryFn: workspaceApi.trash,
    enabled,
    retry: false,
  });
}

export function useDeleteForever() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => workspaceApi.deleteForever(id),
    onSuccess: () => invalidateDocumentLists(qc),
  });
}

export function useEmptyTrash() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => workspaceApi.emptyTrash(),
    onSuccess: () => invalidateDocumentLists(qc),
  });
}

export function useSavedSearches() {
  return useQuery({
    queryKey: queryKeys.savedSearches,
    queryFn: workspaceApi.savedSearches,
    retry: false,
  });
}

export function useSaveSearch() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (search: Pick<SavedSearch, "name" | "query" | "mode" | "filters">) =>
      workspaceApi.saveSearch(search),
    onSuccess: () => void qc.invalidateQueries({ queryKey: queryKeys.savedSearches }),
  });
}

export function useDeleteSavedSearch() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => workspaceApi.deleteSavedSearch(id),
    onSuccess: () => void qc.invalidateQueries({ queryKey: queryKeys.savedSearches }),
  });
}

export function useBackups(enabled = true) {
  return useQuery({
    queryKey: queryKeys.backups,
    queryFn: workspaceApi.backups,
    enabled,
    retry: false,
  });
}

export function useCreateBackup() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => workspaceApi.createBackup(),
    onSuccess: () => void qc.invalidateQueries({ queryKey: queryKeys.backups }),
  });
}

export function useRestoreBackup() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (name: string) => workspaceApi.restoreBackup(name),
    // A restore can change any document, so refresh everything.
    onSuccess: () => void qc.invalidateQueries(),
  });
}

export function useImportMarkdown() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ files, folderId }: { files: File[]; folderId?: string | null }) =>
      workspaceApi.importMarkdown(files, folderId ?? null),
    onSuccess: () => invalidateDocumentLists(qc),
  });
}

export function useSimilarDocuments(documentId: string, revision: number) {
  return useQuery({
    queryKey: ["similar", documentId, revision],
    queryFn: () => blockApi.similar(documentId),
    staleTime: 60_000,
    retry: false,
  });
}

export function useMoveInto() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, targetId }: { id: string; targetId: string }) =>
      blockApi.moveInto(id, targetId),
    onSuccess: (_result, { id, targetId }) => {
      qc.removeQueries({ queryKey: queryKeys.document(id) });
      void qc.invalidateQueries({ queryKey: queryKeys.document(targetId) });
      invalidateDocumentLists(qc);
    },
  });
}
