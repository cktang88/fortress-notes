import type { RelatedResult } from "./types";

interface Props {
  suggestions: RelatedResult[];
  onLink: (suggestion: RelatedResult) => void;
  onOpen: (documentId: string, blockId: string | null) => void;
  onDismiss: () => void;
  onTurnOff: () => void;
}

/** "You wrote about this before": a quiet card of related paragraphs from other notes. */
export function WritingSuggestions({ suggestions, onLink, onOpen, onDismiss, onTurnOff }: Props) {
  if (suggestions.length === 0) return null;
  return (
    <aside
      aria-label="You wrote about this before"
      className="fixed right-4 bottom-4 z-30 w-80 rounded-lg border border-zinc-200 bg-white/95 p-3 text-sm shadow-lg backdrop-blur"
    >
      <div className="mb-2 flex items-center justify-between">
        <span className="text-[11px] font-semibold tracking-wide text-zinc-500 uppercase">
          You wrote about this before
        </span>
        <span className="flex gap-1 text-xs">
          <button
            type="button"
            onClick={onTurnOff}
            className="rounded px-1.5 text-zinc-400 hover:bg-zinc-100 hover:text-zinc-700"
          >
            Turn off
          </button>
          <button
            type="button"
            aria-label="Dismiss suggestions"
            onClick={onDismiss}
            className="rounded px-1.5 text-zinc-400 hover:bg-zinc-100 hover:text-zinc-700"
          >
            ×
          </button>
        </span>
      </div>
      <ul className="space-y-2">
        {suggestions.map((suggestion) => (
          <li key={suggestion.note.id} className="rounded-md bg-zinc-50 p-2">
            <div className="truncate text-xs font-medium text-zinc-700">
              {suggestion.note.title}
            </div>
            <p className="line-clamp-2 text-xs text-zinc-500">{suggestion.matched_block_text}</p>
            <div className="mt-1 flex gap-2 text-xs">
              <button
                type="button"
                // Keep the cursor in the editor so the link lands where you were typing.
                onMouseDown={(event) => event.preventDefault()}
                onClick={() => onLink(suggestion)}
                title="Add a link to this paragraph at the end of the one you're writing"
                className="rounded border border-zinc-300 bg-white px-2 py-0.5 text-zinc-700 hover:bg-zinc-100"
              >
                Link
              </button>
              <button
                type="button"
                onClick={() => onOpen(suggestion.note.id, suggestion.matched_block_id)}
                className="rounded px-2 py-0.5 text-zinc-500 hover:bg-zinc-100 hover:text-zinc-800"
              >
                Open
              </button>
            </div>
          </li>
        ))}
      </ul>
    </aside>
  );
}
