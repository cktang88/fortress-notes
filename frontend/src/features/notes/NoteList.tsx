import { StatusBadge } from "./StatusBadge";
import type { NoteSummary } from "./types";

interface Props {
  notes: NoteSummary[];
  selectedId: string | null;
  onSelect: (id: string) => void;
  scores?: Record<string, number>;
  loading?: boolean;
}

export function NoteList({ notes, selectedId, onSelect, scores, loading }: Props) {
  if (loading) return <div className="p-4 text-sm text-zinc-400">Loading…</div>;
  if (notes.length === 0) return <div className="p-4 text-sm text-zinc-400">No notes.</div>;

  return (
    <ul className="flex-1 overflow-y-auto">
      {notes.map((note) => (
        <li key={note.id}>
          <button
            onClick={() => onSelect(note.id)}
            className={`flex w-full flex-col gap-1 border-b border-zinc-100 px-3 py-2.5 text-left hover:bg-zinc-50 ${
              selectedId === note.id ? "bg-zinc-100" : ""
            }`}
          >
            <div className="flex items-center justify-between gap-2">
              <span className="truncate text-sm font-medium text-zinc-800">{note.title}</span>
              {scores?.[note.id] !== undefined && (
                <span className="shrink-0 text-[10px] text-zinc-400">
                  {scores[note.id].toFixed(2)}
                </span>
              )}
            </div>
            <span className="line-clamp-2 text-xs text-zinc-500">{note.snippet}</span>
            <StatusBadge status={note.status} />
          </button>
        </li>
      ))}
    </ul>
  );
}
