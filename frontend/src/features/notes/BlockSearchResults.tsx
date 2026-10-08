import { moveListFocus } from "./listKeyboard";
import type { UnifiedSearchResult } from "./types";

interface Props {
  results: UnifiedSearchResult[];
  loading: boolean;
  error?: boolean;
  onSelect: (documentId: string, blockId: string) => void;
}

const reasonLabels: Record<UnifiedSearchResult["matched"][number], string> = {
  words: "exact words",
  meaning: "similar meaning",
  title: "in title",
};

/** Search results: the matching passage with your words highlighted, and why it matched. */
export function BlockSearchResults({ results, loading, error = false, onSelect }: Props) {
  if (loading) return <div className="p-4 text-sm text-zinc-400">Searching…</div>;
  if (error) {
    return <div className="p-4 text-sm text-red-600">Search is unavailable right now.</div>;
  }
  if (results.length === 0) {
    return <div className="p-4 text-sm text-zinc-400">Nothing matches.</div>;
  }

  return (
    <ul data-results className="flex-1 overflow-y-auto" onKeyDown={moveListFocus}>
      {results.map((result) => (
        <li key={result.block_id}>
          <button
            data-list-item
            onClick={() => onSelect(result.document_id, result.block_id)}
            className="flex w-full flex-col gap-1 border-b border-zinc-100 px-3 py-2.5 text-left hover:bg-zinc-50 focus-visible:bg-indigo-50 focus-visible:outline-none"
          >
            <div className="flex items-center justify-between gap-2">
              <span className="truncate text-sm font-medium text-zinc-800">
                {result.document_title}
              </span>
              <span className="shrink-0 text-[10px] text-zinc-400">
                {result.matched.map((reason) => reasonLabels[reason]).join(" · ")}
              </span>
            </div>
            <span className="line-clamp-3 text-xs text-zinc-500">
              <Highlighted text={result.text} ranges={result.highlights} />
            </span>
          </button>
        </li>
      ))}
    </ul>
  );
}

function Highlighted({ text, ranges }: { text: string; ranges: number[][] }) {
  const parts: React.ReactNode[] = [];
  let cursor = 0;
  for (const [start, end] of ranges) {
    if (start < cursor || end > text.length) continue;
    if (start > cursor) parts.push(text.slice(cursor, start));
    parts.push(
      <mark key={start} className="rounded-sm bg-amber-100 px-0.5 text-zinc-900">
        {text.slice(start, end)}
      </mark>,
    );
    cursor = end;
  }
  parts.push(text.slice(cursor));
  return <>{parts}</>;
}
