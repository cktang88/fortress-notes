import type { Note, NoteStatus } from "./types";

interface Props {
  note: Note;
  onTitle: (title: string) => void;
  onDelete: () => void;
  onSetStatus: (status: NoteStatus) => void;
  onClarify: () => void;
  checking: boolean;
  /** URL that downloads this note as Markdown. */
  exportUrl?: string;
}

function formatDate(iso: string): string {
  return new Date(iso).toLocaleString(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  });
}

export function NoteHeader({
  note,
  onTitle,
  onDelete,
  onSetStatus,
  onClarify,
  checking,
  exportUrl,
}: Props) {
  return (
    <div className="flex flex-col gap-3 border-b border-zinc-200 px-8 pt-6 pb-3">
      <div className="flex items-start gap-2">
        <input
          value={note.title}
          onChange={(e) => onTitle(e.target.value)}
          className="flex-1 text-2xl font-bold text-zinc-900 focus:outline-none"
          placeholder="Untitled"
        />
        <button
          onClick={onClarify}
          disabled={checking}
          className="shrink-0 rounded-md border border-zinc-300 px-2.5 py-1 text-xs font-medium text-zinc-600 hover:bg-zinc-50 disabled:opacity-50"
        >
          Clarify
        </button>
      </div>

      <div className="text-xs text-zinc-400">
        Created {formatDate(note.created_at)} · Edited {formatDate(note.updated_at)}
      </div>

      <div className="flex items-center gap-2">
        <StatusToggle status={note.status} onSetStatus={onSetStatus} />

        {checking && <span className="text-xs text-zinc-400">scanning…</span>}

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
