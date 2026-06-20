export type NoteStatus = "rough" | "polished";
export type ReviewKind = "factcheck" | "clarify" | "object";
export type SearchMode = "text" | "embedding";

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

export interface SearchResult {
  note: NoteSummary;
  score: number;
}

export interface ReviewItem {
  label: string;
  detail: string;
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
