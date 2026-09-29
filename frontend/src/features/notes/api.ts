import type {
  ConsistencyReport,
  HealReport,
  Note,
  NoteStatus,
  NoteSummary,
  NavigationResponse,
  Folder,
  DocumentOrganization,
  BlockDocument,
  BlockSearchFilters,
  BlockSearchResult,
  BlockLinkTarget,
  Backlink,
  BlockTransaction,
  ReviewKind,
  ReviewResponse,
  BlockReviewResponse,
  BlockReviewContext,
  BlockReviewContextResponse,
  SearchMode,
  SearchResult,
  RelatedResult,
} from "./types";

const API = "/api";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!res.ok) {
    const body = await res.json().catch(() => null);
    const detail =
      body && typeof body === "object" && "detail" in body && typeof body.detail === "string"
        ? body.detail
        : null;
    throw new Error(`${res.status} ${detail ?? res.statusText}`);
  }
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

  related: (id: string, k = 5) => request<RelatedResult[]>(`/notes/${id}/related?k=${k}`),

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

  uploadFile: async (file: File): Promise<{ props: { url: string; name: string } }> => {
    const form = new FormData();
    form.append("file", file);
    const res = await fetch(`${API}/files`, { method: "POST", body: form });
    if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
    return res.json();
  },
};

export const blockApi = {
  navigation: (recentLimit = 10) =>
    request<NavigationResponse>(`/navigation?recent_limit=${recentLimit}`),
  createFolder: (name: string, parent_id: string | null = null, position?: number) =>
    request<Folder>("/folders", {
      method: "POST",
      body: JSON.stringify({ name, parent_id, position }),
    }),
  renameFolder: (id: string, name: string) =>
    request<Folder>(`/folders/${id}`, { method: "PATCH", body: JSON.stringify({ name }) }),
  removeFolder: (id: string) => request<void>(`/folders/${id}`, { method: "DELETE" }),
  moveDocument: (id: string, folder_id: string | null, position?: number) =>
    request<DocumentOrganization>(`/block-documents/${id}/move`, {
      method: "POST",
      body: JSON.stringify({ folder_id, position }),
    }),
  get: (id: string) => request<BlockDocument>(`/block-documents/${id}`),
  review: (documentId: string, blockId: string, kind: ReviewKind = "factcheck") =>
    request<BlockReviewResponse>(
      `/block-documents/${encodeURIComponent(documentId)}/blocks/${encodeURIComponent(blockId)}/review?kind=${kind}`,
      { method: "POST" },
    ),
  reviewContext: (
    documentId: string,
    context: BlockReviewContext,
    blockIds: string[],
    kind: ReviewKind = "factcheck",
  ) =>
    request<BlockReviewContextResponse>(
      `/block-documents/${encodeURIComponent(documentId)}/review-context`,
      { method: "POST", body: JSON.stringify({ kind, context, block_ids: blockIds }) },
    ),
  healthFindings: async (documentId: string) => {
    const [consistency, healing] = await Promise.all([
      notesApi.consistency(documentId),
      notesApi.heal(documentId),
    ]);
    return { consistency, healing };
  },
  backlinks: (id: string, limit = 100) =>
    request<Backlink[]>(`/block-documents/${id}/backlinks?limit=${limit}`),
  search: (q: string, filters: BlockSearchFilters = {}, limit = 50) =>
    request<BlockSearchResult[]>(`/block-search?${blockSearchParams(q, filters, limit)}`),
  linkTargets: (q: string, limit = 50) =>
    request<BlockLinkTarget[]>(`/block-link-targets?q=${encodeURIComponent(q)}&limit=${limit}`),
  transaction: (id: string, transaction: BlockTransaction) =>
    request<BlockDocument>(`/block-documents/${id}/transactions`, {
      method: "POST",
      body: JSON.stringify(transaction),
    }),
  undo: (id: string, base_revision: number) =>
    request<BlockDocument>(`/block-documents/${id}/undo`, {
      method: "POST",
      body: JSON.stringify({ base_revision }),
    }),
  redo: (id: string, base_revision: number) =>
    request<BlockDocument>(`/block-documents/${id}/redo`, {
      method: "POST",
      body: JSON.stringify({ base_revision }),
    }),
};

export function blockSearchParams(q: string, filters: BlockSearchFilters = {}, limit = 50) {
  const params = new URLSearchParams({ q, limit: String(limit) });
  const filterParams = {
    document_id: filters.documentId,
    block_type: filters.blockType,
    status: filters.status,
    tag: filters.tag?.trim() || undefined,
    updated_after: filters.updatedAfter,
    updated_before: filters.updatedBefore,
  };
  for (const [name, value] of Object.entries(filterParams)) {
    if (value) params.set(name, value);
  }
  return params.toString();
}
