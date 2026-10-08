import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { blockApi, notesApi, workspaceApi } from "./api";
import type { BlockSearchFilters, NoteStatus, ReviewKind, SavedSearch, SearchMode } from "./types";

export function useNotes(status?: NoteStatus) {
  return useQuery({ queryKey: ["notes", status], queryFn: () => notesApi.list(status) });
}

export function useNavigation() {
  return useQuery({
    queryKey: ["navigation"],
    queryFn: () => blockApi.navigation(),
    retry: false,
  });
}

function useNavigationMutation<TVariables, TResult>(
  mutationFn: (variables: TVariables) => Promise<TResult>,
) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn,
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["navigation"] });
    },
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

export function useNote(id: string | null) {
  return useQuery({
    queryKey: ["note", id],
    queryFn: () => notesApi.get(id!),
    enabled: !!id,
  });
}

export function useBlockDocument(id: string | null) {
  return useQuery({
    queryKey: ["block-document", id],
    queryFn: () => blockApi.get(id!),
    enabled: !!id,
  });
}

export function useBlockSearch(q: string, filters: BlockSearchFilters = {}, enabled = true) {
  return useQuery({
    queryKey: ["block-search", q, filters],
    queryFn: () => blockApi.search(q, filters),
    enabled: enabled && q.trim().length > 0,
    retry: false,
  });
}

export function useBacklinks(id: string | null) {
  return useQuery({
    queryKey: ["backlinks", id],
    queryFn: () => blockApi.backlinks(id!),
    enabled: !!id,
  });
}

export function useBlockReference(blockId: string | null) {
  return useQuery({
    queryKey: ["block-reference", blockId],
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
    queryKey: ["link-checks", documentId],
    queryFn: () => blockApi.checkLinks(documentId!),
    enabled: !!documentId,
    retry: false,
    refetchOnWindowFocus: false,
  });
}

export function useSearch(q: string, mode: SearchMode, enabled = true) {
  return useQuery({
    queryKey: ["search", mode, q],
    queryFn: () => notesApi.search(q, mode),
    enabled: enabled && q.trim().length > 0,
  });
}

export function useRelated(id: string | null, blockId: string | null = null) {
  return useQuery({
    queryKey: ["related", id, blockId],
    queryFn: () => notesApi.related(id!, 5, blockId),
    enabled: !!id,
  });
}

export function useCreateNote() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (folderId: string | null = null) => notesApi.create("", folderId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["notes"] });
      qc.invalidateQueries({ queryKey: ["navigation"] });
      qc.invalidateQueries({ queryKey: ["block-search"] });
    },
  });
}

export function useUpdateNote() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, ...patch }: { id: string } & Parameters<typeof notesApi.update>[1]) =>
      notesApi.update(id, patch),
    onSuccess: (note) => {
      qc.invalidateQueries({ queryKey: ["notes"] });
      qc.invalidateQueries({ queryKey: ["navigation"] });
      qc.invalidateQueries({ queryKey: ["block-search"] });
      qc.setQueryData(["note", note.id], note);
      qc.invalidateQueries({ queryKey: ["block-document", note.id] });
    },
  });
}

export function useDeleteNote() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => notesApi.remove(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["trash"] });
      qc.invalidateQueries({ queryKey: ["notes"] });
      qc.invalidateQueries({ queryKey: ["navigation"] });
      qc.invalidateQueries({ queryKey: ["block-search"] });
    },
  });
}

export function usePromoteNote() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => notesApi.promote(id),
    onSuccess: (note) => {
      qc.invalidateQueries({ queryKey: ["notes"] });
      qc.setQueryData(["note", note.id], note);
      qc.invalidateQueries({ queryKey: ["block-search"] });
    },
  });
}

export function useReview() {
  return useMutation({
    mutationFn: ({ id, kind }: { id: string; kind: ReviewKind }) => notesApi.review(id, kind),
  });
}

/** Every query that lists or shows documents; refreshed after workspace-wide changes. */
const documentQueryKeys = [
  ["notes"],
  ["navigation"],
  ["block-search"],
  ["search"],
  ["trash"],
] as const;

function useInvalidateDocuments() {
  const qc = useQueryClient();
  return () => {
    for (const queryKey of documentQueryKeys) qc.invalidateQueries({ queryKey });
  };
}

export function useTrash(enabled = true) {
  return useQuery({ queryKey: ["trash"], queryFn: workspaceApi.trash, enabled, retry: false });
}

export function useRestoreDocument() {
  const invalidate = useInvalidateDocuments();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => workspaceApi.restore(id),
    onSuccess: (_result, id) => {
      invalidate();
      qc.invalidateQueries({ queryKey: ["note", id] });
      qc.invalidateQueries({ queryKey: ["block-document", id] });
    },
  });
}

export function useDeleteForever() {
  const invalidate = useInvalidateDocuments();
  return useMutation({
    mutationFn: (id: string) => workspaceApi.deleteForever(id),
    onSuccess: invalidate,
  });
}

export function useEmptyTrash() {
  const invalidate = useInvalidateDocuments();
  return useMutation({ mutationFn: () => workspaceApi.emptyTrash(), onSuccess: invalidate });
}

export function useSavedSearches() {
  return useQuery({
    queryKey: ["saved-searches"],
    queryFn: workspaceApi.savedSearches,
    retry: false,
  });
}

export function useSaveSearch() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (search: Pick<SavedSearch, "name" | "query" | "mode" | "filters">) =>
      workspaceApi.saveSearch(search),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["saved-searches"] }),
  });
}

export function useDeleteSavedSearch() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => workspaceApi.deleteSavedSearch(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["saved-searches"] }),
  });
}

export function useBackups(enabled = true) {
  return useQuery({ queryKey: ["backups"], queryFn: workspaceApi.backups, enabled, retry: false });
}

export function useCreateBackup() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => workspaceApi.createBackup(),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["backups"] }),
  });
}

export function useRestoreBackup() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (name: string) => workspaceApi.restoreBackup(name),
    // A restore can change any document, so refresh everything.
    onSuccess: () => qc.invalidateQueries(),
  });
}

export function useImportMarkdown() {
  const invalidate = useInvalidateDocuments();
  return useMutation({
    mutationFn: ({ files, folderId }: { files: File[]; folderId?: string | null }) =>
      workspaceApi.importMarkdown(files, folderId ?? null),
    onSuccess: invalidate,
  });
}
