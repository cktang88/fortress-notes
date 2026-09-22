import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { blockApi, notesApi } from "./api";
import type { BlockSearchFilters, NoteStatus, ReviewKind, SearchMode } from "./types";

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

export function useSearch(q: string, mode: SearchMode, enabled = true) {
  return useQuery({
    queryKey: ["search", mode, q],
    queryFn: () => notesApi.search(q, mode),
    enabled: enabled && q.trim().length > 0,
  });
}

export function useRelated(id: string | null) {
  return useQuery({
    queryKey: ["related", id],
    queryFn: () => notesApi.related(id!),
    enabled: !!id,
  });
}

export function useCreateNote() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => notesApi.create(""),
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
    },
  });
}

export function useDeleteNote() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => notesApi.remove(id),
    onSuccess: () => {
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

export function useConsistency() {
  return useMutation({ mutationFn: (id: string) => notesApi.consistency(id) });
}

export function useHeal() {
  return useMutation({ mutationFn: (id: string) => notesApi.heal(id) });
}
