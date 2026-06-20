import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { notesApi } from "./api";
import type { NoteStatus, ReviewKind, SearchMode } from "./types";

export function useNotes(status?: NoteStatus) {
  return useQuery({ queryKey: ["notes", status], queryFn: () => notesApi.list(status) });
}

export function useNote(id: string | null) {
  return useQuery({
    queryKey: ["note", id],
    queryFn: () => notesApi.get(id!),
    enabled: !!id,
  });
}

export function useSearch(q: string, mode: SearchMode) {
  return useQuery({
    queryKey: ["search", mode, q],
    queryFn: () => notesApi.search(q, mode),
    enabled: q.trim().length > 0,
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
    onSuccess: () => qc.invalidateQueries({ queryKey: ["notes"] }),
  });
}

export function useUpdateNote() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, ...patch }: { id: string } & Parameters<typeof notesApi.update>[1]) =>
      notesApi.update(id, patch),
    onSuccess: (note) => {
      qc.invalidateQueries({ queryKey: ["notes"] });
      qc.setQueryData(["note", note.id], note);
    },
  });
}

export function useDeleteNote() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => notesApi.remove(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["notes"] }),
  });
}

export function usePromoteNote() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => notesApi.promote(id),
    onSuccess: (note) => {
      qc.invalidateQueries({ queryKey: ["notes"] });
      qc.setQueryData(["note", note.id], note);
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
