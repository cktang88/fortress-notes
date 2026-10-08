import { useEffect, useRef, useState } from "react";
import { focusFirstResult, isEditableTarget, SEARCH_INPUT_ID } from "./listKeyboard";
import type {
  BlockSearchFilters,
  BlockType,
  NoteStatus,
  NoteSummary,
  SavedSearch,
  SearchMode,
} from "./types";

interface Props {
  query: string;
  onQuery: (q: string) => void;
  mode: SearchMode;
  onMode: (m: SearchMode) => void;
  filters: BlockSearchFilters;
  onFilters: (filters: BlockSearchFilters) => void;
  documents?: readonly NoteSummary[];
  tags?: readonly string[];
  savedSearches?: readonly SavedSearch[];
  onSaveSearch?: () => void;
  onApplySavedSearch?: (search: SavedSearch) => void;
  onDeleteSavedSearch?: (id: string) => void;
  /** Extra controls shown beside the New button (the workspace menu). */
  actions?: React.ReactNode;
  onNew: () => void;
}

const blockTypes: { value: BlockType; label: string }[] = [
  { value: "paragraph", label: "Paragraph" },
  { value: "heading", label: "Heading" },
  { value: "list", label: "List" },
  { value: "quote", label: "Quote" },
  { value: "code", label: "Code" },
  { value: "thematic_break", label: "Divider" },
];

const statuses: { value: NoteStatus; label: string }[] = [
  { value: "rough", label: "Rough" },
  { value: "polished", label: "Polished" },
];

