import { useEffect, useEffectEvent, useState } from "react";
import { ApiError, workspaceApi } from "./features/notes/api";
import { BacklinksPanel } from "./features/notes/BacklinksPanel";
import { BlockNoteEditor } from "./features/notes/BlockNoteEditor";
import { BlockSearchResults } from "./features/notes/BlockSearchResults";
import { CaptureBox, CAPTURE_INPUT_ID } from "./features/notes/CaptureBox";
import { DocumentSidebar } from "./features/notes/DocumentSidebar";
import { isEditableTarget, SEARCH_INPUT_ID } from "./features/notes/listKeyboard";
import { NoteHeader } from "./features/notes/NoteHeader";
import { NoteList } from "./features/notes/NoteList";
import { OutlinePanel } from "./features/notes/OutlinePanel";
import { RelatedNotes } from "./features/notes/RelatedNotes";
import { SearchBar } from "./features/notes/SearchBar";
import { Toast, type Notice } from "./features/notes/Toast";
import { useNoteSelection } from "./features/notes/useNoteSelection";
import { WorkspaceMenu } from "./features/notes/WorkspaceMenu";
import {
  useBlockDocument,
  useCapture,
  useBlockSearch,
  useCreateDocument,
  useCreateFolder,
  useDebouncedValue,
  useDeleteFolder,
  useDeleteSavedSearch,
  useLinkChecks,
  useMoveDocument,
  useMoveFolder,
  useNavigation,
  useNotes,
  useRenameFolder,
  useRestoreDocument,
  useSavedSearches,
  useSaveSearch,
  useSearch,
  useTrashDocument,
  useUpdateDocument,
} from "./features/notes/hooks";
import type { BlockSearchFilters, SavedSearch, SearchMode } from "./features/notes/types";

