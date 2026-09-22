import { useState } from "react";
import { SearchBar } from "./features/notes/SearchBar";
import { NoteList } from "./features/notes/NoteList";
import { DocumentSidebar } from "./features/notes/DocumentSidebar";
import { BlockSearchResults } from "./features/notes/BlockSearchResults";
import { RelatedNotes } from "./features/notes/RelatedNotes";
import { BacklinksPanel } from "./features/notes/BacklinksPanel";
import { OutlinePanel } from "./features/notes/OutlinePanel";
import { NoteHeader } from "./features/notes/NoteHeader";
import { NoteEditor } from "./features/notes/NoteEditor";
import { RawEditor } from "./features/notes/RawEditor";
import { BlockNoteEditor } from "./features/notes/BlockNoteEditor";
import type { Annotation } from "./features/notes/annotations";
import {
  useConsistency,
  useCreateNote,
  useDeleteNote,
  useHeal,
  useNote,
  useBlockDocument,
  useBlockSearch,
  useNavigation,
  useNotes,
  useReview,
  useSearch,
  useUpdateNote,
} from "./features/notes/hooks";
import type { ReviewKind, SearchMode } from "./features/notes/types";

export function App() {
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [focusBlockId, setFocusBlockId] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [mode, setMode] = useState<SearchMode>("text");
  const [dismissed, setDismissed] = useState<Set<string>>(new Set());
  const [rawView, setRawView] = useState(false);

  const searching = query.trim().length > 0;
  const allNotes = useNotes();
  const navigation = useNavigation();
  const blockSearch = useBlockSearch(query, mode === "text");
  const searchResults = useSearch(query, mode, mode !== "text" || blockSearch.isError);
  const note = useNote(selectedId);
  const blockDocument = useBlockDocument(selectedId);

  const createNote = useCreateNote();
  const updateNote = useUpdateNote();
  const deleteNote = useDeleteNote();
  const review = useReview();
  const consistency = useConsistency();
  const heal = useHeal();

  const listNotes = searching
    ? (searchResults.data ?? []).map((r) => r.note)
    : (allNotes.data ?? []);
  const scores = searching
    ? Object.fromEntries((searchResults.data ?? []).map((r) => [r.note.id, r.score]))
    : undefined;

  // All AI findings (review, consistency, stale facts) become inline annotations.
  const annotations: Annotation[] = [
    ...(review.data?.items ?? [])
      .filter((it) => it.quote)
      .map((it, i) => ({
        id: `r${i}`,
        claim: it.quote,
        type: "review" as const,
        severity: it.severity,
        message: it.detail,
      })),
    ...(consistency.data?.issues ?? []).map((iss, i) => ({
      id: `c${i}`,
      claim: iss.claim,
      type: "consistency" as const,
      severity: iss.severity,
      message: `Conflicts with "${iss.related_note_title}": ${iss.conflict}`,
    })),
    ...(heal.data?.stale_facts ?? []).map((f, i) => ({
      id: `s${i}`,
      claim: f.claim,
      type: "stale" as const,
      severity: "high" as const,
      message: f.finding,
      suggestion: f.suggestion,
    })),
  ].filter((a) => !dismissed.has(a.id));

  const brokenLinks = (heal.data?.dead_links ?? []).map((d) => d.url);

  const selectNote = (id: string, blockId: string | null = null) => {
    setSelectedId(id);
    setFocusBlockId(blockId);
    setDismissed(new Set());
    consistency.reset();
    heal.reset();
    review.reset();
  };

  const handleNew = async () => {
    const created = await createNote.mutateAsync();
    selectNote(created.id);
  };

  const handleDelete = () => {
    if (!selectedId) return;
    deleteNote.mutate(selectedId);
    setSelectedId(null);
  };

  const handleReview = (kind: ReviewKind) => {
    if (!selectedId) return;
    setDismissed(new Set());
    review.mutate({ id: selectedId, kind });
  };

  const runCheck = (which: "consistency" | "heal") => {
    if (!selectedId) return;
    setDismissed(new Set());
    if (which === "consistency") consistency.mutate(selectedId);
    else heal.mutate(selectedId);
  };

  const dismiss = (id: string) => setDismissed((prev) => new Set(prev).add(id));

  return (
    <div className="flex h-screen text-zinc-900">
      {/* Left column */}
      <aside className="flex w-80 flex-col border-r border-zinc-200">
        <SearchBar
          query={query}
          onQuery={setQuery}
          mode={mode}
          onMode={setMode}
          onNew={handleNew}
        />
        {searching && mode === "text" && !blockSearch.isError ? (
          <BlockSearchResults
            results={blockSearch.data ?? []}
            loading={blockSearch.isLoading}
            onSelect={(documentId, blockId) => selectNote(documentId, blockId)}
          />
        ) : navigation.isError ? (
          <NoteList
            notes={listNotes}
            selectedId={selectedId}
            onSelect={selectNote}
            scores={scores}
            loading={searching ? searchResults.isLoading : allNotes.isLoading}
          />
        ) : (
          <DocumentSidebar
            nodes={navigation.data?.items ?? []}
            selectedId={selectedId}
            loading={navigation.isLoading}
            onSelect={selectNote}
          />
        )}
        <OutlinePanel
          document={blockDocument.data ?? null}
          onFocus={(blockId) => selectedId && selectNote(selectedId, blockId)}
        />
        <RelatedNotes noteId={selectedId} onSelect={selectNote} />
        <BacklinksPanel documentId={selectedId} onSelect={selectNote} />
      </aside>

      {/* Right pane */}
      <main className="flex flex-1 flex-col overflow-y-auto">
        {!selectedId && (
          <div className="flex flex-1 items-center justify-center text-zinc-400">
            Select a note or create a new one.
          </div>
        )}
        {selectedId && note.data && (
          <>
            <NoteHeader
              note={note.data}
              onTitle={(title) => updateNote.mutate({ id: note.data!.id, title })}
              onDelete={handleDelete}
              onSetStatus={(status) => updateNote.mutate({ id: note.data!.id, status })}
              onFactcheck={() => handleReview("factcheck")}
              onClarify={() => handleReview("clarify")}
              onConsistency={() => runCheck("consistency")}
              onLint={() => runCheck("heal")}
              checking={consistency.isPending || heal.isPending || review.isPending}
              rawView={rawView}
              onToggleRaw={() => setRawView((v) => !v)}
            />
            {rawView ? (
              <RawEditor
                key={`raw-${note.data.id}`}
                initialMarkdown={note.data.body}
                onSave={(body) => updateNote.mutate({ id: note.data!.id, body })}
              />
            ) : blockDocument.data ? (
              <BlockNoteEditor
                key={blockDocument.data.id}
                document={blockDocument.data}
                focusBlockId={focusBlockId}
                linkTargets={allNotes.data ?? []}
              />
            ) : blockDocument.isLoading ? (
              <div className="p-8 text-sm text-zinc-400">Loading blocks…</div>
            ) : (
              <NoteEditor
                key={note.data.id}
                initialMarkdown={note.data.body}
                onSave={(body) => updateNote.mutate({ id: note.data!.id, body })}
                annotations={annotations}
                brokenLinks={brokenLinks}
                onResolve={dismiss}
              />
            )}
          </>
        )}
      </main>
    </div>
  );
}
