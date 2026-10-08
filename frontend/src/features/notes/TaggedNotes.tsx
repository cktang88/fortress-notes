import { useState } from "react";
import { useDeleteTag, useRenameTag } from "./hooks";
import { moveListFocus } from "./listKeyboard";
import { StatusBadge } from "./StatusBadge";
import type { NoteSummary } from "./types";

interface Props {
  tag: string;
  notes: readonly NoteSummary[];
  selectedId: string | null;
  onSelect: (id: string) => void;
  onTagChange: (tag: string | null) => void;
}

/** The notes carrying one tag, with rename and remove for the tag itself. */
export function TaggedNotes({ tag, notes, selectedId, onSelect, onTagChange }: Props) {
  const [renaming, setRenaming] = useState(false);
  const [name, setName] = useState(tag);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const renameTag = useRenameTag();
  const deleteTag = useDeleteTag();
  const tagged = notes.filter((note) => note.tags.includes(tag));
  const error = renameTag.error ?? deleteTag.error;

  return (
    <section aria-label={`Notes tagged ${tag}`} className="flex flex-1 flex-col overflow-hidden">
      <div className="flex flex-col gap-2 border-b border-zinc-100 px-3 py-2">
        {renaming ? (
          <form
            className="flex items-center gap-1"
            onSubmit={(event) => {
              event.preventDefault();
              const next = name.trim();
              if (!next || next === tag) return setRenaming(false);
              renameTag.mutate(
                { tag, name: next },
                {
                  onSuccess: () => {
                    setRenaming(false);
                    onTagChange(next);
                  },
                },
              );
            }}
          >
            <label htmlFor="rename-tag" className="sr-only">
              New tag name
            </label>
            <input
              id="rename-tag"
              autoFocus
              value={name}
              onChange={(event) => setName(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Escape") setRenaming(false);
              }}
              className="min-w-0 flex-1 rounded border border-zinc-300 px-2 py-1 text-sm"
            />
            <button type="submit" className="rounded bg-zinc-900 px-2 py-1 text-xs text-white">
              Rename
            </button>
          </form>
        ) : (
          <div className="flex items-center gap-2">
            <span className="truncate text-sm font-semibold text-zinc-800">#{tag}</span>
            <span className="text-xs text-zinc-400">
              {tagged.length} {tagged.length === 1 ? "note" : "notes"}
            </span>
            <button
              type="button"
              aria-label="Show all notes"
              title="Show all notes"
              onClick={() => onTagChange(null)}
              className="ml-auto rounded px-1.5 text-zinc-400 hover:bg-zinc-100 hover:text-zinc-700"
            >
              ×
            </button>
          </div>
        )}
        {!renaming && (
          <div className="flex gap-2 text-xs">
            <button
              type="button"
              onClick={() => {
                setName(tag);
                setRenaming(true);
              }}
              className="text-zinc-500 hover:text-zinc-800"
            >
              Rename tag
            </button>
            {confirmDelete ? (
              <>
                <button
                  type="button"
                  onClick={() => deleteTag.mutate(tag, { onSuccess: () => onTagChange(null) })}
                  className="font-medium text-red-600"
                >
                  Remove from {tagged.length} {tagged.length === 1 ? "note" : "notes"}
                </button>
                <button
                  type="button"
                  onClick={() => setConfirmDelete(false)}
                  className="text-zinc-500"
                >
                  Cancel
                </button>
              </>
            ) : (
              <button
                type="button"
                onClick={() => setConfirmDelete(true)}
                className="text-zinc-500 hover:text-red-600"
              >
                Remove tag…
              </button>
            )}
          </div>
        )}
        {error && (
          <p role="alert" className="text-xs text-red-600">
            {error.message}
          </p>
        )}
      </div>
      <ul data-results className="flex-1 overflow-y-auto" onKeyDown={moveListFocus}>
        {tagged.length === 0 && (
          <li className="p-4 text-sm text-zinc-400">No notes with this tag.</li>
        )}
        {tagged.map((note) => (
          <li key={note.id}>
            <button
              data-list-item
              onClick={() => onSelect(note.id)}
              className={`flex w-full flex-col gap-1 border-b border-zinc-100 px-3 py-2.5 text-left hover:bg-zinc-50 focus-visible:bg-indigo-50 focus-visible:outline-none ${
                selectedId === note.id ? "bg-zinc-100" : ""
              }`}
            >
              <span className="truncate text-sm font-medium text-zinc-800">{note.title}</span>
              <span className="line-clamp-2 text-xs text-zinc-500">{note.snippet}</span>
              <StatusBadge status={note.status} />
            </button>
          </li>
        ))}
      </ul>
    </section>
  );
}