export function App() {
  const selection = useNoteSelection();
  const { selectedId } = selection;
  const [query, setQuery] = useState("");
  const [mode, setMode] = useState<SearchMode>("text");
  const [blockSearchFilters, setBlockSearchFilters] = useState<BlockSearchFilters>({});
  const [notice, setNotice] = useState<Notice | null>(null);
  const [justCreatedId, setJustCreatedId] = useState<string | null>(null);

  const settledQuery = useDebouncedValue(query.trim());
  const searching = query.trim().length > 0;
  const hasBlockSearchFilters = Object.values(blockSearchFilters).some(Boolean);
  const allNotes = useNotes();
  const navigation = useNavigation();
  const blockSearch = useBlockSearch(settledQuery, blockSearchFilters, mode === "text");
  const useNoteLevelSearch =
    mode === "embedding" || (blockSearch.isError && !hasBlockSearchFilters);
  const noteSearch = useSearch(settledQuery, mode, useNoteLevelSearch);
  const document = useBlockDocument(selectedId);
  const linkChecks = useLinkChecks(selectedId);

  const createDocument = useCreateDocument();
  const updateDocument = useUpdateDocument();
  const trashDocument = useTrashDocument();
  const restoreDocument = useRestoreDocument();
  const createFolder = useCreateFolder();
  const renameFolder = useRenameFolder();
  const deleteFolder = useDeleteFolder();
  const moveDocument = useMoveDocument();
  const moveFolder = useMoveFolder();
  const capture = useCapture();
  const savedSearches = useSavedSearches();
  const saveSearch = useSaveSearch();
  const deleteSavedSearch = useDeleteSavedSearch();

  const showNotice = (next: Omit<Notice, "id">) => setNotice({ ...next, id: Date.now() });
  const showError = (error: unknown) =>
    showNotice({ tone: "error", message: error instanceof Error ? error.message : String(error) });

  const selectNote = (id: string, blockId: string | null = null) => {
    selection.select(id, blockId);
  };

  const handleNew = async (folderId: string | null = null) => {
    try {
      const created = await createDocument.mutateAsync({ folderId });
      setJustCreatedId(created.id);
      selectNote(created.id);
    } catch (error) {
      showError(error);
    }
  };

  const handleDelete = async (id: string) => {
    const title =
      (id === selectedId ? document.data?.title : undefined) ??
      allNotes.data?.find((item) => item.id === id)?.title ??
      "note";
    try {
      await trashDocument.mutateAsync(id);
    } catch (error) {
      showError(error);
      return;
    }
    if (id === selectedId) selection.clear();
    showNotice({
      message: `Moved “${title}” to Trash`,
      action: {
        label: "Undo",
        run: () =>
          restoreDocument.mutate(id, { onSuccess: () => selectNote(id), onError: showError }),
      },
    });
  };

  const applySavedSearch = (saved: SavedSearch) => {
    setMode(saved.mode);
    setBlockSearchFilters(saved.filters ?? {});
    setQuery(saved.query);
  };

  const handleSaveSearch = () => {
    const savedQuery = query.trim();
    saveSearch.mutate(
      {
        name: savedQuery,
        query: savedQuery,
        mode,
        filters: mode === "text" ? blockSearchFilters : {},
      },
      {
        onSuccess: () => showNotice({ message: `Saved search “${savedQuery}”` }),
        onError: showError,
      },
    );
  };

  // Global shortcuts: ⌘/Ctrl+K or "/" to search, Alt+N for a new note. Alt+N is ignored
  // inside text fields, where Option+N on a Mac types accents such as ñ.
  const onShortcut = useEffectEvent((event: KeyboardEvent) => {
    const mod = event.metaKey || event.ctrlKey;
    if (mod && event.shiftKey && event.code === "Space") {
      // Quick capture works from anywhere, even mid-sentence in a note.
      event.preventDefault();
      window.document.getElementById(CAPTURE_INPUT_ID)?.focus();
    } else if (mod && !event.shiftKey && !event.altKey && event.key.toLowerCase() === "k") {
      event.preventDefault();
      focusSearch();
    } else if (event.key === "/" && !mod && !isEditableTarget(event.target)) {
      event.preventDefault();
      focusSearch();
    } else if (event.altKey && !mod && event.code === "KeyN" && !isEditableTarget(event.target)) {
      event.preventDefault();
      void handleNew();
    }
  });

  useEffect(() => {
    const listener = (event: KeyboardEvent) => onShortcut(event);
    window.document.addEventListener("keydown", listener);
    return () => window.document.removeEventListener("keydown", listener);
  }, []);

  const noteResults = noteSearch.data ?? [];
  const searchSettling = settledQuery !== query.trim();

  return (
    <div className="flex h-screen text-zinc-900">
      <aside className="flex w-80 flex-col border-r border-zinc-200">
        <SearchBar
          query={query}
          onQuery={setQuery}
          mode={mode}
          onMode={setMode}
          filters={blockSearchFilters}
          onFilters={setBlockSearchFilters}
          documents={allNotes.data ?? []}
          savedSearches={savedSearches.data ?? []}
          onSaveSearch={handleSaveSearch}
          onApplySavedSearch={applySavedSearch}
          onDeleteSavedSearch={(id) => deleteSavedSearch.mutate(id, { onError: showError })}
          onNew={() => void handleNew()}
          actions={<WorkspaceMenu onNotice={showNotice} onOpenNote={(id) => selectNote(id)} />}
        />
        <CaptureBox
          onCapture={(text) =>
            capture.mutateAsync(text).then(
              (result) =>
                showNotice({
                  message: `Saved “${result.title}” to ${result.folder}`,
                  action: { label: "Open", run: () => selectNote(result.document_id) },
                }),
              (error: unknown) => {
                showError(error);
                throw error;
              },
            )
          }
        />
        {searching ? (
          useNoteLevelSearch ? (
            <NoteList
              notes={noteResults.map((result) => result.note)}
              selectedId={selectedId}
              onSelect={selectNote}
              scores={Object.fromEntries(noteResults.map((r) => [r.note.id, r.score]))}
              loading={noteSearch.isLoading || (searchSettling && !noteSearch.data)}
            />
          ) : (
            <BlockSearchResults
              results={blockSearch.data ?? []}
              loading={blockSearch.isLoading || (searchSettling && !blockSearch.data)}
              error={blockSearch.isError}
              onSelect={(documentId, blockId) => selectNote(documentId, blockId)}
            />
          )
        ) : navigation.isError ? (
          <NoteList
            notes={allNotes.data ?? []}
            selectedId={selectedId}
            onSelect={selectNote}
            loading={allNotes.isLoading}
          />
        ) : (
          <DocumentSidebar
            nodes={navigation.data?.items ?? []}
            recent={navigation.data?.recent ?? []}
            selectedId={selectedId}
            loading={navigation.isLoading}
            onSelect={selectNote}
            onCreateFolder={(name, parentId) => createFolder.mutateAsync({ name, parentId })}
            onCreateNote={(folderId) => handleNew(folderId)}
            onRenameFolder={(id, name) => renameFolder.mutateAsync({ id, name })}
            onRenameDocument={(id, title) => updateDocument.mutateAsync({ id, title })}
            onDeleteFolder={(id) => deleteFolder.mutateAsync(id)}
            onDeleteDocument={(id) => handleDelete(id)}
            onMoveDocument={(id, folderId) => moveDocument.mutateAsync({ id, folderId })}
            onMoveFolder={(id, parentId) => moveFolder.mutateAsync({ id, parentId })}
          />
        )}
        <OutlinePanel
          document={document.data ?? null}
          onFocus={(blockId) => selectedId && selectNote(selectedId, blockId)}
        />
        <RelatedNotes noteId={selectedId} blockId={selection.activeBlockId} onSelect={selectNote} />
        <BacklinksPanel documentId={selectedId} onSelect={selectNote} />
      </aside>

      <main className="flex flex-1 flex-col overflow-y-auto">
        {!selectedId ? (
          <EmptyState />
        ) : document.data ? (
          <>
            <NoteHeader
              key={`header:${document.data.id}`}
              note={document.data}
              autoFocusTitle={justCreatedId === document.data.id}
              onTitle={(title) =>
                updateDocument.mutate({ id: document.data!.id, title }, { onError: showError })
              }
              onSetStatus={(status) =>
                updateDocument.mutate({ id: document.data!.id, status }, { onError: showError })
              }
              onDelete={() => void handleDelete(document.data!.id)}
              exportUrl={workspaceApi.documentMarkdownUrl(document.data.id)}
            />
            <BlockNoteEditor
              key={document.data.id}
              document={document.data}
              focusBlockId={selection.focus?.blockId ?? null}
              focusNonce={selection.focus?.nonce ?? 0}
              linkTargets={allNotes.data ?? []}
              linkChecks={linkChecks.data?.links ?? []}
              onFocusedBlockChange={selection.setActiveBlockId}
            />
          </>
        ) : document.isError ? (
          <MissingNote
            notFound={document.error instanceof ApiError && document.error.status === 404}
            onRetry={() => void document.refetch()}
            onClose={selection.clear}
          />
        ) : (
          <div className="p-8 text-sm text-zinc-400">Loading…</div>
        )}
      </main>
      <Toast notice={notice} onDismiss={() => setNotice(null)} />
    </div>
  );
}

