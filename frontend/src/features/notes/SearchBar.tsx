import { useEffect, useRef, useState } from "react";
import type { BlockSearchFilters, BlockType, NoteStatus, NoteSummary, SearchMode } from "./types";

interface Props {
  query: string;
  onQuery: (q: string) => void;
  mode: SearchMode;
  onMode: (m: SearchMode) => void;
  filters: BlockSearchFilters;
  onFilters: (filters: BlockSearchFilters) => void;
  documents?: readonly NoteSummary[];
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
  onNew,
}: Props) {
  const [helpOpen, setHelpOpen] = useState(false);
  const helpDialogRef = useRef<HTMLDialogElement>(null);
  const updateFilters = (patch: Partial<BlockSearchFilters>) => onFilters({ ...filters, ...patch });

  useEffect(() => {
    const dialog = helpDialogRef.current;
    if (!dialog) return;
    if (helpOpen && !dialog.open) dialog.showModal();
    if (!helpOpen && dialog.open) dialog.close();
  }, [helpOpen]);

  return (
    <div className="flex flex-col gap-2 border-b border-zinc-200 p-3">
      <div className="flex gap-2">
        <input
          value={query}
          onChange={(e) => onQuery(e.target.value)}
          placeholder="Search notes…"
          className="flex-1 rounded-md border border-zinc-300 px-3 py-1.5 text-sm focus:border-zinc-500 focus:outline-none"
        />
        <button
          onClick={() => onNew()}
          className="rounded-md bg-zinc-900 px-3 py-1.5 text-sm font-medium text-white hover:bg-zinc-700"
        >
          + New
        </button>
        <button
          type="button"
          aria-label="Help and tips"
          title="Help and tips"
          onClick={() => setHelpOpen(true)}
          className="rounded-md border border-zinc-300 px-2.5 py-1.5 text-sm text-zinc-600 hover:bg-zinc-100"
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
      </div>
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
        </ul>
      </dialog>
    </div>
  );
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
