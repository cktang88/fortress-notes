import { useId, useState } from "react";
import { Modal } from "./Modal";

interface Props {
  open: boolean;
  title: string;
  tags: readonly string[];
  suggestions: readonly string[];
  onChange: (tags: string[]) => void;
  onClose: () => void;
}

/** Edit one note's tags. Changes save as you make them. */
export function TagEditorDialog({ open, title, tags, suggestions, onChange, onClose }: Props) {
  const [draft, setDraft] = useState("");
  const listId = useId();

  function add(value: string) {
    const next = value
      .split(",")
      .map((tag) => tag.trim().replace(/^#/, ""))
      .filter((tag) => tag && !tags.includes(tag));
    if (next.length > 0) onChange([...tags, ...new Set(next)]);
    setDraft("");
  }

  return (
    <Modal
      open={open}
      title={`Tags for “${title}”`}
      onClose={() => {
        if (draft.trim()) add(draft);
        onClose();
      }}
    >
      <div className="flex flex-wrap gap-1.5" aria-label="Current tags">
        {tags.length === 0 && <span className="text-sm text-zinc-400">No tags yet.</span>}
        {tags.map((tag) => (
          <span
            key={tag}
            className="inline-flex items-center gap-1 rounded-full bg-zinc-100 px-2 py-1 text-xs text-zinc-700"
          >
            #{tag}
            <button
              type="button"
              aria-label={`Remove tag ${tag}`}
              onClick={() => onChange(tags.filter((existing) => existing !== tag))}
              className="text-zinc-400 hover:text-zinc-700"
            >
              ×
            </button>
          </span>
        ))}
      </div>
      <form
        className="mt-3 flex gap-2"
        onSubmit={(event) => {
          event.preventDefault();
          add(draft);
        }}
      >
        <label htmlFor={`${listId}-input`} className="sr-only">
          Add a tag
        </label>
        <input
          id={`${listId}-input`}
          data-autofocus
          list={listId}
          value={draft}
          onChange={(event) => {
            const value = event.target.value;
            if (value.endsWith(",")) add(value);
            else setDraft(value);
          }}
          placeholder="Add a tag and press Enter"
          className="flex-1 rounded-md border border-zinc-300 px-3 py-1.5 text-sm focus:border-zinc-500 focus:outline-none"
        />
        <datalist id={listId}>
          {suggestions
            .filter((tag) => !tags.includes(tag))
            .map((tag) => (
              <option key={tag} value={tag} />
            ))}
        </datalist>
        <button
          type="submit"
          className="rounded-md border border-zinc-300 px-3 py-1.5 text-sm text-zinc-700 hover:bg-zinc-50"
        >
          Add
        </button>
      </form>
    </Modal>
  );
}
