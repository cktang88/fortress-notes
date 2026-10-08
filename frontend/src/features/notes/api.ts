import type {
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
  LinkCheckResponse,
  TrashedDocument,
  TagCount,
  SavedSearch,
  Backup,
  MarkdownUploadResult,
} from "./types";

const API = "/api";

async function failure(res: Response): Promise<Error> {
  const body = await res.json().catch(() => null);
  const detail =
    body && typeof body === "object" && "detail" in body && typeof body.detail === "string"
      ? body.detail
      : null;
  return new Error(`${res.status} ${detail ?? res.statusText}`);
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!res.ok) throw await failure(res);
  if (res.status === 204) return undefined as T;
  return res.json() as Promise<T>;
}

export const notesApi = {
  list: (status?: NoteStatus) =>
    request<NoteSummary[]>(`/notes${status ? `?status=${status}` : ""}`),

  get: (id: string) => request<Note>(`/notes/${id}`),

  create: (body = "", folderId: string | null = null) =>
    request<Note>("/notes", {
      method: "POST",
      body: JSON.stringify({ body, folder_id: folderId }),
    }),

  update: (id: string, patch: Partial<Pick<Note, "title" | "body" | "status" | "tags">>) =>
    request<Note>(`/notes/${id}`, { method: "PUT", body: JSON.stringify(patch) }),

  remove: (id: string) => request<void>(`/notes/${id}`, { method: "DELETE" }),

  promote: (id: string) => request<Note>(`/notes/${id}/promote`, { method: "POST" }),

  search: (q: string, mode: SearchMode) =>
    request<SearchResult[]>(`/search?q=${encodeURIComponent(q)}&mode=${mode}`),

  related: (id: string, k = 5, blockId?: string | null) => {
    const params = new URLSearchParams({ k: String(k) });
    if (blockId) params.set("block_id", blockId);
    return request<RelatedResult[]>(`/notes/${id}/related?${params.toString()}`);
  },

  review: (id: string, kind: ReviewKind) =>
    request<ReviewResponse>(`/notes/${id}/review?kind=${kind}`, { method: "POST" }),

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
  moveFolder: (id: string, parent_id: string | null) =>
    request<Folder>(`/folders/${id}/move`, {
      method: "POST",
      body: JSON.stringify({ parent_id }),
    }),
  moveDocument: (id: string, folder_id: string | null, position?: number) =>
    request<DocumentOrganization>(`/block-documents/${id}/move`, {
      method: "POST",
      body: JSON.stringify({ folder_id, position }),
    }),
  get: (id: string) => request<BlockDocument>(`/block-documents/${id}`),
  checkLinks: (id: string) =>
    request<LinkCheckResponse>(`/block-documents/${id}/link-checks`, { method: "POST" }),
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

/** Workspace-level actions: trash, tags, saved searches, backups, import/export. */
export const workspaceApi = {
  trash: () => request<TrashedDocument[]>("/trash"),
  restore: (id: string) =>
    request<DocumentOrganization>(`/trash/${encodeURIComponent(id)}/restore`, { method: "POST" }),
  deleteForever: (id: string) =>
    request<void>(`/trash/${encodeURIComponent(id)}`, { method: "DELETE" }),
  emptyTrash: () => request<{ deleted: number }>("/trash", { method: "DELETE" }),

  tags: () => request<TagCount[]>("/tags"),
  renameTag: (tag: string, name: string) =>
    request<{ documents: string[] }>("/tags/rename", {
      method: "POST",
      body: JSON.stringify({ tag, name }),
    }),
  deleteTag: (tag: string) =>
    request<{ documents: string[] }>("/tags/delete", {
      method: "POST",
      body: JSON.stringify({ tag }),
    }),

  savedSearches: () => request<SavedSearch[]>("/saved-searches"),
  saveSearch: (search: Pick<SavedSearch, "name" | "query" | "mode" | "filters">) =>
    request<SavedSearch>("/saved-searches", {
      method: "POST",
      body: JSON.stringify({ ...search, filters: savedSearchFilters(search.filters) }),
    }),
  deleteSavedSearch: (id: string) =>
    request<void>(`/saved-searches/${encodeURIComponent(id)}`, { method: "DELETE" }),

  backups: () => request<Backup[]>("/backups"),
  createBackup: () => request<Backup>("/backups", { method: "POST" }),
  restoreBackup: (name: string) =>
    request<{ restored: string; safety_backup: string }>(
      `/backups/${encodeURIComponent(name)}/restore`,
      { method: "POST" },
    ),
  backupUrl: (name: string) => `${API}/backups/${encodeURIComponent(name)}/download`,

  importMarkdown: async (files: File[], folderId: string | null = null) => {
    const form = new FormData();
    for (const file of files) form.append("files", file);
    if (folderId) form.append("folder_id", folderId);
    const res = await fetch(`${API}/markdown-import/files`, { method: "POST", body: form });
    if (!res.ok) throw await failure(res);
    return (await res.json()) as MarkdownUploadResult;
  },
  exportAllUrl: `${API}/markdown-export.zip`,
  documentMarkdownUrl: (id: string) =>
    `${API}/block-documents/${encodeURIComponent(id)}/markdown?download=true`,
};

function savedSearchFilters(filters: BlockSearchFilters): Record<string, string> {
  return Object.fromEntries(
    Object.entries(filters).filter((entry): entry is [string, string] => !!entry[1]),
  );
}
