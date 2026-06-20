import type { SearchMode } from "./types";

interface Props {
  query: string;
  onQuery: (q: string) => void;
  mode: SearchMode;
  onMode: (m: SearchMode) => void;
  onNew: () => void;
}

export function SearchBar({ query, onQuery, mode, onMode, onNew }: Props) {
  return (
    <div className="flex flex-col gap-2 border-b border-zinc-200 p-3">
      <div className="flex gap-2">
        <input
          value={query}
          onChange={(e) => onQuery(e.target.value)}
          placeholder="Search notes…"
          className="flex-1 rounded-md border border-zinc-300 px-3 py-1.5 text-sm focus:border-zinc-500 focus:outline-none"
        />
        <button
          onClick={onNew}
          className="rounded-md bg-zinc-900 px-3 py-1.5 text-sm font-medium text-white hover:bg-zinc-700"
        >
          + New
        </button>
      </div>
      <div className="flex gap-1 text-xs">
        <ModeButton active={mode === "text"} onClick={() => onMode("text")}>
          Full-text
        </ModeButton>
        <ModeButton active={mode === "embedding"} onClick={() => onMode("embedding")}>
          Embedding
        </ModeButton>
      </div>
    </div>
  );
}

function ModeButton({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      onClick={onClick}
      className={`rounded-full px-3 py-1 ${
        active ? "bg-zinc-900 text-white" : "bg-zinc-100 text-zinc-600 hover:bg-zinc-200"
      }`}
    >
      {children}
    </button>
  );
}
