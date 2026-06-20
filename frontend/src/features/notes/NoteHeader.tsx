import { useState } from "react";
import { StatusBadge } from "./StatusBadge";
import type { Note, ReviewKind } from "./types";

interface Props {
  note: Note;
  onTitle: (title: string) => void;
  onPromote: () => void;
  onDelete: () => void;
  onReview: (kind: ReviewKind) => void;
  onConsistency: () => void;
  onHeal: () => void;
}

const REVIEW_OPTIONS: { kind: ReviewKind; label: string }[] = [
  { kind: "factcheck", label: "Fact-check" },
  { kind: "clarify", label: "Ask clarifying questions" },
  { kind: "object", label: "Object / find inconsistencies" },
];

export function NoteHeader({
  note,
  onTitle,
  onPromote,
  onDelete,
  onReview,
  onConsistency,
  onHeal,
}: Props) {
  const [menuOpen, setMenuOpen] = useState(false);

  return (
    <div className="flex flex-col gap-3 border-b border-zinc-200 px-8 pt-6 pb-3">
      <input
        value={note.title}
        onChange={(e) => onTitle(e.target.value)}
        className="text-2xl font-bold text-zinc-900 focus:outline-none"
        placeholder="Untitled"
      />
      <div className="flex items-center gap-2">
        <StatusBadge status={note.status} />
        {note.status === "rough" && (
          <button
            onClick={onPromote}
            className="rounded-md border border-emerald-300 px-2.5 py-1 text-xs font-medium text-emerald-700 hover:bg-emerald-50"
          >
            Promote to polished
          </button>
        )}

        <div className="relative">
          <button
            onClick={() => setMenuOpen((o) => !o)}
            className="rounded-md bg-indigo-600 px-3 py-1 text-xs font-medium text-white hover:bg-indigo-500"
          >
            AI review ▾
          </button>
          {menuOpen && (
            <div className="absolute z-10 mt-1 w-60 rounded-md border border-zinc-200 bg-white py-1 shadow-lg">
              {REVIEW_OPTIONS.map((o) => (
                <button
                  key={o.kind}
                  onClick={() => {
                    setMenuOpen(false);
                    onReview(o.kind);
                  }}
                  className="block w-full px-3 py-1.5 text-left text-xs text-zinc-700 hover:bg-zinc-50"
                >
                  {o.label}
                </button>
              ))}
            </div>
          )}
        </div>

        <button
          onClick={onConsistency}
          className="rounded-md border border-zinc-300 px-2.5 py-1 text-xs font-medium text-zinc-700 hover:bg-zinc-50"
        >
          Check consistency
        </button>
        <button
          onClick={onHeal}
          className="rounded-md border border-zinc-300 px-2.5 py-1 text-xs font-medium text-zinc-700 hover:bg-zinc-50"
        >
          Heal
        </button>

        <button
          onClick={onDelete}
          className="ml-auto rounded-md px-2.5 py-1 text-xs text-zinc-400 hover:bg-red-50 hover:text-red-600"
        >
          Delete
        </button>
      </div>
    </div>
  );
}
