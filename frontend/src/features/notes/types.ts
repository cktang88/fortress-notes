export type NoteStatus = "rough" | "polished";
export type ReviewKind = "factcheck" | "clarify" | "object";
export type SearchMode = "text" | "embedding";
export type BlockType = "paragraph" | "heading" | "list" | "quote" | "code" | "thematic_break";

export interface NoteSummary {
  id: string;
  title: string;
  status: NoteStatus;
  tags: string[];
  updated_at: string;
  snippet: string;
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
  content: Record<string, unknown>;
  text: string;
  created_at: string;
  updated_at: string;
  children: BlockNode[];
}

export interface BlockDocument {
  id: string;
  title: string;
  status: NoteStatus;
  tags: string[];
  created_at: string;
  updated_at: string;
  children: BlockNode[];
}

export interface BlockOperation {
  operation: "insert" | "update" | "move" | "delete" | "set_attrs";
  block_id?: string;
  parent_id?: string | null;
  position?: number;
  type?: BlockType | string;
  attrs?: Record<string, unknown>;
  content?: Record<string, unknown>;
  text?: string;
  expected_updated_at?: string;
}

export interface SearchResult {
  note: NoteSummary;
  score: number;
}

export interface BlockSearchResult {
  block_id: string;
  document_id: string;
  document_title: string;
  block_type: string;
  text: string;
  score: number;
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

export interface ConsistencyIssue {
  related_note_id: string;
  related_note_title: string;
  claim: string;
  conflict: string;
  severity: "low" | "medium" | "high";
}

export interface ConsistencyReport {
  summary: string;
  issues: ConsistencyIssue[];
  checked_against: string[];
}

export interface DeadLink {
  url: string;
  status: string;
}

export interface StaleFact {
  claim: string;
  finding: string;
  suggestion: string;
}

export interface HealReport {
  summary: string;
  dead_links: DeadLink[];
  stale_facts: StaleFact[];
}
