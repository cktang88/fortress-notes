import { useTags } from "./hooks";

interface Props {
  activeTag: string | null;
  onSelectTag: (tag: string | null) => void;
}

/** Browse notes by tag. Clicking a tag shows only the notes that carry it. */
export function TagsPanel({ activeTag, onSelectTag }: Props) {
  const { data, isError } = useTags();
  if (isError) return null;
  const tags = data ?? [];

  return (
    <details className="group border-t border-zinc-200 bg-zinc-50/60">
      <summary className="flex cursor-pointer list-none items-center justify-between px-3 py-2 text-[11px] font-semibold uppercase tracking-wide text-zinc-400 hover:text-zinc-600">
        <span>Tags{tags.length > 0 && ` (${tags.length})`}</span>
        <span aria-hidden className="transition-transform group-open:rotate-90">
          ›
        </span>
      </summary>
      <div className="flex max-h-32 flex-wrap gap-1.5 overflow-y-auto px-3 pb-3">
        {tags.length === 0 && (
          <span className="text-xs text-zinc-400">
            No tags yet. Right-click a note and choose “Edit tags…”.
          </span>
        )}
        {tags.map(({ tag, count }) => (
          <button
            key={tag}
            type="button"
            aria-pressed={activeTag === tag}
            onClick={() => onSelectTag(activeTag === tag ? null : tag)}
            className={`rounded-full px-2 py-0.5 text-xs ${
              activeTag === tag
                ? "bg-zinc-900 text-white"
                : "bg-white text-zinc-600 ring-1 ring-zinc-200 hover:bg-zinc-100"
            }`}
          >
            #{tag} <span className="opacity-60">{count}</span>
          </button>
        ))}
      </div>
    </details>
  );
}
