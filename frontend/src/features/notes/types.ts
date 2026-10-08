export type NoteStatus = "rough" | "polished";
export type ReviewKind = "factcheck" | "clarify" | "object";
export type SearchMode = "text" | "embedding";
export type BlockType = "paragraph" | "heading" | "list" | "quote" | "code" | "thematic_break";

export interface BlockSearchFilters {
  documentId?: string;
  blockType?: BlockType;
  status?: NoteStatus;
  updatedAfter?: string;
  updatedBefore?: string;
}

export interface NoteSummary {
  id: string;
  title: string;
  status: NoteStatus;
  tags: string[];
  updated_at: string;
  snippet: string;
}

export interface NavigationDocument extends NoteSummary {
  kind: "document";
  folder_id: string | null;
  position: number;
  created_at: string;
}

export interface NavigationFolder {
  kind: "folder";
  id: string;
  parent_id: string | null;
  name: string;
  position: number;
  created_at: string;
  updated_at: string;
  children: NavigationNode[];
}

export type NavigationNode = NavigationDocument | NavigationFolder;

export interface NavigationResponse {
  items: NavigationNode[];
  recent: NavigationDocument[];
}

export interface Folder {
  id: string;
  parent_id: string | null;
  name: string;
  position: number;
  created_at: string;
  updated_at: string;
}

export interface DocumentOrganization {
  id: string;
  folder_id: string | null;
  position: number;
}

export interface Note extends NoteSummary {
  body: string;
  created_at: string;
}

export interface BlockNode {
  id: string;
  document_id: string;
  parent_id: string | null;
  position: number;
  type: BlockType | string;
  attrs: Record<string, unknown>;
  user_attrs: Record<string, unknown>;
  content: Record<string, unknown>;
  text: string;
  created_at: string;
  updated_at: string;
  children: BlockNode[];
}

export interface BlockDocument {
  id: string;
  revision: number;
  can_undo?: boolean;
  can_redo?: boolean;
  title: string;
  status: NoteStatus;
  tags: string[];
  created_at: string;
  updated_at: string;
  children: BlockNode[];
}

export interface BlockTransaction {
  transaction_id?: string;
  base_revision: number;
  operations: BlockOperation[];
}

export interface BlockOperation {
  operation:
    | "insert"
    | "update"
    | "move"
    | "delete"
    | "set_attrs"
    | "set_user_attrs"
    | "duplicate"
    | "split"
    | "merge";
  block_id?: string;
  parent_id?: string | null;
  position?: number;
  type?: BlockType | string;
  attrs?: Record<string, unknown>;
  user_attrs?: Record<string, unknown>;
  content?: Record<string, unknown>;
  text?: string;
  split_at?: number;
  expected_updated_at?: string;
}

export interface SearchResult {
  note: NoteSummary;
  score: number;
}

export interface RelatedResult extends SearchResult {
  matched_block_id: string | null;
  matched_block_text: string;
}

export interface BlockSearchResult {
  block_id: string;
  document_id: string;
  document_title: string;
  block_type: string;
  text: string;
  score: number;
}

export interface UnifiedSearchResult {
  block_id: string;
  document_id: string;
  document_title: string;
  block_type: string;
  /** A passage around the first match (may start or end with "…"). */
  text: string;
  /** [start, end) character ranges of query words inside `text`. */
  highlights: number[][];
  matched: ("words" | "meaning" | "title")[];
  score: number;
}

export interface AskCitation {
  block_id: string;
  document_id: string;
  document_title: string;
  quote: string;
}

export interface AskResponse {
  status: "answered" | "not_found" | "not_configured";
  answer: { text: string; citations: AskCitation[] }[];
  sources: { block_id: string; document_id: string; document_title: string; text: string }[];
}

export interface BlockLinkTarget {
  block_id: string;
  document_id: string;
  document_title: string;
  block_type: string;
  text: string;
}

export interface Backlink {
  source_block_id: string;
  source_document_id: string;
  source_document_title: string;
  source_text: string;
  label: string;
  target_block_id: string | null;
}

export type Severity = "high" | "medium" | "low";

export interface ReviewItem {
  label: string;
  detail: string;
  quote: string;
  severity: Severity;
}

export interface ReviewResponse {
  kind: ReviewKind;
  summary: string;
  items: ReviewItem[];
}

export interface BlockReviewItem extends ReviewItem {
  block_id: string;
}

export interface BlockReviewResponse {
  kind: ReviewKind;
  summary: string;
  block_id: string;
  items: BlockReviewItem[];
}

export type BlockReviewContext = "current" | "selected" | "document" | "linked";

export interface BlockReviewContextResponse {
  kind: ReviewKind;
  context: BlockReviewContext;
  summary: string;
  items: BlockReviewItem[];
}

export interface LinkCheck {
  url: string;
  status: string;
  checked_at: string;
  block_ids: string[];
}

export interface LinkCheckResponse {
  links: LinkCheck[];
}

export interface TrashedDocument {
  id: string;
  title: string;
  status: NoteStatus;
  tags: string[];
  deleted_at: string;
  snippet: string;
}

export interface SavedSearch {
  id: string;
  name: string;
  query: string;
  mode: SearchMode;
  filters: BlockSearchFilters;
  created_at: string;
}

export interface Backup {
  name: string;
  kind: "auto" | "manual" | "pre-restore";
  created_at: string;
  size: number;
}

export interface MarkdownUploadResult {
  imported: { name: string; id: string; title: string }[];
  errors: { name: string; detail: string }[];
}
