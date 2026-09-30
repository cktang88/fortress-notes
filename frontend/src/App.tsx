import { useEffect, useState } from "react";
import { SearchBar } from "./features/notes/SearchBar";
import { NoteList } from "./features/notes/NoteList";
import { DocumentSidebar } from "./features/notes/DocumentSidebar";
import { BlockSearchResults } from "./features/notes/BlockSearchResults";
import { RelatedNotes } from "./features/notes/RelatedNotes";
import { BacklinksPanel } from "./features/notes/BacklinksPanel";
import { OutlinePanel } from "./features/notes/OutlinePanel";
import { NoteHeader } from "./features/notes/NoteHeader";
import { NoteEditor } from "./features/notes/NoteEditor";
import { BlockNoteEditor } from "./features/notes/BlockNoteEditor";
import type { Annotation } from "./features/notes/annotations";
import {
  useCreateNote,
  useCreateFolder,
  useDeleteFolder,
  useDeleteNote,
  useLinkChecks,
  useNote,
  useBlockDocument,
  useBlockSearch,
  useNavigation,
  useNotes,
  useMoveDocument,
  useMoveFolder,
  useRenameFolder,
  useReview,
  useSearch,
  useUpdateNote,
} from "./features/notes/hooks";
import type { BlockSearchFilters, ReviewKind, SearchMode } from "./features/notes/types";

export function App() {
  const [selectedId, setSelectedId] = useState<string | null>(() =>
    new URLSearchParams(window.location.search).get("note"),
  );
  const [focusBlockId, setFocusBlockId] = useState<string | null>(() =>
    blockIdFromHash(window.location.hash),
  );
  const [activeBlockId, setActiveBlockId] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [mode, setMode] = useState<SearchMode>("text");
  const [blockSearchFilters, setBlockSearchFilters] = useState<BlockSearchFilters>({});
  const [dismissed, setDismissed] = useState<Set<string>>(new Set());

  const searching = query.trim().length > 0;
  const hasBlockSearchFilters = Object.values(blockSearchFilters).some(Boolean);
  const allNotes = useNotes();
  const navigation = useNavigation();
  const blockSearch = useBlockSearch(query, blockSearchFilters, mode === "text");
  const searchResults = useSearch(
    query,
    mode,
    mode !== "text" || (blockSearch.isError && !hasBlockSearchFilters),
  );
  const note = useNote(selectedId);
  const blockDocument = useBlockDocument(selectedId);

  const createNote = useCreateNote();
  const createFolder = useCreateFolder();
  const renameFolder = useRenameFolder();
  const deleteFolder = useDeleteFolder();
  const moveDocument = useMoveDocument();
  const moveFolder = useMoveFolder();
  const updateNote = useUpdateNote();
  const deleteNote = useDeleteNote();
  const review = useReview();
  const linkChecks = useLinkChecks(selectedId);

  const listNotes = searching
    ? (searchResults.data ?? []).map((r) => r.note)
    : (allNotes.data ?? []);
  const scores = searching
    ? Object.fromEntries((searchResults.data ?? []).map((r) => [r.note.id, r.score]))
    : undefined;

  // Review findings stay attached to their quoted text in the editor.
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
  ].filter((a) => !dismissed.has(a.id));

  const brokenLinks = (linkChecks.data?.links ?? [])
    .filter((link) => link.status !== "ok")
    .map((link) => link.url);

  useEffect(() => {
    const restoreLocation = () => {
      setSelectedId(new URLSearchParams(window.location.search).get("note"));
      setFocusBlockId(blockIdFromHash(window.location.hash));
      setActiveBlockId(null);
    };
    window.addEventListener("popstate", restoreLocation);
    return () => window.removeEventListener("popstate", restoreLocation);
  }, []);

  const selectNote = (id: string, blockId: string | null = null) => {
    setSelectedId(id);
    setFocusBlockId(blockId);
    setActiveBlockId(null);
    const url = new URL(window.location.href);
    url.searchParams.set("note", id);
    url.hash = blockId ? `block=${encodeURIComponent(blockId)}` : "";
    window.history.replaceState(null, "", url);
    setDismissed(new Set());
    review.reset();
  };

  const handleNew = async (folderId: string | null = null) => {
    const created = await createNote.mutateAsync(folderId);
    selectNote(created.id);
  };

  const handleDelete = async (id = selectedId) => {
    if (!id) return;
    await deleteNote.mutateAsync(id);
    if (id === selectedId) {
      setSelectedId(null);
      setActiveBlockId(null);
      const url = new URL(window.location.href);
      url.searchParams.delete("note");
      window.history.replaceState(null, "", url);
    }
  };

  const handleReview = (kind: ReviewKind) => {
    if (!selectedId) return;
    setDismissed(new Set());
    review.mutate({ id: selectedId, kind });
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
          filters={blockSearchFilters}
          onFilters={setBlockSearchFilters}
          documents={allNotes.data ?? []}
          onNew={handleNew}
        />
        {searching && mode === "text" ? (
          blockSearch.isError && !hasBlockSearchFilters ? (
            <NoteList
              notes={listNotes}
              selectedId={selectedId}
              onSelect={selectNote}
              scores={scores}
              loading={searchResults.isLoading}
            />
          ) : (
            <BlockSearchResults
              results={blockSearch.data ?? []}
              loading={blockSearch.isLoading}
              error={blockSearch.isError}
              onSelect={(documentId, blockId) => selectNote(documentId, blockId)}
            />
          )
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
            recent={navigation.data?.recent ?? []}
            selectedId={selectedId}
            loading={navigation.isLoading}
            onSelect={selectNote}
            onCreateFolder={(name, parentId) => createFolder.mutateAsync({ name, parentId })}
            onCreateNote={handleNew}
            onRenameFolder={(id, name) => renameFolder.mutateAsync({ id, name })}
            onRenameDocument={(id, title) => updateNote.mutateAsync({ id, title })}
            onDeleteFolder={(id) => deleteFolder.mutateAsync(id)}
            onDeleteDocument={async (id) => handleDelete(id)}
            onMoveDocument={(id, folderId) => moveDocument.mutateAsync({ id, folderId })}
            onMoveFolder={(id, parentId) => moveFolder.mutateAsync({ id, parentId })}
          />
        )}
        <OutlinePanel
          document={blockDocument.data ?? null}
          onFocus={(blockId) => selectedId && selectNote(selectedId, blockId)}
        />
        <RelatedNotes noteId={selectedId} blockId={activeBlockId} onSelect={selectNote} />
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
              onClarify={() => handleReview("clarify")}
              checking={review.isPending}
            />
            {blockDocument.data ? (
              <BlockNoteEditor
                key={blockDocument.data.id}
                document={blockDocument.data}
                focusBlockId={focusBlockId}
                linkTargets={allNotes.data ?? []}
                linkChecks={linkChecks.data?.links ?? []}
                onFocusedBlockChange={setActiveBlockId}
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

function blockIdFromHash(hash: string): string | null {
  if (!hash.startsWith("#block=")) return null;
  const encodedId = hash.slice("#block=".length);
  try {
    return decodeURIComponent(encodedId) || null;
  } catch {
    return null;
  }
}
