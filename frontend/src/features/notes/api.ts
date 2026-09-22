import type {
  ConsistencyReport,
  HealReport,
  Note,
  NoteStatus,
  NoteSummary,
  BlockDocument,
  BlockOperation,
  ReviewKind,
  ReviewResponse,
  SearchMode,
  SearchResult,
} from "./types";

const API = "/api";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  if (res.status === 204) return undefined as T;
  return res.json() as Promise<T>;
}

export const notesApi = {
  list: (status?: NoteStatus) =>
    request<NoteSummary[]>(`/notes${status ? `?status=${status}` : ""}`),

  get: (id: string) => request<Note>(`/notes/${id}`),

  create: (body = "") =>
    request<Note>("/notes", { method: "POST", body: JSON.stringify({ body }) }),

  update: (id: string, patch: Partial<Pick<Note, "title" | "body" | "status" | "tags">>) =>
    request<Note>(`/notes/${id}`, { method: "PUT", body: JSON.stringify(patch) }),

  remove: (id: string) => request<void>(`/notes/${id}`, { method: "DELETE" }),

  promote: (id: string) => request<Note>(`/notes/${id}/promote`, { method: "POST" }),

  search: (q: string, mode: SearchMode) =>
    request<SearchResult[]>(`/search?q=${encodeURIComponent(q)}&mode=${mode}`),

  related: (id: string, k = 5) => request<SearchResult[]>(`/notes/${id}/related?k=${k}`),

  review: (id: string, kind: ReviewKind) =>
    request<ReviewResponse>(`/notes/${id}/review?kind=${kind}`, { method: "POST" }),

  consistency: (id: string) =>
    request<ConsistencyReport>(`/notes/${id}/consistency`, { method: "POST" }),

  heal: (id: string) => request<HealReport>(`/notes/${id}/heal`, { method: "POST" }),

  uploadImage: async (file: File): Promise<{ url: string; text: string }> => {
    const form = new FormData();
    form.append("file", file);
    const res = await fetch(`${API}/images`, { method: "POST", body: form });
    if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
    return res.json();
  },
};

export const blockApi = {
  get: (id: string) => request<BlockDocument>(`/block-documents/${id}`),
  transaction: (id: string, operations: BlockOperation[]) =>
    request<BlockDocument>(`/block-documents/${id}/transactions`, {
      method: "POST",
      body: JSON.stringify({ operations }),
    }),
};
