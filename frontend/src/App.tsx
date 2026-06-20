import { useState } from "react";
import { SearchBar } from "./features/notes/SearchBar";
import { NoteList } from "./features/notes/NoteList";
import { RelatedNotes } from "./features/notes/RelatedNotes";
import { NoteHeader } from "./features/notes/NoteHeader";
import { NoteEditor } from "./features/notes/NoteEditor";
import { ReviewPanel } from "./features/notes/ReviewPanel";
import { ConsistencyPanel, HealPanel } from "./features/notes/AnalysisPanel";
import {
  useConsistency,
  useCreateNote,
  useDeleteNote,
  useHeal,
  useNote,
  useNotes,
  usePromoteNote,
  useReview,
  useSearch,
  useUpdateNote,
} from "./features/notes/hooks";
import type { ReviewKind, SearchMode } from "./features/notes/types";

export function App() {
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [mode, setMode] = useState<SearchMode>("text");

  const searching = query.trim().length > 0;
  const allNotes = useNotes();
  const searchResults = useSearch(query, mode);
  const note = useNote(selectedId);

  const createNote = useCreateNote();
  const updateNote = useUpdateNote();
  const deleteNote = useDeleteNote();
  const promoteNote = usePromoteNote();
  const review = useReview();
  const consistency = useConsistency();
  const heal = useHeal();

  const listNotes = searching
    ? (searchResults.data ?? []).map((r) => r.note)
    : (allNotes.data ?? []);
  const scores = searching
    ? Object.fromEntries((searchResults.data ?? []).map((r) => [r.note.id, r.score]))
    : undefined;

  const handleNew = async () => {
    const created = await createNote.mutateAsync();
    setSelectedId(created.id);
  };

  const handleDelete = () => {
    if (!selectedId) return;
    deleteNote.mutate(selectedId);
    setSelectedId(null);
  };

  const handleReview = (kind: ReviewKind) => {
    if (selectedId) review.mutate({ id: selectedId, kind });
  };

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
        <NoteList
          notes={listNotes}
          selectedId={selectedId}
          onSelect={setSelectedId}
          scores={scores}
          loading={searching ? searchResults.isLoading : allNotes.isLoading}
        />
        <RelatedNotes noteId={selectedId} onSelect={setSelectedId} />
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
              onPromote={() => promoteNote.mutate(note.data!.id)}
              onDelete={handleDelete}
              onReview={handleReview}
              onConsistency={() => consistency.mutate(note.data!.id)}
              onHeal={() => heal.mutate(note.data!.id)}
            />
            <NoteEditor
              key={note.data.id}
              initialMarkdown={note.data.body}
              onSave={(body) => updateNote.mutate({ id: note.data!.id, body })}
            />
            <ReviewPanel
              review={review.data ?? null}
              loading={review.isPending}
              onClose={() => review.reset()}
            />
            <ConsistencyPanel
              report={consistency.data ?? null}
              loading={consistency.isPending}
              onClose={() => consistency.reset()}
              onOpenNote={setSelectedId}
            />
            <HealPanel
              report={heal.data ?? null}
              loading={heal.isPending}
              onClose={() => heal.reset()}
            />
          </>
        )}
      </main>
    </div>
  );
}
