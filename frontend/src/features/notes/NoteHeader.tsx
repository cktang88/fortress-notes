import { useState } from "react";
import type { Note, NoteStatus } from "./types";

interface Props {
  note: Note;
  onTitle: (title: string) => void;
  onTags: (tags: string[]) => void;
  onDelete: () => void;
  onSetStatus: (status: NoteStatus) => void;
  onFactcheck: () => void;
  onClarify: () => void;
  onConsistency: () => void;
  onLint: () => void;
  checking: boolean;
  rawView: boolean;
  onToggleRaw: () => void;
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
  onTags,
  onDelete,
  onSetStatus,
  onFactcheck,
  onClarify,
  onConsistency,
  onLint,
  checking,
  rawView,
  onToggleRaw,
}: Props) {
  const [newTag, setNewTag] = useState("");

  function addTag() {
    const nextTags = editTags(note.tags, newTag, "add");
    if (nextTags.length !== note.tags.length) onTags(nextTags);
    setNewTag("");
  }

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
        <button
          onClick={onToggleRaw}
          className="shrink-0 rounded-md border border-zinc-300 px-2.5 py-1 text-xs font-medium text-zinc-600 hover:bg-zinc-50"
        >
          {rawView ? "Rich" : "Raw"}
        </button>
      </div>

      <div className="text-xs text-zinc-400">
        Created {formatDate(note.created_at)} · Edited {formatDate(note.updated_at)}
      </div>

      <div className="flex flex-wrap items-center gap-2" aria-label="Document tags">
        {note.tags.map((tag) => (
          <span
            key={tag}
            className="inline-flex items-center gap-1 rounded-full bg-zinc-100 px-2 py-1 text-xs text-zinc-700"
          >
            {tag}
            <button
              type="button"
              aria-label={`Remove tag ${tag}`}
              onClick={() => onTags(editTags(note.tags, tag, "remove"))}
              className="text-zinc-400 hover:text-zinc-700"
            >
              ×
            </button>
          </span>
        ))}
        <form
          onSubmit={(event) => {
            event.preventDefault();
            addTag();
          }}
          className="flex items-center gap-1"
        >
          <label htmlFor="new-document-tag" className="sr-only">
            Add document tag
          </label>
          <input
            id="new-document-tag"
            value={newTag}
            onChange={(event) => setNewTag(event.target.value)}
            placeholder="Add tag"
            className="w-24 rounded border border-zinc-200 px-2 py-1 text-xs text-zinc-700 placeholder:text-zinc-400 focus:border-zinc-400 focus:outline-none"
          />
          <button
            type="submit"
            className="rounded border border-zinc-300 px-2 py-1 text-xs text-zinc-600 hover:bg-zinc-50"
          >
            Add
          </button>
        </form>
      </div>

      <div className="flex items-center gap-2">
        <StatusToggle status={note.status} onSetStatus={onSetStatus} />

        <button
          onClick={onFactcheck}
          disabled={checking}
          className="rounded-md bg-indigo-600 px-3 py-1 text-xs font-medium text-white hover:bg-indigo-500 disabled:opacity-50"
        >
          Fact-check
        </button>
        <button
          onClick={onConsistency}
          disabled={checking}
          className="rounded-md border border-zinc-300 px-2.5 py-1 text-xs font-medium text-zinc-700 hover:bg-zinc-50 disabled:opacity-50"
        >
          Check consistency
        </button>
        <button
          onClick={onLint}
          disabled={checking}
          className="rounded-md border border-zinc-300 px-2.5 py-1 text-xs font-medium text-zinc-700 hover:bg-zinc-50 disabled:opacity-50"
        >
          Lint
        </button>
        {checking && <span className="text-xs text-zinc-400">scanning…</span>}

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

export function editTags(tags: string[], value: string, action: "add" | "remove"): string[] {
  const tag = value.trim();
  if (action === "remove") return tags.filter((existingTag) => existingTag !== tag);
  if (!tag || tags.includes(tag)) return tags;
  return [...tags, tag];
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
