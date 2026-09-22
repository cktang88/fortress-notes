import type { BlockSearchResult } from "./types";

interface Props {
  results: BlockSearchResult[];
  loading: boolean;
  error?: boolean;
  onSelect: (documentId: string, blockId: string) => void;
}

export function BlockSearchResults({ results, loading, error = false, onSelect }: Props) {
  if (loading) return <div className="p-4 text-sm text-zinc-400">Searching blocks…</div>;
  if (error) {
    return (
      <div className="p-4 text-sm text-red-600">
        Block search is unavailable; the selected filters were not applied.
      </div>
    );
  }
  if (results.length === 0) {
    return <div className="p-4 text-sm text-zinc-400">No matching blocks.</div>;
  }

  return (
    <ul className="flex-1 overflow-y-auto">
      {results.map((result) => (
        <li key={result.block_id}>
          <button
            onClick={() => onSelect(result.document_id, result.block_id)}
            className="flex w-full flex-col gap-1 border-b border-zinc-100 px-3 py-2.5 text-left hover:bg-zinc-50"
          >
            <div className="flex items-center justify-between gap-2">
              <span className="truncate text-sm font-medium text-zinc-800">
                {result.document_title}
              </span>
              <span className="shrink-0 text-[10px] text-zinc-400">{result.block_type}</span>
            </div>
            <span className="line-clamp-3 text-xs text-zinc-500">{result.text}</span>
          </button>
        </li>
      ))}
    </ul>
  );
}