export function SearchBar({
  query,
  onQuery,
  mode,
  onMode,
  filters,
  onFilters,
  documents = [],
  tags = [],
  savedSearches = [],
  onSaveSearch,
  onApplySavedSearch,
  onDeleteSavedSearch,
  actions,
  onNew,
}: Props) {
  const searching = query.trim().length > 0;
  const alreadySaved = savedSearches.some(
    (saved) =>
      saved.query === query.trim() &&
      saved.mode === mode &&
      sameFilters(saved.filters, mode === "text" ? filters : {}),
  );
  const [helpOpen, setHelpOpen] = useState(false);
  const helpDialogRef = useRef<HTMLDialogElement>(null);
  const updateFilters = (patch: Partial<BlockSearchFilters>) => onFilters({ ...filters, ...patch });

  useEffect(() => {
    const dialog = helpDialogRef.current;
    if (!dialog) return;
    if (helpOpen && !dialog.open) dialog.showModal();
    if (!helpOpen && dialog.open) dialog.close();
  }, [helpOpen]);

  useEffect(() => {
    const openHelp = (event: KeyboardEvent) => {
      if (
        event.key === "?" &&
        !isEditableTarget(event.target) &&
        !event.metaKey &&
        !event.ctrlKey
      ) {
        event.preventDefault();
        setHelpOpen(true);
      }
    };
    document.addEventListener("keydown", openHelp);
    return () => document.removeEventListener("keydown", openHelp);
  }, []);

  return (
    <div className="flex flex-col gap-2 border-b border-zinc-200 p-3">
      <div className="flex items-center gap-1.5">
        <input
          id={SEARCH_INPUT_ID}
          type="search"
          aria-label="Search notes"
          value={query}
          onChange={(e) => onQuery(e.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Escape") {
              event.preventDefault();
              onQuery("");
              event.currentTarget.blur();
            } else if (event.key === "ArrowDown") {
              event.preventDefault();
              focusFirstResult();
            } else if (event.key === "Enter") {
              event.preventDefault();
              focusFirstResult()?.click();
            }
          }}
          placeholder="Search… (⌘K)"
          className="min-w-0 flex-1 rounded-md border border-zinc-300 px-3 py-1.5 text-sm focus:border-zinc-500 focus:outline-none"
        />
        <button
          onClick={() => onNew()}
          title="New note (Alt+N)"
          className="shrink-0 whitespace-nowrap rounded-md bg-zinc-900 px-3 py-1.5 text-sm font-medium text-white hover:bg-zinc-700"
        >
          + New
        </button>
        {actions}
        <button
          type="button"
          aria-label="Help and tips"
          title="Help and tips"
          onClick={() => setHelpOpen(true)}
          className="shrink-0 rounded-md border border-zinc-300 px-2.5 py-1.5 text-sm text-zinc-600 hover:bg-zinc-100"
        >
          ?
        </button>
      </div>
      <div className="flex gap-1 text-xs">
        <ModeButton active={mode === "text"} onClick={() => onMode("text")}>
          Full-text
        </ModeButton>
        <ModeButton active={mode === "embedding"} onClick={() => onMode("embedding")}>
          Embedding
        </ModeButton>
        {searching && onSaveSearch && (
          <button
            type="button"
            disabled={alreadySaved}
            onClick={onSaveSearch}
            className="ml-auto rounded-full px-2 py-1 text-zinc-500 hover:bg-zinc-100 hover:text-zinc-800 disabled:text-zinc-400 disabled:hover:bg-transparent"
          >
            {alreadySaved ? "★ Saved" : "☆ Save search"}
          </button>
        )}
      </div>
      {!searching && savedSearches.length > 0 && (
        <div aria-label="Saved searches" role="group" className="flex flex-wrap gap-1.5">
          {savedSearches.map((saved) => (
            <span
              key={saved.id}
              className="inline-flex items-center rounded-full bg-amber-50 text-xs text-amber-900 ring-1 ring-amber-200"
            >
              <button
                type="button"
                title={`Search for “${saved.query}”`}
                onClick={() => onApplySavedSearch?.(saved)}
                className="max-w-40 truncate py-0.5 pr-1 pl-2 hover:underline"
              >
                ★ {saved.name}
              </button>
              <button
                type="button"
                aria-label={`Forget saved search ${saved.name}`}
                onClick={() => onDeleteSavedSearch?.(saved.id)}
                className="rounded-full px-1.5 text-amber-700/60 hover:text-amber-900"
              >
                ×
              </button>
            </span>
          ))}
        </div>
      )}
      {mode === "text" && (
        <fieldset className="flex flex-wrap gap-1.5" aria-label="Block search filters">
          <select
            aria-label="Block type"
            value={filters.blockType ?? ""}
            onChange={(event) =>
              updateFilters({
                blockType: (event.target.value || undefined) as BlockType | undefined,
              })
            }
            className="rounded border border-zinc-200 bg-white px-2 py-1 text-xs text-zinc-700"
          >
            <option value="">All blocks</option>
            {blockTypes.map((type) => (
              <option key={type.value} value={type.value}>
                {type.label}
              </option>
            ))}
          </select>
          <select
            aria-label="Document status"
            value={filters.status ?? ""}
            onChange={(event) =>
              updateFilters({ status: (event.target.value || undefined) as NoteStatus | undefined })
            }
            className="rounded border border-zinc-200 bg-white px-2 py-1 text-xs text-zinc-700"
          >
            <option value="">All statuses</option>
            {statuses.map((status) => (
              <option key={status.value} value={status.value}>
                {status.label}
              </option>
            ))}
          </select>
          {tags.length > 0 && (
            <select
              aria-label="Tag"
              value={filters.tag ?? ""}
              onChange={(event) => updateFilters({ tag: event.target.value || undefined })}
              className="max-w-32 rounded border border-zinc-200 bg-white px-2 py-1 text-xs text-zinc-700"
            >
              <option value="">All tags</option>
              {tags.map((tag) => (
                <option key={tag} value={tag}>
                  #{tag}
                </option>
              ))}
            </select>
          )}
          <select
            aria-label="Document"
            value={filters.documentId ?? ""}
            onChange={(event) => updateFilters({ documentId: event.target.value || undefined })}
            className="max-w-40 rounded border border-zinc-200 bg-white px-2 py-1 text-xs text-zinc-700"
          >
            <option value="">All documents</option>
            {documents.map((document) => (
              <option key={document.id} value={document.id}>
                {document.title}
              </option>
            ))}
          </select>
          <label className="flex items-center gap-1 text-xs text-zinc-500">
            After
            <input
              aria-label="Updated after"
              type="date"
              value={filters.updatedAfter ?? ""}
              onChange={(event) => updateFilters({ updatedAfter: event.target.value || undefined })}
              className="rounded border border-zinc-200 px-1 py-1 text-xs text-zinc-700"
            />
          </label>
          <label className="flex items-center gap-1 text-xs text-zinc-500">
            Before
            <input
              aria-label="Updated before"
              type="date"
              value={filters.updatedBefore ?? ""}
              onChange={(event) =>
                updateFilters({ updatedBefore: event.target.value || undefined })
              }
              className="rounded border border-zinc-200 px-1 py-1 text-xs text-zinc-700"
            />
          </label>
        </fieldset>
      )}
      <dialog
        ref={helpDialogRef}
        aria-labelledby="help-title"
        onCancel={(event) => {
          event.preventDefault();
          setHelpOpen(false);
        }}
        className="m-auto w-full max-w-md rounded-lg bg-white p-5 shadow-xl backdrop:bg-black/30"
      >
        <div className="mb-3 flex items-center justify-between gap-4">
          <h2 id="help-title" className="text-base font-semibold text-zinc-900">
            Quick tips
          </h2>
          <button
            type="button"
            aria-label="Close help"
            autoFocus
            onClick={() => setHelpOpen(false)}
            className="rounded px-2 py-1 text-zinc-500 hover:bg-zinc-100"
          >
            ×
          </button>
        </div>
        <ul className="space-y-2 text-sm text-zinc-700">
          <li>Right-click the sidebar or a folder to add a folder or note inside it.</li>
          <li>Double-click a note or folder name to rename it.</li>
          <li>
            Drag notes or folders into a folder. Drop on All notes to move them to the top level.
          </li>
          <li>Highlight text in one or more blocks to fact-check them.</li>
          <li>
            Links are checked automatically about once a month; broken links are marked in the
            editor.
          </li>
          <li>Related notes follow the block you last focused in the editor.</li>
          <li>
            Right-click a note and choose “Edit tags…”; browse tags at the bottom of the sidebar.
          </li>
          <li>Deleted notes go to Trash. Import, export, Trash and backups live in the ⋯ menu.</li>
        </ul>
        <h3 className="mt-4 mb-2 text-sm font-semibold text-zinc-900">Keyboard shortcuts</h3>
        <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1.5 text-sm text-zinc-700">
          <Shortcut keys={["⌘/Ctrl", "K"]}>Search notes</Shortcut>
          <Shortcut keys={["/"]}>Search (when not typing)</Shortcut>
          <Shortcut keys={["Alt", "N"]}>New note</Shortcut>
          <Shortcut keys={["↓", "↑"]}>Move through search results</Shortcut>
          <Shortcut keys={["Enter"]}>Open the highlighted result</Shortcut>
          <Shortcut keys={["Esc"]}>Clear the search</Shortcut>
          <Shortcut keys={["⌘/Ctrl", "/"]}>Insert a block in the editor</Shortcut>
          <Shortcut keys={["?"]}>Show these tips</Shortcut>
        </dl>
      </dialog>
    </div>
  );
}

function Shortcut({ keys, children }: { keys: string[]; children: React.ReactNode }) {
  return (
    <>
      <dt className="flex gap-1">
        {keys.map((key) => (
          <kbd
            key={key}
            className="rounded border border-zinc-300 bg-zinc-50 px-1.5 font-sans text-xs text-zinc-700"
          >
            {key}
          </kbd>
        ))}
      </dt>
      <dd>{children}</dd>
    </>
  );
}

function sameFilters(a: BlockSearchFilters, b: BlockSearchFilters) {
  const keys = new Set([...Object.keys(a), ...Object.keys(b)]) as Set<keyof BlockSearchFilters>;
  return [...keys].every((key) => (a[key] || undefined) === (b[key] || undefined));
}

function ModeButton({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      onClick={onClick}
      className={`rounded-full px-3 py-1 ${
        active ? "bg-zinc-900 text-white" : "bg-zinc-100 text-zinc-600 hover:bg-zinc-200"
      }`}
    >
      {children}
    </button>
  );
}
