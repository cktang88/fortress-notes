import { useEffect, useEffectEvent, useState } from "react";
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
import { TagsPanel } from "./features/notes/TagsPanel";
import { TaggedNotes } from "./features/notes/TaggedNotes";
import { TagEditorDialog } from "./features/notes/TagEditorDialog";
import { WorkspaceMenu } from "./features/notes/WorkspaceMenu";
import { Toast, type Notice } from "./features/notes/Toast";
import { workspaceApi } from "./features/notes/api";
import { isEditableTarget, SEARCH_INPUT_ID } from "./features/notes/listKeyboard";
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
  useRestoreDocument,
  useReview,
  useSavedSearches,
  useSaveSearch,
  useDeleteSavedSearch,
  useTags,
  useSearch,
  useUpdateNote,
} from "./features/notes/hooks";
import type {
  BlockSearchFilters,
  ReviewKind,
  SavedSearch,
  SearchMode,
} from "./features/notes/types";

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
  const [activeTag, setActiveTag] = useState<string | null>(null);
  const [tagEditorId, setTagEditorId] = useState<string | null>(null);
  const [notice, setNotice] = useState<Notice | null>(null);

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
  const restoreDocument = useRestoreDocument();
  const tags = useTags();
  const savedSearches = useSavedSearches();
  const saveSearch = useSaveSearch();
  const deleteSavedSearch = useDeleteSavedSearch();
  const tagNames = (tags.data ?? []).map((item) => item.tag);
  const tagEditorNote = (allNotes.data ?? []).find((item) => item.id === tagEditorId) ?? null;

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

  const showNotice = (next: Omit<Notice, "id">) => setNotice({ ...next, id: Date.now() });

  const handleNew = async (folderId: string | null = null) => {
    const created = await createNote.mutateAsync(folderId);
    selectNote(created.id);
  };

  const handleDelete = async (id = selectedId) => {
    if (!id) return;
    const title =
      (allNotes.data ?? []).find((item) => item.id === id)?.title ??
      (id === selectedId ? note.data?.title : undefined) ??
      "note";
    await deleteNote.mutateAsync(id);
    showNotice({
      message: `Moved “${title}” to Trash`,
      action: {
        label: "Undo",
        run: () =>
          restoreDocument.mutate(id, {
            onSuccess: () => selectNote(id),
            onError: (error) => showNotice({ tone: "error", message: error.message }),
          }),
      },
    });
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

  const applySavedSearch = (saved: SavedSearch) => {
    setActiveTag(null);
    setMode(saved.mode);
    setBlockSearchFilters(saved.filters ?? {});
    setQuery(saved.query);
  };

  const handleSaveSearch = () => {
    saveSearch.mutate(
      {
        name: query.trim(),
        query: query.trim(),
        mode,
        filters: mode === "text" ? blockSearchFilters : {},
      },
      {
        onSuccess: () => showNotice({ message: `Saved search “${query.trim()}”` }),
        onError: (error) => showNotice({ tone: "error", message: error.message }),
      },
    );
  };

  // Global shortcuts: ⌘/Ctrl+K or "/" to search, Alt+N for a new note.
  const onShortcut = useEffectEvent((event: KeyboardEvent) => {
    const mod = event.metaKey || event.ctrlKey;
    if (mod && !event.shiftKey && !event.altKey && event.key.toLowerCase() === "k") {
      event.preventDefault();
      focusSearch();
    } else if (event.key === "/" && !mod && !isEditableTarget(event.target)) {
      event.preventDefault();
      focusSearch();
    } else if (event.altKey && !mod && event.code === "KeyN") {
      event.preventDefault();
      void handleNew();
    }
  });

  useEffect(() => {
    const listener = (event: KeyboardEvent) => onShortcut(event);
    document.addEventListener("keydown", listener);
    return () => document.removeEventListener("keydown", listener);
  }, []);

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
          tags={tagNames}
          savedSearches={savedSearches.data ?? []}
          onSaveSearch={handleSaveSearch}
          onApplySavedSearch={applySavedSearch}
          onDeleteSavedSearch={(id) => deleteSavedSearch.mutate(id)}
          onNew={handleNew}
          actions={
            <WorkspaceMenu
              onNotice={showNotice}
              onOpenNote={(id) => {
                setActiveTag(null);
                selectNote(id);
              }}
            />
          }
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
        ) : activeTag ? (
          <TaggedNotes
            tag={activeTag}
            notes={allNotes.data ?? []}
            selectedId={selectedId}
            onSelect={selectNote}
            onTagChange={setActiveTag}
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
            onEditTags={setTagEditorId}
          />
        )}
        <TagsPanel activeTag={activeTag} onSelectTag={setActiveTag} />
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
          <div className="flex flex-1 flex-col items-center justify-center gap-2 text-zinc-400">
            <p>Select a note or create a new one.</p>
            <p className="text-xs">
              <kbd className="rounded border border-zinc-300 px-1">Alt</kbd>+
              <kbd className="rounded border border-zinc-300 px-1">N</kbd> new note ·{" "}
              <kbd className="rounded border border-zinc-300 px-1">⌘K</kbd> search ·{" "}
              <kbd className="rounded border border-zinc-300 px-1">?</kbd> tips
            </p>
          </div>
        )}
        {selectedId && note.data && (
          <>
            <NoteHeader
              note={note.data}
              onTitle={(title) => updateNote.mutate({ id: note.data!.id, title })}
              onDelete={() => void handleDelete()}
              onSetStatus={(status) => updateNote.mutate({ id: note.data!.id, status })}
              onClarify={() => handleReview("clarify")}
              checking={review.isPending}
              exportUrl={
                blockDocument.data ? workspaceApi.documentMarkdownUrl(note.data.id) : undefined
              }
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
      <TagEditorDialog
        open={tagEditorNote !== null}
        title={tagEditorNote?.title ?? ""}
        tags={tagEditorNote?.tags ?? []}
        suggestions={tagNames}
        onChange={(next) => {
          if (tagEditorNote)
            updateNote.mutate(
              { id: tagEditorNote.id, tags: next },
              { onError: (error) => showNotice({ tone: "error", message: error.message }) },
            );
        }}
        onClose={() => setTagEditorId(null)}
      />
      <Toast notice={notice} onDismiss={() => setNotice(null)} />
    </div>
  );
}

function focusSearch() {
  const input = document.getElementById(SEARCH_INPUT_ID);
  if (input instanceof HTMLInputElement) {
    input.focus();
    input.select();
  }
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
