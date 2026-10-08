import { useEffect, useRef, useState } from "react";
import type { NoteStatus } from "./types";

interface HeaderDocument {
  id: string;
  title: string;
  status: NoteStatus;
  created_at: string;
  updated_at: string;
}

interface Props {
  note: HeaderDocument;
  /** Called with the finished title (after a pause, on Enter, or on blur). */
  onTitle: (title: string) => void;
  onDelete: () => void;
  onSetStatus: (status: NoteStatus) => void;
  /** URL that downloads this note as Markdown. */
  exportUrl?: string;
  /** Focus and select the title, e.g. right after creating the note. */
  autoFocusTitle?: boolean;
}

const TITLE_SAVE_DELAY_MS = 600;

function formatDate(iso: string): string {
  return new Date(iso).toLocaleString(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  });
}

/**
 * Render with `key={note.id}` so the draft starts fresh for each note.
 */
export function NoteHeader({
  note,
  onTitle,
  onDelete,
  onSetStatus,
  exportUrl,
  autoFocusTitle = false,
}: Props) {
  const [draft, setDraft] = useState(note.title);
  const committed = useRef(note.title);
  const timer = useRef<number | undefined>(undefined);
  const titleRef = useRef<HTMLInputElement>(null);

  // Adopt outside renames (e.g. from the sidebar) while the title isn't being edited.
  useEffect(() => {
    if (note.title === committed.current || document.activeElement === titleRef.current) return;
    committed.current = note.title;
    setDraft(note.title);
  }, [note.title]);

  useEffect(() => {
    if (!autoFocusTitle) return;
    titleRef.current?.focus();
    titleRef.current?.select();
  }, [autoFocusTitle]);

  useEffect(() => () => window.clearTimeout(timer.current), []);

  function commit(value: string) {
    window.clearTimeout(timer.current);
    const title = value.trim() || "Untitled";
    if (title === committed.current) return;
    committed.current = title;
    onTitle(title);
  }

  return (
    <div className="flex flex-col gap-3 border-b border-zinc-200 px-8 pt-6 pb-3">
      <div className="flex items-start gap-2">
        <input
          ref={titleRef}
          aria-label="Note title"
          value={draft}
          onChange={(event) => {
            const value = event.target.value;
            setDraft(value);
            window.clearTimeout(timer.current);
            timer.current = window.setTimeout(() => commit(value), TITLE_SAVE_DELAY_MS);
          }}
          onBlur={() => commit(draft)}
          onKeyDown={(event) => {
            if (event.key === "Enter") {
              event.preventDefault();
              commit(draft);
              focusEditor();
            } else if (event.key === "Escape") {
              window.clearTimeout(timer.current);
              setDraft(committed.current);
            }
          }}
          className="flex-1 text-2xl font-bold text-zinc-900 focus:outline-none"
          placeholder="Untitled"
        />
      </div>

      <div className="text-xs text-zinc-400">
        Created {formatDate(note.created_at)} · Edited {formatDate(note.updated_at)}
      </div>

      <div className="flex items-center gap-2">
        <StatusToggle status={note.status} onSetStatus={onSetStatus} />

        {exportUrl && (
          <a
            href={exportUrl}
            title="Download this note as Markdown"
            className="ml-auto rounded-md px-2.5 py-1 text-xs text-zinc-400 hover:bg-zinc-100 hover:text-zinc-700"
          >
            Export
          </a>
        )}
        <button
          onClick={onDelete}
          title="Move to Trash — you can restore it later"
          className={`${exportUrl ? "" : "ml-auto "}rounded-md px-2.5 py-1 text-xs text-zinc-400 hover:bg-red-50 hover:text-red-600`}
        >
          Delete
        </button>
      </div>
    </div>
  );
}

function focusEditor() {
  document.querySelector<HTMLElement>('[aria-label="Document content"]')?.focus();
}

function StatusToggle({
  status,
  onSetStatus,
}: {
  status: NoteStatus;
  onSetStatus: (status: NoteStatus) => void;
}) {
  return (
    <div className="flex overflow-hidden rounded-md border border-zinc-300 text-xs font-medium">
      <button
        aria-pressed={status === "rough"}
        onClick={() => onSetStatus("rough")}
        className={
          status === "rough"
            ? "bg-amber-500 px-2.5 py-1 text-white"
            : "px-2.5 py-1 text-zinc-500 hover:bg-zinc-50"
        }
      >
        Rough
      </button>
      <button
        aria-pressed={status === "polished"}
        onClick={() => onSetStatus("polished")}
        className={
          status === "polished"
            ? "bg-emerald-600 px-2.5 py-1 text-white"
            : "px-2.5 py-1 text-zinc-500 hover:bg-zinc-50"
        }
      >
        Polished
      </button>
    </div>
  );
}