function EmptyState() {
  return (
    <div className="flex flex-1 flex-col items-center justify-center gap-2 text-zinc-400">
      <p>Select a note or create a new one.</p>
      <p className="text-xs">
        <kbd className="rounded border border-zinc-300 px-1">Alt</kbd>+
        <kbd className="rounded border border-zinc-300 px-1">N</kbd> new note ·{" "}
        <kbd className="rounded border border-zinc-300 px-1">⌘K</kbd> search ·{" "}
        <kbd className="rounded border border-zinc-300 px-1">?</kbd> tips
      </p>
    </div>
  );
}

function MissingNote({
  notFound,
  onRetry,
  onClose,
}: {
  notFound: boolean;
  onRetry: () => void;
  onClose: () => void;
}) {
  return (
    <div role="alert" className="flex flex-1 flex-col items-center justify-center gap-3 text-sm">
      <p className="text-zinc-600">
        {notFound
          ? "This note doesn't exist or is in the Trash."
          : "This note couldn't be loaded. Your saved work is safe."}
      </p>
      <div className="flex gap-2">
        {!notFound && (
          <button
            type="button"
            onClick={onRetry}
            className="rounded-md bg-zinc-900 px-3 py-1.5 text-white hover:bg-zinc-700"
          >
            Try again
          </button>
        )}
        <button
          type="button"
          onClick={onClose}
          className="rounded-md border border-zinc-300 px-3 py-1.5 text-zinc-700 hover:bg-zinc-50"
        >
          Close
        </button>
      </div>
    </div>
  );
}

function focusSearch() {
  const input = window.document.getElementById(SEARCH_INPUT_ID);
  if (input instanceof HTMLInputElement) {
    input.focus();
    input.select();
  }
}
